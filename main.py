"""Category-5 read-only entrypoint; broker mutations are not implemented here.

Strategy and risk output remains advisory. Break-even and partial-close
intents may be prepared locally, but real broker mutations are disabled.
"""
from __future__ import annotations

import argparse
import logging
import time
from functools import lru_cache
from datetime import datetime, timezone

import config
from data.data_handler import DataHandler
from data.candle_validation import CandleDataError, validate_candles
from data.candle_gate import CandleGate, CandleGateError
from data.news_filter import NewsFilter
from data.market_sessions import MarketSessions
from broker.account_reconciler import AccountReconciler, ReconcileError
from execution.trade_management import ManagementLoop
from storage.lifecycle_state import StateError
from storage.intent_journal import JournalError
from execution.trade_executor import BrokerDataError, TradeExecutor
from risk.risk_engine import Grade, RiskConfig, RiskEngine, TradeCandidate
from risk.trade_plan import RiskPolicy, TradePlanner
from strategy.strategy import Strategy
from utils.time_tools import utc_now

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


def scan_once() -> int:
    """One read-only observation cycle; fail closed on broker uncertainty."""
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID locally")
        return 2
    executor = TradeExecutor()
    try:
        open_trades = executor.get_open_trades()
    except BrokerDataError as exc:
        LOG.error("Cannot verify open broker exposure: %s", exc)
        return 3
    if open_trades:
        LOG.warning("Broker positions exist; risk assessment blocked until reconciliation")
        return 0
    now = utc_now()
    if config.ENABLE_SESSIONS and not MarketSessions().can_trade(now):
        LOG.info("Outside configured session (read-only scan)")
        return 0
    # A successful openTrades GET does not establish an EMPTY account:
    # pending orders and outcome-unknown attempts require reconciliation.
    # Signals here are observations, never an ACCEPTED risk decision.
    data = DataHandler()
    strategy = Strategy()
    gate = CandleGate(config.ACCOUNT_ID)
    calendar = build_news_filter()
    for instrument in INSTRUMENTS:
        # Historical V10 inputs; category 5 must validate completed candles.
        candles = data.get_candles(instrument, "M15", 200)
        if not candles or len(candles) < 50:
            LOG.info("%s: no usable completed M15 history", instrument)
            continue
        try:
            candles = validate_candles(
                candles, now=now, min_count=50,
                max_age_minutes=config.SIGNAL_CANDLE_MAX_AGE_MINUTES
            )
        except (CandleDataError, ValueError) as exc:
            LOG.warning("%s: blocked invalid M15 data: %s", instrument, exc)
            continue
        # Do not consume the candle when the instrument is not tradeable.
        quote = data.get_price(instrument)
        if not MarketSessions().instrument_tradeable(
            instrument, now=now, broker_tradeable=quote is not None
        ):
            LOG.info("%s: instrument not verified tradeable", instrument)
            continue
        try:
            if not gate.claim(instrument, candles[-1]["time"]):
                LOG.debug("%s: completed M15 candle already processed", instrument)
                continue
        except CandleGateError as exc:
            LOG.warning("%s: M15 checkpoint unavailable: %s", instrument, exc)
            continue
        closes = [c["close"] for c in candles]
        fast = strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = strategy.atr_engine.atr(candles, period=config.ATR_PERIOD)
        if fast is None or slow is None or not atr or atr <= 0:
            continue
        # Legacy arguments retained for the Strategy public interface.
        # Category 5 derives the true retest zone from PRE-breakout swings.
        zone_low = min(c["low"] for c in candles[:-1][-10:])
        zone_high = max(c["high"] for c in candles[:-1][-10:])
        signals = [
            strategy.evaluate_signal(instrument, direction, candles, fast, slow,
                                     atr, zone_low, zone_high, now)
            for direction in ("LONG", "SHORT")
        ]
        signals = [s for s in signals if s is not None]
        if not signals:
            continue
        signal = max(signals, key=lambda s: s.score)
        LOG.info("%s %s grade=%s score=%.2f risk=NOT_EVALUATED_UNRECONCILED OBSERVE_ONLY",
                 instrument, signal.direction, signal.grade, signal.score)
        news_status = calendar.block_trading(instrument, now)
        if news_status is not None:
            LOG.warning("%s: entry blocked by news gate: %s", instrument, news_status)
    return 0


def manage_once() -> int:
    """Read-only recovery before evaluating entry sessions or signals."""
    if not config.API_KEY or not config.ACCOUNT_ID:
        LOG.error("OANDA Practice credentials required for lifecycle recovery")
        return 2
    try:
        report = ManagementLoop(AccountReconciler(config.ACCOUNT_ID)).run_once()
    except (ReconcileError, StateError, JournalError, ValueError) as exc:
        LOG.error("Lifecycle reconciliation blocked: %s", exc)
        return 3
    if report.blocked_reasons:
        LOG.error("Lifecycle blocked: %s", "; ".join(report.blocked_reasons))
        return 3
    LOG.info("Account cursor=%s managed_trades=%d no_broker_writes",
             report.cursor, len(report.owned_open_trades))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Algin Category 5 read-only baseline")
    parser.add_argument("--check", action="store_true", help="Verify imports without network")
    parser.add_argument("--once", action="store_true", help="One legacy read-only signal scan")
    parser.add_argument("--manage-once", action="store_true",
                        help="Reconcile broker/lifecycle state without new entries")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if config.ORDER_WRITES_ENABLED:
        LOG.error("Category-5 build rejects any order-enabled configuration")
        return 3
    if args.check:
        LOG.info("Category-5 imports OK; broker writes DISABLED")
        return 0
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
                if report.blocked_reasons:
                    LOG.error("Lifecycle blocked: %s", "; ".join(report.blocked_reasons))
                    return 3
                result = scan_once()
                if result:
                    return result
                time.sleep(60)
    except (ReconcileError, StateError, JournalError, ValueError) as exc:
        LOG.error("Management process stopped safely: %s", exc)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
