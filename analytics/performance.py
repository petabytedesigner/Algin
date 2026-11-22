"""Deterministic trade-series analytics; caller must provide verified net P/L.

This module never downloads broker transactions or infers missing costs.
Trades must be provided in chronological closing order for drawdown/streaks.
Without reconciled NET results, this is only an offline analytics function.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from analytics.conservative_backtest import dec, BacktestError
from risk.trade_plan import advance_loss_streak


def _median(values):
    if not values:
        return None
    series = sorted(values)
    n = len(series)
    if n % 2:
        return series[n // 2]
    return (series[n // 2 - 1] + series[n // 2]) / Decimal("2")


def report(trades, *, starting_equity):
    """Report P/L, drawdown, streaks, concentration and breakdown.

    The existing four-column trade input contract is preserved. This function
    CANNOT establish whether a supplied net_pl has valid broker provenance.
    """
    equity = dec(starting_equity, "starting_equity")
    if equity <= 0:
        raise BacktestError("Starting equity must be positive")
    if not isinstance(trades, (tuple, list)):
        raise BacktestError("Chronological trade sequence required")
    seen = set()
    pl = []
    by_instrument = defaultdict(list)
    by_session = defaultdict(list)
    peak = equity
    drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")
    max_loss_streak = 0
    max_win_streak = 0
    loss_streak = 0
    win_streak = 0
    for trade in trades:
        if (not isinstance(trade, dict) or
            set(trade) != {"trade_id", "instrument", "session", "net_pl"}):
            raise BacktestError("Incomplete realized trade")
        trade_id = trade["trade_id"]
        if not isinstance(trade_id, str) or not trade_id or trade_id in seen:
            raise BacktestError("Duplicate/invalid trade")
        seen.add(trade_id)
        if not all(isinstance(trade[k], str) and trade[k]
                   for k in ("instrument", "session")):
            raise BacktestError("Missing instrument/session")
        amount = dec(trade["net_pl"], "net_pl")
        pl.append(amount)
        by_instrument[trade["instrument"]].append(amount)
        by_session[trade["session"]].append(amount)
        equity += amount
        if equity > peak:
            peak = equity
        drawdown = max(drawdown, peak - equity)
        max_drawdown_pct = max(
            max_drawdown_pct, (peak - equity) / peak * 100
        )
        loss_streak = advance_loss_streak(loss_streak, amount)
        if amount < 0:
            win_streak = 0
        elif amount > 0:
            win_streak += 1
        else:
            win_streak = 0
        max_loss_streak = max(max_loss_streak, loss_streak)
        max_win_streak = max(max_win_streak, win_streak)
    wins = [x for x in pl if x > 0]
    losses = [x for x in pl if x < 0]
    gain = sum(wins, Decimal("0"))
    loss = -sum(losses, Decimal("0"))
    return {
        "trades": len(pl),
        "net_pl": sum(pl, Decimal("0")),
        "ending_equity": equity,
        "win_rate_pct": Decimal(len(wins))*100/len(pl) if pl else None,
        "expectancy": sum(pl, Decimal("0"))/len(pl) if pl else None,
        "profit_factor": gain/loss if loss else (None if not gain else "INF"),
        "max_drawdown_home": drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "max_loss_streak": max_loss_streak,
        "max_win_streak": max_win_streak,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "mean_win": gain/len(wins) if wins else None,
        "mean_loss": -loss/len(losses) if losses else None,
        "median_trade": _median(pl),
        "largest_win_share_pct": max(wins)/gain*100 if wins else None,
        "by_instrument": {
            k: sum(v, Decimal("0"))
            for k, v in sorted(by_instrument.items())
        },
        "by_session": {
            k: sum(v, Decimal("0"))
            for k, v in sorted(by_session.items())
        },
    }
