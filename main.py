"""Category-6 read-only entrypoint; broker mutations are not implemented here.

Strategy and risk output remains advisory. Break-even and partial-close
intents may be prepared locally, but real broker mutations are disabled.
"""
from __future__ import annotations

import argparse
import logging
import time
from functools import lru_cache
from uuid import uuid4
from storage.event_log import EventLog, AuditError, AuditProjector
from storage.decision_trace import DecisionTrace, TraceError
from storage.heartbeat import Heartbeat, HeartbeatError
from datetime import datetime, timezone

import config
from data.data_handler import DataHandler
from data.candle_validation import CandleDataError, validate_candles
from data.candle_gate import CandleGate, CandleGateError
from data.news_filter import NewsFilter
from data.market_sessions import MarketSessions
from broker.account_reconciler import AccountReconciler, ReconcileError
from execution.trade_management import ManagementLoop
from execution.trade_coordinator import stable_signal_id
from storage.lifecycle_state import StateError
from storage.intent_journal import JournalError
from execution.trade_executor import BrokerDataError, TradeExecutor
from risk.risk_engine import Grade, RiskConfig, RiskEngine, TradeCandidate
from risk.trade_plan import InstrumentRules, RiskPolicy, TradePlanner
from strategy.strategy import Strategy
from utils.time_tools import utc_now, parse_utc

LOG = logging.getLogger("algin")

INSTRUMENTS = ("EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "AUD_USD",
               "NZD_USD", "GBP_JPY", "EUR_JPY", "XAU_USD", "BTC_USD")


def build_risk_engine() -> RiskEngine:
    """Use the RiskEngine public contract without introducing sizing policies."""
    return RiskEngine(RiskConfig(
        max_total_risk_pct=config.MAX_TOTAL_RISK_PCT,
        risk_pct_grade_a=config.RISK_GRADE_A_PCT,
        risk_pct_grade_b=config.RISK_GRADE_B_PCT,
        max_open_trades_total=config.MAX_ACTIVE_TRADES,
        max_open_trades_grade_a=config.MAX_TRADES_GRADE_A,
        max_open_trades_grade_b=config.MAX_TRADES_GRADE_B,
        cooldown_minutes_after_loss=30,
        allow_multiple_trades_same_instrument=False,
        allow_hedging_same_instrument=False,
    ))


