"""Deterministic conservative exit replay and cost-aware realized-P/L reporting.

This module never places broker orders and does not estimate signal quality.
Reported P/L only includes fields explicitly supplied by the caller.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import argparse
from pathlib import Path


class AnalyticsError(ValueError):
    pass


def finite(value):
    try:
        x = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise AnalyticsError("Invalid numeric input") from exc
    if not x.is_finite():
        raise AnalyticsError("Non-finite numeric input")
    return x


def conservative_exit(*, direction: str, entry: str, stop: str, target: str,
                      open_price: str, high: str, low: str, close: str,
                      price_side: str):
    """Exit with BID OHLC for LONG, ASK OHLC for SHORT. Same-bar stop wins.

    Return (reason, price) or None. Uses no future prices. Stops can gap through.
    This OHLC-only simulation is pessimistic when SL and TP touch in the same bar.
    """
    if direction not in ("LONG", "SHORT"):
        raise AnalyticsError("Unknown direction")
    required_side = "BID" if direction == "LONG" else "ASK"
    if price_side != required_side:
        raise AnalyticsError("Executable bid/ask candles required; midpoint unsupported")
    e, s, t, o, h, l, c = map(finite, (entry, stop, target, open_price, high, low, close))
    if min(e, s, t, o, h, l, c) <= 0 or not l <= min(o, c) <= max(o, c) <= h:
        raise AnalyticsError("Invalid OHLC or prices")
    if direction == "LONG":
        if not s < e < t:
            raise AnalyticsError("Invalid LONG stop/target")
        if o <= s:
            return "STOP_GAP", o
        if o >= t:
            return "TARGET_GAP", t  # Conservative: no positive gap improvement.
        if l <= s:
            return "STOP", s
        if h >= t:
            return "TARGET", t
    else:
        if not t < e < s:
            raise AnalyticsError("Invalid SHORT stop/target")
        if o >= s:
            return "STOP_GAP", o
        if o <= t:
            return "TARGET_GAP", t
        if h >= s:
            return "STOP", s
        if l <= t:
            return "TARGET", t
    return None


def summarize(trades: list, starting_equity) -> dict:
    """Trades have gross_price_pl_home and costs in the account currency.

    If gross_price_pl_home uses actual broker executed entry and exit prices,
    spread is already included: spread_cost_home MUST be zero.
    Subtract spread once only for mid-price simulations.
    """
    start = finite(starting_equity)
    if start <= 0:
        raise AnalyticsError("Starting equity must be positive")
    if not isinstance(trades, list):
        raise AnalyticsError("Trades must be list")
    net = []
    instruments = {}
    equity = peak = start
    worst_drawdown_home = Decimal("0")
    gross_positive = gross_negative = Decimal("0")
    for trade in trades:
        if not isinstance(trade, dict) or not trade.get("instrument"):
            raise AnalyticsError("Missing instrument")
        price_pl = finite(trade["gross_price_pl_home"])
        spread = finite(trade.get("spread_cost_home", 0))
        commission = finite(trade.get("commission_home", 0))
        slippage = finite(trade.get("slippage_home", 0))
        financing = finite(trade.get("financing_home", 0))
        if min(spread, commission, slippage, financing) < 0:
            raise AnalyticsError("Costs must be nonnegative; use explicit rebate separately")
        result = price_pl - spread - commission - slippage - financing
        net.append(result)
        equity += result
        peak = max(peak, equity)
        worst_drawdown_home = max(worst_drawdown_home, peak-equity)
        instrument = str(trade["instrument"])
        instruments[instrument] = instruments.get(instrument, Decimal("0")) + result
        if result > 0:
            gross_positive += result
        elif result < 0:
            gross_negative += -result
    n = len(net)
    wins = sum(x > 0 for x in net)
    losses = sum(x < 0 for x in net)
    net_total = sum(net, Decimal("0"))
    if gross_negative == 0:
        factor = "UNDEFINED_NO_LOSSES" if gross_positive else "UNDEFINED_NO_TRADES"
    else:
        factor = str(gross_positive / gross_negative)
    largest_winner = max([x for x in net if x > 0], default=Decimal("0"))
    return {
        "count": n, "wins": wins, "losses": losses,
        "win_rate_pct": str(Decimal(wins)*100/n) if n else None,
        "net_profit_home": str(net_total),
        "ending_equity_home": str(equity),
        "expectancy_home_per_trade": str(net_total/n) if n else None,
        "profit_factor": factor,
        "max_drawdown_home": str(worst_drawdown_home),
        "max_drawdown_pct_of_start": str(worst_drawdown_home*100/start),
        "largest_win_home": str(largest_winner),
        "largest_winner_fraction_of_positive_pl": str(largest_winner/gross_positive) if gross_positive else None,
        "net_by_instrument": {k: str(v) for k, v in sorted(instruments.items())},
        "notes": "All costs must be supplied. Broker-execution gross PL already includes spread; do not subtract twice."
    }


def main():
    p = argparse.ArgumentParser(description="Offline performance report, no broker access")
    p.add_argument("trades_json", help="JSON list of realized/simulated trade records")
    p.add_argument("--starting-equity", required=True)
    args = p.parse_args()
    content = json.loads(Path(args.trades_json).read_text(encoding="utf-8"))
    print(json.dumps(summarize(content, args.starting_equity), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
