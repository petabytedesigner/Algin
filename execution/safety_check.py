"""Read-only protection report using the same broker snapshot contract.

CSV/event schema consolidation is deferred to Category 6.
"""
from __future__ import annotations

from typing import Any, Dict

from execution.trade_executor import BrokerDataError, TradeExecutor


class SafetyCheck:
    """Report confirmed missing protections; fail closed if broker data fails."""

    def __init__(self):
        self.executor = TradeExecutor()

    def check_all(self) -> Dict[str, Dict[str, Any]]:
        try:
            trades = self.executor.get_open_trades()
        except BrokerDataError:
            return {"__broker_error__": {
                "issues": ["Broker openTrades unavailable or malformed"]
            }}
        report: Dict[str, Dict[str, Any]] = {}
        for trade in trades:
            has_sl = trade["sl"] is not None
            has_tp = trade["tp"] is not None
            issues = []
            if not has_sl:
                issues.append("Mungon SL")
            if not has_tp:
                issues.append("Mungon TP")
            report[trade["id"]] = {
                "instrument": trade["instrument"],
                "has_sl": has_sl,
                "has_tp": has_tp,
                "issues": issues,
            }
        return report