def evaluate_signal_risk(risk: RiskEngine, signal, now: datetime):
    """Advisory only; caller must verify there is NO broker exposure."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware signal timestamp required")
    candidate = TradeCandidate(
        instrument=signal.instrument, direction=signal.direction,
        grade=Grade(signal.grade), score=signal.score,
        requested_risk_pct=(
            config.RISK_GRADE_A_PCT if signal.grade == "A"
            else config.RISK_GRADE_B_PCT
        ),
        timestamp=now.astimezone(timezone.utc),
    )
    return risk.evaluate_candidate(candidate, [])



def build_risk_policy() -> RiskPolicy:
    """One authoritative Category-2 planning policy; no broker mutations."""
    from decimal import Decimal

    return RiskPolicy(
        grade_a_pct=Decimal(str(config.RISK_GRADE_A_PCT)),
        grade_b_pct=Decimal(str(config.RISK_GRADE_B_PCT)),
        max_total_risk_pct=Decimal(str(config.MAX_TOTAL_RISK_PCT)),
        max_instrument_risk_pct=Decimal(str(config.MAX_INSTRUMENT_RISK_PCT)),
        max_currency_risk_pct=Decimal(str(config.MAX_CURRENCY_RISK_PCT)),
        max_daily_loss_pct=Decimal(str(config.MAX_DAILY_LOSS_PCT)),
        max_trades_grade_a=config.MAX_TRADES_GRADE_A,
        max_trades_grade_b=config.MAX_TRADES_GRADE_B,
        max_active_trades=config.MAX_ACTIVE_TRADES,
        max_consecutive_losses=config.MAX_CONSECUTIVE_LOSSES,
        max_drawdown_pct=Decimal(str(config.MAX_DRAWDOWN_PCT)),
        reward_to_risk=Decimal(str(config.RISK_REWARD_RATIO)),
        atr_stop_multiple=Decimal(str(config.ATR_STOP_MULTIPLE)),
        minimum_stop_pips=Decimal(str(config.MIN_DISTANCE_PIPS)),
        max_margin_fraction=Decimal(str(config.MAX_MARGIN_FRACTION)),
        max_quote_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
    )


def build_trade_plan(signal, atr, account, instrument, quote, exposure, losses, *, now, structure_level=None):
    """P5: bridge a strategy signal to a fully validated preview-only plan.

    The caller must supply reconciled exposure and verified loss-history inputs.
    The category-1 observation loop does not fabricate these attestations.
    """
    return TradePlanner(build_risk_policy()).plan(
        signal, atr, account, instrument, quote, exposure, losses,
        now=now, structure_level=structure_level
    )


@lru_cache(maxsize=1)
def build_news_filter():
    """One verified calendar cache across observation cycles."""
    return NewsFilter(
        ttl_minutes=config.NEWS_CACHE_TTL_MINUTES,
        max_age_minutes=config.NEWS_CACHE_MAX_AGE_MINUTES
    )


def _scan_once(cycle: str) -> int:
    """One read-only observation cycle; fail closed on broker uncertainty."""
    audit = EventLog(config.CSV_PATH) if config.CSV_ENABLED else None
    decision_trace = DecisionTrace(config.CSV_PATH) if config.CSV_ENABLED else None
    heartbeat = Heartbeat(config.HEARTBEAT_PATH)
    def record(event, reason, *, instrument=None, outcome="BLOCKED", stage="BLOCK", direction=None):
        if audit is None: return
        # Specific event switches take priority over the general signals
        # flag, preventing unintended news/session/cooldown event logging.
        if stage == "SIGNAL":
            enabled = config.CSV_LOG_SIGNALS
        elif reason.startswith("NEWS_"):
            enabled = config.CSV_LOG_NEWS_BLOCK
        elif reason == "SESSION_BLOCK":
            enabled = config.CSV_LOG_SESSION_BLOCK
        elif reason == "COOLDOWN":
            enabled = config.CSV_LOG_COOLDOWN
        else:
            enabled = stage == "BLOCK" and config.CSV_LOG_SIGNALS
        if enabled:
            try:
                audit.emit(event_id=uuid4().hex,correlation_id=cycle,
                           instrument=instrument,direction=direction,
                           stage=stage,event=event,outcome=outcome,
                           reason_code=reason)
            except AuditError:
                # Persist the precise root cause before legacy caller paths
                # can catch the failure and return without a final heartbeat.
                try:
                    heartbeat.write(
                        "BLOCKED", reason_code="AUDIT_FAILURE", cycle_id=cycle
                    )
                except HeartbeatError:
                    LOG.error("Audit and heartbeat both unavailable")
                raise
    try:
        heartbeat.write("ACTIVE",cycle_id=cycle)
    except HeartbeatError:
        LOG.error("Heartbeat unavailable; scan blocked")
        return 3
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID locally")
        try:
            record("ENTRY_DENIED","MISSING_CREDENTIALS")
            heartbeat.write("BLOCKED",reason_code="MISSING_CREDENTIALS",cycle_id=cycle)
        except (AuditError,HeartbeatError): return 3
        return 2
    executor = TradeExecutor()
    try:
        open_trades = executor.get_open_trades()
    except BrokerDataError as exc:
        LOG.error("Cannot verify open broker exposure: %s", exc)
        try:
            record("ENTRY_DENIED","BROKER_UNAVAILABLE")
            heartbeat.write("BLOCKED",reason_code="BROKER_UNAVAILABLE",cycle_id=cycle)
        except (AuditError,HeartbeatError): pass
        return 3
    if open_trades:
        LOG.warning("Broker positions exist; risk assessment blocked until reconciliation")
        try:
            record("ENTRY_DENIED","UNRECONCILED_EXPOSURE")
            heartbeat.write("BLOCKED",reason_code="UNRECONCILED_EXPOSURE",cycle_id=cycle)
        except (AuditError,HeartbeatError): return 3
        return 0
    now = utc_now()
    if config.ENABLE_SESSIONS and not MarketSessions().can_trade(now):
        LOG.info("Outside configured session (read-only scan)")
        try:
            record("ENTRY_DENIED","SESSION_BLOCK")
            heartbeat.write("BLOCKED",reason_code="SESSION_BLOCK",cycle_id=cycle)
        except (AuditError,HeartbeatError): return 3
        return 0
    # A successful openTrades GET does not establish an EMPTY account:
    # pending orders and outcome-unknown attempts require reconciliation.
    # Signals here are observations, never an ACCEPTED risk decision.
    data = DataHandler()
    # Do not treat a configured symbol as tradable until the broker confirms
    # its account-specific instrument metadata in this scan generation.
    # A failed catalog GET cannot be repaired by a valid pricing response.
    catalog = data.get_account_instruments()
    if not isinstance(catalog, dict):
        record("ENTRY_DENIED", "INSTRUMENT_CATALOG_UNVERIFIED")
        heartbeat.write(
            "BLOCKED", reason_code="INSTRUMENT_CATALOG_UNVERIFIED",
            cycle_id=cycle,
        )
        return 3
    strategy = Strategy()
    gate = CandleGate(config.ACCOUNT_ID)
    calendar = build_news_filter()
    for instrument in INSTRUMENTS:
        rules = catalog.get(instrument)
        if not isinstance(rules, InstrumentRules) or rules.kind != "CURRENCY":
            record("ENTRY_DENIED", "INSTRUMENT_NOT_AUTHORIZED",
                   instrument=instrument)
            continue
        # Historical V10 inputs; category 5 must validate completed candles.
        candles = data.get_candles(instrument, "M15", 200)
        if not candles or len(candles) < 50:
            LOG.info("%s: no usable completed M15 history", instrument)
            record("ENTRY_DENIED","M15_MISSING",instrument=instrument)
            continue
        try:
            candles = validate_candles(
                candles, now=now, min_count=50,
                max_age_minutes=config.SIGNAL_CANDLE_MAX_AGE_MINUTES
            )
        except (CandleDataError, ValueError) as exc:
            LOG.warning("%s: blocked invalid M15 data: %s", instrument, exc)
            record("ENTRY_DENIED","M15_INVALID",instrument=instrument)
            continue
        # Do not consume the candle when the instrument is not tradeable.
        quote = data.get_price(instrument, rules=rules)
        if not MarketSessions().instrument_tradeable(
            instrument, now=now, broker_tradeable=quote is not None,
            require_session=config.ENABLE_SESSIONS,
        ):
            LOG.info("%s: instrument not verified tradeable", instrument)
            record("ENTRY_DENIED","QUOTE_OR_SESSION_UNVERIFIED",instrument=instrument)
            continue
        try:
            if not gate.claim(instrument, candles[-1]["time"]):
                LOG.debug("%s: completed M15 candle already processed", instrument)
                record("ENTRY_DENIED","M15_ALREADY_PROCESSED",instrument=instrument)
                continue
        except CandleGateError as exc:
            LOG.warning("%s: M15 checkpoint unavailable: %s", instrument, exc)
            record("ENTRY_DENIED","M15_CHECKPOINT_UNAVAILABLE",instrument=instrument)
            continue
        closes = [c["close"] for c in candles]
        fast = strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = strategy.atr_engine.atr(candles, period=config.ATR_PERIOD)
        if fast is None or slow is None or not atr or atr <= 0:
            record("ENTRY_DENIED","INDICATORS_UNAVAILABLE",instrument=instrument)
            continue
        # Legacy arguments retained for the Strategy public interface.
        # Category 5 derives the true retest zone from PRE-breakout swings.
        zone_low = min(c["low"] for c in candles[:-1][-10:])
        zone_high = max(c["high"] for c in candles[:-1][-10:])
        signals = []
        for direction in ("LONG", "SHORT"):
            signal = strategy.evaluate_signal(
                instrument, direction, candles, fast, slow,
                atr, zone_low, zone_high, now
            )
            # Persist only explicitly allowlisted decision evidence. A signal
            # is OBSERVED, never a verified risk approval or broker fill.
            if decision_trace is not None:
                try:
                    scoring = strategy.scoring.config
                    signal_id = (
                        stable_signal_id(
                            config.ACCOUNT_ID, instrument, direction,
                            parse_utc(candles[-1]["time"]),
                            setup_id=getattr(signal, "setup_id", None),
                        ) if signal is not None else None
                    )
                    decision_trace.append(
                        cycle_id=cycle, instrument=instrument, direction=direction,
                        timestamp=now, candle_time=candles[-1]["time"],
                        decision="OBSERVED" if signal is not None else "REJECTED",
                        reason_code="OBSERVE_ONLY" if signal is not None else (
                            strategy.last_rejection_reason or "UNSPECIFIED"
                        ),
                        strategy_version=strategy.TRACE_VERSION,
                        inputs={
                            "ema_fast": fast, "ema_slow": slow,
                            "atr": atr, "close": candles[-1]["close"],
                            "bid": quote["bid"], "ask": quote["ask"],
                        },
                        metrics=(getattr(signal, "metrics", {}) if signal is not None else
                                 (strategy.last_evaluation_metrics or {})),
                        policy={
                            "ema_fast_period": config.EMA_FAST,
                            "ema_slow_period": config.EMA_SLOW,
                            "atr_period": config.ATR_PERIOD,
                            "max_retest_bars": config.MAX_RETEST_BARS,
                            "grade_a_min": scoring.grade_a_min,
                            "grade_b_min": scoring.grade_b_min,
                            "grade_c_min": scoring.grade_c_min,
                            "max_spread_pips": config.MAX_SPREAD_PIPS,
                            "risk_grade_a_pct": config.RISK_GRADE_A_PCT,
                            "risk_grade_b_pct": config.RISK_GRADE_B_PCT,
                            "max_total_risk_pct": config.MAX_TOTAL_RISK_PCT,
                            "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                            "max_drawdown_pct": config.MAX_DRAWDOWN_PCT,
                            "min_distance_pips": config.MIN_DISTANCE_PIPS,
                        },
                        risk_status="NOT_EVALUATED_UNRECONCILED",
                        score=(signal.score if signal is not None else
                               strategy.last_evaluation_score),
                        grade=(signal.grade if signal is not None else
                               strategy.last_evaluation_grade),
                        signal_id=signal_id,
                        setup_id=(getattr(signal, "setup_id", None)
                                  if signal is not None else None),
                        attempt_id=None, trade_id=None, transaction_id=None,
                    )
                except (TraceError, ValueError, TypeError, KeyError) as exc:
                    raise AuditError("Decision evidence unavailable") from exc
            if signal is not None:
                signals.append(signal)
            else:
                reason = getattr(strategy, "last_rejection_reason", None)
                if not isinstance(reason, str) or not reason:
                    reason = "UNSPECIFIED"
                record("SIGNAL_REJECTED", "SIGNAL_" + reason,
                       instrument=instrument, direction=direction)
        if not signals:
            record("ENTRY_DENIED","STRATEGY_NO_SIGNAL",instrument=instrument)
            continue
        signal = max(signals, key=lambda s: s.score)
        LOG.info("%s %s grade=%s score=%.2f risk=NOT_EVALUATED_UNRECONCILED OBSERVE_ONLY",
                 instrument, signal.direction, signal.grade, signal.score)
        record("SIGNAL_OBSERVED","OBSERVE_ONLY",instrument=instrument,
               direction=signal.direction,outcome="INFO",stage="SIGNAL")
        news_status = calendar.block_trading(instrument, now)
        if news_status is not None:
            LOG.warning("%s: entry blocked by news gate: %s", instrument, news_status)
            record("ENTRY_DENIED","NEWS_UNKNOWN" if news_status.startswith("NEWS_UNKNOWN")
                   else "NEWS_LOCKOUT",instrument=instrument)
        else:
            record("ENTRY_DENIED","RISK_NOT_RECONCILED",instrument=instrument)
    heartbeat.write("CYCLE_COMPLETE",cycle_id=cycle)
    return 0


def scan_once() -> int:
    """Fault boundary: a failed audit/scan never leaves silent ACTIVE status."""
    cycle = uuid4().hex
    try:
        outcome = _scan_once(cycle)
        if outcome != 0:
            # Some legacy error branches return a code before writing the
            # final heartbeat (for example, when CSV audit itself fails).
            # Never leave a previous ACTIVE state after such a failure.
            hb = Heartbeat(config.HEARTBEAT_PATH)
            current = hb.read()
            if current["cycle_id"] != cycle or current["state"] == "ACTIVE":
                hb.write(
                    "BLOCKED", reason_code="SCAN_INCOMPLETE", cycle_id=cycle
                )
        return outcome
    except Exception as exc:
        # Do not log raw broker replies, authorization headers or payloads.
        LOG.error("Signal scan blocked by %s", type(exc).__name__)
        reason = "AUDIT_FAILURE" if isinstance(exc, AuditError) else "SCAN_ERROR"
        try:
            Heartbeat(config.HEARTBEAT_PATH).write(
                "BLOCKED", reason_code=reason, cycle_id=cycle
            )
        except HeartbeatError:
            LOG.error("Unable to persist BLOCKED heartbeat")
        return 3


def project_account_audit(reconciler, report):
    """Read-only/idempotent projection AFTER broker reconciliation.

    No event is evidence of a broker write. On blocked accounts, emit only
    durable journal observations; never certify an inconsistent lifecycle.
    """
    if not config.CSV_ENABLED:
        return ()
    projector = AuditProjector(config.ACCOUNT_ID, EventLog(config.CSV_PATH))
    attempts = reconciler.journal.snapshot()
    state = None if report.blocked_reasons else reconciler.store.load()
    lookup = None
    if state is not None:
        # Read-only OANDA transaction evidence; immutable transaction facts
        # can be cached within this process to avoid one GET per old closure
        # on every subsequent management cycle.
        lookup = getattr(reconciler, "_audit_transaction_lookup", None)
        if lookup is None:
            @lru_cache(maxsize=512)
            def lookup(transaction_id):
                raw = reconciler._read("/transactions/" + transaction_id)
                return raw["transaction"]
            reconciler._audit_transaction_lookup = lookup
    return projector.project(attempts, state, transaction_fetcher=lookup)


def manage_once() -> int:
    """Read-only recovery before evaluating entry sessions or signals."""
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("OANDA Practice credentials required for lifecycle recovery")
        return 2
    cycle = uuid4().hex
    heartbeat = Heartbeat(config.HEARTBEAT_PATH)
    try:
        heartbeat.write("ACTIVE",cycle_id=cycle)
        reconciler = AccountReconciler(config.ACCOUNT_ID)
        report = ManagementLoop(reconciler).run_once()
        project_account_audit(reconciler, report)
        if report.blocked_reasons:
            LOG.error("Lifecycle blocked: %s", "; ".join(report.blocked_reasons))
            heartbeat.write("BLOCKED",reason_code="RECONCILIATION_BLOCKED",
                            cycle_id=cycle)
            return 3
        LOG.info("Account cursor=%s managed_trades=%d no_broker_writes",
                 report.cursor, len(report.owned_open_trades))
        heartbeat.write("CYCLE_COMPLETE",cycle_id=cycle)
        return 0
    except (ReconcileError, StateError, JournalError, HeartbeatError, AuditError, ValueError) as exc:
        LOG.error("Lifecycle reconciliation blocked: %s", exc)
        try:
            heartbeat.write("BLOCKED",reason_code="RECOVERY_ERROR",cycle_id=cycle)
        except HeartbeatError:
            pass
        return 3


def heartbeat_check() -> int:
    """External/read-only watchdog probe; missing/stale/BLOCKED is nonzero."""
    try:
        status = Heartbeat(config.HEARTBEAT_PATH).inspect()
        LOG.info("Heartbeat health=%s age=%ss state=%s",
                 status["health"], status["age_seconds"], status["state"])
        return 0 if status["health"] in ("ACTIVE", "CYCLE_COMPLETE") else 3
    except (HeartbeatError, ValueError) as exc:
        LOG.error("Heartbeat unavailable: %s", type(exc).__name__)
        return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Algin Category 6 read-only baseline")
    parser.add_argument("--check", action="store_true", help="Verify imports without network")
    parser.add_argument("--once", action="store_true", help="One legacy read-only signal scan")
    parser.add_argument("--manage-once", action="store_true",
                        help="Reconcile broker/lifecycle state without new entries")
    parser.add_argument("--heartbeat-check", action="store_true",
                        help="Read-only health probe for external watchdogs")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if config.ORDER_WRITES_ENABLED:
        LOG.error("Category-6 build rejects any order-enabled configuration")
        return 3
    if args.check:
        LOG.info("Category-6 imports OK; broker writes DISABLED")
        return 0
    if args.heartbeat_check:
        return heartbeat_check()
    if args.manage_once:
        return manage_once()
    if args.once:
        return scan_once()
    # Hold the account lease for the WHOLE running service lifetime.
    # A second instance cannot interleave between scan cycles.
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("OANDA Practice credentials required")
        return 2
    try:
        with AccountReconciler(config.ACCOUNT_ID) as reconciler:
            while True:
                # P36: recovery before checking any entry-session constraint.
                report = reconciler.run()
                project_account_audit(reconciler, report)
                if report.blocked_reasons:
                    LOG.error("Lifecycle blocked: %s", "; ".join(report.blocked_reasons))
                    return 3
                result = scan_once()
                if result:
                    return result
                time.sleep(60)
    except Exception as exc:
        LOG.error("Management process blocked by %s", type(exc).__name__)
        try:
            Heartbeat(config.HEARTBEAT_PATH).write(
                "BLOCKED", reason_code="MANAGEMENT_ERROR"
            )
        except HeartbeatError:
            LOG.error("Unable to persist BLOCKED management heartbeat")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
