"""P74 realized-only performance with verified cost provenance.

Metrics are descriptive, not a profitability forecast. Undefined PF (no
losses) is None, not Infinity. Missing cost events, unmatched trades, or
unsettled trades are rejected, never silently shown as 0/closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from collections import defaultdict

from analytics.costs import CostError, CostLedger, amount
from utils.time_tools import parse_utc
from data.market_sessions import MarketSessions


class PerformanceError(ValueError):
    pass


@dataclass(frozen=True)
class CompletedTrade:
    trade_id: str
    instrument: str
    session: str
    closed_at: str
    transaction_ids: tuple[str, ...]
    opened_at: str

    def __post_init__(self):
        if (not isinstance(self.trade_id, str) or not self.trade_id.isascii()
            or not self.trade_id.isdecimal() or self.session not in (
                "Asia", "London", "New York", "Other"
            ) or not isinstance(self.instrument, str)
            or self.instrument.count("_") != 1):
            raise PerformanceError("Invalid completed trade identity/session")
        try:
            parse_utc(self.closed_at)
        except (ValueError, TypeError) as exc:
            raise PerformanceError("Missing closing timestamp") from exc
        try:
            opened = parse_utc(self.opened_at)
            closed = parse_utc(self.closed_at)
            if opened > closed:
                raise PerformanceError("Close precedes trade opening")
            verified_session = MarketSessions().active_session(opened) or "Other"
            if verified_session != self.session:
                raise PerformanceError("Session label disagrees with opened_at")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, PerformanceError):
                raise
            raise PerformanceError("Unverified trade opening timestamp") from exc
        if (not isinstance(self.transaction_ids, tuple)
            or not self.transaction_ids
            or len(self.transaction_ids) != len(set(self.transaction_ids))):
            raise PerformanceError("Incomplete trade transaction attribution")


def _metrics(records, *, starting_equity=None):
    values = [x[1] for x in records]
    n = len(values)
    winning = [x for x in values if x > 0]
    losing = [x for x in values if x < 0]
    gross_wins = sum(winning, Decimal("0"))
    gross_losses = sum(losing, Decimal("0"))
    net = sum(values, Decimal("0"))
    max_streak = streak = 0
    for value in values:
        if value < 0:
            streak += 1
            max_streak = max(streak, max_streak)
        elif value > 0:
            streak = 0
        # Break-even does not reset the consecutive-loss streak.
    result = {
        "trade_count": n,
        "net_pl_home": str(net),
        "gross_profit_home": str(gross_wins),
        "gross_loss_home": str(gross_losses),
        "average_win_home": str(gross_wins / len(winning)) if winning else None,
        "average_loss_home": str(gross_losses / len(losing)) if losing else None,
        "win_rate_pct": str(Decimal(len(winning)) * 100 / n) if n else None,
        "expectancy_home_per_trade": str(net / n) if n else None,
        "profit_factor": str(gross_wins / -gross_losses) if losing else None,
        "max_consecutive_losses": max_streak,
        "largest_winner_share_of_gross_wins_pct": (
            str(max(winning) * 100 / gross_wins) if winning else None
        ),
        "largest_winner_to_net_pl_ratio": (
            str(max(winning) / net) if winning and net > 0 else None
        ),
    }
    if starting_equity is not None:
        nav = peak = starting_equity
        deepest = Decimal("0")
        deepest_pct = Decimal("0")
        for value in values:
            nav += value
            if nav > peak:
                peak = nav
            drop = peak - nav
            if drop > deepest:
                deepest = drop
            ratio = drop / peak * 100 if peak > 0 else Decimal("0")
            deepest_pct = max(deepest_pct, ratio)
        result.update({
            "starting_equity_home": str(starting_equity),
            "ending_equity_home": str(nav),
            "max_drawdown_home": str(deepest),
            "max_drawdown_pct": str(deepest_pct),
        })
    return result


def performance_report(trades, ledger: CostLedger, *, initial_equity_home):
    if not isinstance(ledger, CostLedger) or not isinstance(trades, (list, tuple)):
        raise PerformanceError("Unknown reporting source")
    initial = amount(initial_equity_home, "initial equity", nonnegative=True)
    if initial <= 0:
        raise PerformanceError("Positive initial equity required")
    if not trades:
        return {
            "status": "NO_TRADES",
            "overall": _metrics([], starting_equity=initial),
            "by_instrument": {}, "by_session": {},
            "high_concentration_warning": False,
        }
    if not all(isinstance(t, CompletedTrade) for t in trades):
        raise PerformanceError("Open/incomplete trade in report")
    if len({t.trade_id for t in trades}) != len(trades):
        raise PerformanceError("Duplicate closed trade")
    records = []
    accounted_tx = set()
    for trade in trades:
        try:
            result = ledger.verify_complete_trade(
                trade.trade_id, trade.transaction_ids
            )
        except CostError as exc:
            raise PerformanceError("Costs not completely attributable") from exc
        if any(row.instrument != trade.instrument
               for row in ledger.for_trade(trade.trade_id)):
            raise PerformanceError("Cost instrument disagrees with trade")
        if any(x in accounted_tx for x in trade.transaction_ids):
            raise PerformanceError("Transaction counted across multiple trades")
        accounted_tx.update(trade.transaction_ids)
        records.append((trade, result))
    if accounted_tx != {row.transaction_id for row in ledger.records}:
        raise PerformanceError("Unattributed broker costs omitted from results")
    records.sort(key=lambda pair: (parse_utc(pair[0].closed_at),
                                   int(pair[0].trade_id)))
    overall = _metrics(records, starting_equity=initial)
    groups = {"instrument": defaultdict(list), "session": defaultdict(list)}
    for t, net in records:
        groups["instrument"][t.instrument].append((t, net))
        groups["session"][t.session].append((t, net))
    return {
        "status": "VERIFIED_CLOSED_TRADES",
        "overall": overall,
        "by_instrument": {
            key: _metrics(value) for key, value in sorted(groups["instrument"].items())
        },
        "by_session": {
            key: _metrics(value) for key, value in sorted(groups["session"].items())
        },
        "high_concentration_warning": (
            Decimal(overall["largest_winner_share_of_gross_wins_pct"]) > 50
            if overall["largest_winner_share_of_gross_wins_pct"] is not None
            else False
        ),
    }
