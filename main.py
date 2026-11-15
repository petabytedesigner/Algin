"""Category-2 read-only entrypoint; broker mutations are not implemented here.

Strategy and risk output is advisory. No order submission, SL/TP changes or
partial close can be requested until later categories are verified.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

import config
from data.data_handler import DataHandler
from data.market_sessions import MarketSessions
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


def build_trade_plan(signal, atr, account, instrument, quote, exposure, losses, *, now):
    """P5: bridge a strategy signal to a fully validated preview-only plan.

    The caller must supply reconciled exposure and verified loss-history inputs.
    The category-1 observation loop does not fabricate these attestations.
    """
    return TradePlanner(build_risk_policy()).plan(
        signal, atr, account, instrument, quote, exposure, losses, now=now
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
    risk = build_risk_engine()
    data = DataHandler()
    strategy = Strategy()
    for instrument in INSTRUMENTS:
        # Historical V10 inputs; category 5 must validate completed candles.
        candles = data.get_candles(instrument, "M15", 200)
        if not candles or len(candles) < 50:
            LOG.info("%s: no usable history", instrument)
            continue
        closes = [c["close"] for c in candles]
        fast = strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = strategy.atr_engine.atr(candles, period=config.ATR_PERIOD)
        if fast is None or slow is None or not atr or atr <= 0:
            continue
        zone_low = min(c["low"] for c in candles[-10:])
        zone_high = max(c["high"] for c in candles[-10:])
        signals = [
            strategy.evaluate_signal(instrument, direction, candles, fast, slow,
                                     atr, zone_low, zone_high, now)
            for direction in ("LONG", "SHORT")
        ]
        signals = [s for s in signals if s is not None]
        if not signals:
            continue
        signal = max(signals, key=lambda s: s.score)
        decision = evaluate_signal_risk(risk, signal, now)
        LOG.info("%s %s grade=%s score=%.2f risk=%s OBSERVE_ONLY",
                 instrument, signal.direction, signal.grade,
                 signal.score, decision.code.value)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Algin Category 2 read-only baseline")
    parser.add_argument("--check", action="store_true", help="Verify imports without network")
    parser.add_argument("--once", action="store_true", help="One read-only scan")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if config.ORDER_WRITES_ENABLED:
        LOG.error("Category-1 build rejects any order-enabled configuration")
        return 3
    if args.check:
        LOG.info("Category-2 imports OK; broker writes DISABLED")
        return 0
    if args.once:
        return scan_once()
    while True:
        result = scan_once()
        if result:
            return result
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
