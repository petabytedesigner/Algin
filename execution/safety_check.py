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


    @staticmethod
    def audit_raw_trades(trades, journal_entries):
        """P29/P30 read-only gate: distinguish owned from foreign/manual trades.

        An Algin tag alone is insufficient: clientExtensions.id must match a
        durable FILLED record for this account and the same trade ID.
        """
        if not isinstance(trades, list) or not isinstance(journal_entries, (list, tuple)):
            return {"__broker_error__": {"issues": ["Unverified trade inventory"]}}
        known = {
            entry["attempt_id"]: entry
            for entry in journal_entries
            if isinstance(entry, dict) and entry.get("status") == "FILLED"
            and isinstance(entry.get("attempt_id"), str)
        }
        report = {}
        for trade in trades:
            if not isinstance(trade, dict):
                return {"__broker_error__": {"issues": ["Malformed trade object"]}}
            trade_id = trade.get("id")
            if not isinstance(trade_id, str) or not trade_id or trade_id in report:
                return {"__broker_error__": {"issues": ["Missing or duplicate trade ID"]}}
            client = trade.get("clientExtensions")
            intent_id = client.get("id") if isinstance(client, dict) else None
            saved = known.get(intent_id)
            owned = (
                saved is not None
                and saved.get("trade_id") == trade_id
                and client.get("tag") == "algin"
            )
            issues = []
            if not owned:
                issues.append("Manual/foreign/unattributed trade")
            if not isinstance(trade.get("stopLossOrder"), dict):
                issues.append("Unverified/missing SL")
            if not isinstance(trade.get("takeProfitOrder"), dict):
                issues.append("Unverified/missing TP")
            if owned and not saved.get("protection_verified"):
                issues.append("Algin trade protection not confirmed")
            if issues:
                report[trade_id] = {"instrument": trade.get("instrument"),
                                    "issues": issues, "owned": owned}
        return report

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
