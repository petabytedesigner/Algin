"""Frozen-scenario holdout and sensitivity analysis for OFFLINE research.

All statistics are descriptive and explicitly unverified, not a performance
claim. Test periods are separated by ENTRY time and any trade crossing the
cut is purged rather than counted in both partitions. No parameter fitting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt

from analytics.conservative_backtest import BacktestError, dec
from analytics.performance import report
from analytics.strategy_replay import (
    ReplayAssumptions, ReplayError, ReplayResult, ResearchTrade, replay,
)
from utils.time_tools import require_utc


@dataclass(frozen=True)
class HoldoutReport:
    in_sample: dict
    out_of_sample: dict
    by_grade: dict
    by_instrument: dict
    purged_cross_boundary: int
    unresolved_positions: int
    source_kind: str
    status: str = "RESEARCH_ONLY"
    broker_verified: bool = False


def _wilson(wins, n):
    if n < 1:
        return None
    z = 1.959963984540054
    p = wins / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    error = z*sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return (max(0., (center-error)*100), min(100., (center+error)*100))


def _stats(trades, initial_equity, min_sample):
    if (type(min_sample) is not int or min_sample < 30):
        raise ReplayError("Minimum inferential sample must be at least 30")
    chron = sorted(trades, key=lambda t: (t.closed_at, t.trade_id))
    base = []
    for index, trade in enumerate(chron):
        if not isinstance(trade, ResearchTrade):
            raise ReplayError("Trade report provenance is incomplete")
        base.append({
            "trade_id": "research-" + str(index),
            "instrument": trade.instrument, "session": "UNVERIFIED",
            "net_pl": trade.net_home,
        })
    descriptive = report(base, starting_equity=initial_equity)
    n = len(chron)
    wins = sum(t.net_home > 0 for t in chron)
    return {
        "count": n, "descriptive": descriptive,
        "mean_net_r": (
            sum((t.net_r for t in chron), Decimal("0")) / n
            if n else None
        ),
        "sample_sufficiency": (
            "MINIMUM_N_REACHED" if n >= min_sample else "INSUFFICIENT_SAMPLE"
        ),
        "win_rate_wilson_95_pct": _wilson(wins, n) if n >= min_sample else None,
        "statistically_validated_edge": False,
    }


def holdout(result: ReplayResult, *, starting_equity, holdout_start,
            min_sample=30) -> HoldoutReport:
    if not isinstance(result, ReplayResult) or result.broker_verified:
        raise ReplayError("Offline result required")
    split = require_utc(holdout_start)
    capital = dec(starting_equity, "starting_equity")
    if capital <= 0:
        raise ReplayError("Nonpositive research capital")
    train, test, crossed = [], [], []
    for trade in result.trades:
        require_utc(trade.opened_at)
        require_utc(trade.closed_at)
        if trade.opened_at >= trade.closed_at:
            raise ReplayError("Inverted trade chronology")
        if trade.opened_at < split and trade.closed_at > split:
            crossed.append(trade)
        elif trade.closed_at <= split:
            train.append(trade)
        elif trade.opened_at >= split:
            test.append(trade)
        else:
            raise ReplayError("Unclassified holdout trade")
    # Do not use training P/L to set test initial equity in a way that
    # silently grants a hypothetical separate account. Both sub-reports are
    # DESCRIPTIVE; whole-path equity is in the full replay, not recomposed.
    qualified = train + test
    by_grade = {
        g: {
            "in_sample": _stats(
                [t for t in train if t.grade == g], capital, min_sample
            ),
            "out_of_sample": _stats(
                [t for t in test if t.grade == g], capital, min_sample
            )
        } for g in ("A", "B")
    }
    instruments = sorted(set(t.instrument for t in qualified))
    by_instrument = {
        instrument: {
            "in_sample": _stats(
                [t for t in train if t.instrument == instrument],
                capital, min_sample
            ),
            "out_of_sample": _stats(
                [t for t in test if t.instrument == instrument],
                capital, min_sample
            ),
        } for instrument in instruments
    }
    return HoldoutReport(
        in_sample=_stats(train, capital, min_sample),
        out_of_sample=_stats(test, capital, min_sample),
        by_grade=by_grade, by_instrument=by_instrument,
        purged_cross_boundary=len(crossed),
        unresolved_positions=result.unresolved,
        source_kind=result.source_kind,
    )


def cost_sensitivity(payload, rules, evidence, *, scenarios, holdout_start,
                     min_sample=30):
    """Frozen, operator-declared assumptions; NO auto-fitting on test data.

    All scenarios replay independently because execution costs can change
    fill feasibility, position units, future risk/equity and trade selection.
    """
    if (not isinstance(scenarios, dict) or not scenarios
        or len(scenarios) > 12 or any(
            not isinstance(k, str) or not k or len(k) > 48
            or not isinstance(v, ReplayAssumptions)
            for k, v in scenarios.items()
        )):
        raise ReplayError("Explicit, finite cost scenarios required")
    source = {v.source_kind for v in scenarios.values()}
    currencies = {v.account_currency for v in scenarios.values()}
    capital = {v.starting_equity for v in scenarios.values()}
    if len(source) != 1 or len(currencies) != 1 or len(capital) != 1:
        raise ReplayError("Sensitivity scenarios need same source, account and capital")
    result = {}
    for label, settings in sorted(scenarios.items()):
        run = replay(payload, rules, evidence, assumptions=settings)
        result[label] = {
            "assumptions": settings,
            "replay": run,
            "holdout": holdout(
                run, starting_equity=settings.starting_equity,
                holdout_start=holdout_start, min_sample=min_sample,
            ),
        }
    return result
