"""Forward *realized-only* analytics from validated durable broker evidence.

Never infer a trade from forward heartbeat/CSV signals. When financial facts
are missing, report NOT_VERIFIED instead of win-rate, expectancy or drawdown.
Full NAV/equity drawdown is unavailable without audited broker NAV history.
"""
from __future__ import annotations

from decimal import Decimal

from broker.financial_ledger import (
    FinancialEvidenceError, trade_summary, validate_ledger,
)
from risk.trade_lifecycle import LifecycleError, TradeRecord
from storage.lifecycle_state import LifecycleStore, StateError
from utils.time_tools import parse_utc


class ForwardPerformanceError(ValueError):
    """Incomplete or inconsistent broker-derived financial evidence."""


def _empty(status):
    return {
        "status": status, "source": "DURABLE_LIFECYCLE_LEDGER",
        "net_pl_home": None, "win_rate_pct": None,
        "expectancy_home": None, "profit_factor": None,
        "realized_only_drawdown_home": None,
        "account_nav_drawdown_pct": None,
        "account_nav_drawdown_status": "NOT_VERIFIED",
        "trades": 0,
    }


def verified_realized_performance(state):
    """State must originate from LifecycleStore.load; revalidate ledger too."""
    if state is None:
        return _empty("NO_PERSISTED_BROKER_EVIDENCE")
    if (not isinstance(state, dict)
        or not isinstance(state.get("closed_results"), dict)
        or not isinstance(state.get("trades"), dict)
        or not isinstance(state.get("financial_ledger"), dict)
        or not isinstance(state.get("processed_transactions"), list)
        or not isinstance(state.get("cursor"), str)):
        raise ForwardPerformanceError("Malformed lifecycle/financial state")
    try:
        ledger = validate_ledger(
            state["financial_ledger"], cursor=state["cursor"],
            processed=state["processed_transactions"],
        )
        closed = state["closed_results"]
        if not closed:
            return _empty("NO_BROKER_VERIFIED_CLOSED_TRADES")
        results = []
        for trade_id, final in closed.items():
            trade = TradeRecord.from_dict(state["trades"][trade_id])
            if (trade.trade_id != trade_id or not trade.closed
                or not trade.result_counted
                or not isinstance(final, dict)
                or final.get("transaction_id") not in
                    state["processed_transactions"]
                or final.get("transaction_id") not in
                    trade.close_transaction_ids):
                raise ForwardPerformanceError("Unverified closed-trade lineage")
            money = trade_summary(ledger, trade_id)
            if (not money["complete"]
                or money["net_pl_home"] is None
                or final["transaction_id"] not in
                    money["related_transaction_ids"]):
                raise ForwardPerformanceError("Incomplete broker trade net P/L")
            closed_time = parse_utc(final["closed_at"])
            results.append((
                closed_time, int(final["transaction_id"]), trade_id,
                Decimal(money["net_pl_home"]),
            ))
        if any(TradeRecord.from_dict(t).closed and tid not in closed
               for tid, t in state["trades"].items()):
            raise ForwardPerformanceError("Closed trade missing accounting result")
    except (FinancialEvidenceError, LifecycleError, KeyError, TypeError,
            ValueError, ArithmeticError, StateError) as exc:
        raise ForwardPerformanceError("Unverified realized performance provenance") from None
    ordered = sorted(results)
    values = [t[3] for t in ordered]
    gain = sum((x for x in values if x > 0), Decimal("0"))
    loss = -sum((x for x in values if x < 0), Decimal("0"))
    equity = peak = drawdown = Decimal("0")
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    n = len(values)
    return {
        "status": "VERIFIED_REALIZED_ONLY",
        "source": "DURABLE_LIFECYCLE_LEDGER",
        "trades": n,
        "net_pl_home": str(sum(values, Decimal("0"))),
        "win_rate_pct": str(Decimal(sum(x > 0 for x in values))*100/n),
        "expectancy_home": str(sum(values, Decimal("0"))/n),
        "profit_factor": (
            str(gain/loss) if loss else ("UNDEFINED_NO_LOSSES" if gain else None)
        ),
        "realized_only_drawdown_home": str(drawdown),
        "account_nav_drawdown_pct": None,
        "account_nav_drawdown_status": "NOT_VERIFIED",
    }


def performance_from_store(account_id, *, directory=".local"):
    try:
        state = LifecycleStore(account_id, directory=directory).load()
        return verified_realized_performance(state)
    except (ForwardPerformanceError, StateError):
        return _empty("INCOMPLETE_OR_CORRUPT_FINANCIAL_EVIDENCE")
