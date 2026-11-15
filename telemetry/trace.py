"""P60 forensic chain: signal -> risk -> request -> result -> fill -> close.

Reads Category-3 journal, Category-4 lifecycle and Category-6 event CSV.
Never reveals raw payloads, credentials, access tokens or free-text errors.
An event without a corresponding durable identity cannot certify execution.
"""
from __future__ import annotations

from hashlib import sha256
from decimal import Decimal, InvalidOperation
import json
import re

from telemetry.events import AuditEvents, AuditError
from storage.intent_journal import IntentJournal, JournalError
from storage.lifecycle_state import LifecycleStore, StateError
from risk.trade_lifecycle import TradeRecord


class TraceError(RuntimeError):
    pass


def _hash_order(request):
    return sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TradeTrace:
    def __init__(self, account_id, *, events=None, journal=None, state=None):
        self.events = events or AuditEvents(account_id)
        self.journal = journal or IntentJournal(account_id)
        self.state = state or LifecycleStore(account_id)
        if self.events.account_ref != sha256(account_id.encode()).hexdigest()[:20]:
            raise TraceError("Different account audit ledger")
        if self.journal.account_id != account_id or self.state.account_id != account_id:
            raise TraceError("Different account journal or lifecycle")

    def attempt(self, attempt_id):
        try:
            attempt = self.journal.get(attempt_id)
            if _hash_order(attempt["request"]) != attempt["request_hash"]:
                raise TraceError("Broker order intent hash mismatch")
            rows = [
                row for row in self.events.read()
                if row["attempt_id"] == attempt_id
                or (not row["attempt_id"]
                    and row["signal_id"] == attempt["signal_id"])
            ]
            if any(row["signal_id"] and row["signal_id"] != attempt["signal_id"]
                   for row in rows):
                raise TraceError("Event/intent signal provenance conflict")
            lifecycle = self.state.load()
        except (AuditError, JournalError, StateError, TypeError, ValueError, KeyError) as exc:
            raise TraceError("Untrustworthy trade provenance") from exc
        trade_id = attempt.get("trade_id")
        for field in ("order_id", "trade_id"):
            value = attempt.get(field)
            if value is not None and (
                not isinstance(value, str) or not value.isascii()
                or not value.isdecimal() or len(value) > 50
            ):
                raise TraceError("Untrusted broker identifier")
        if not isinstance(attempt.get("signal_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", attempt["signal_id"]
        ):
            raise TraceError("Untrusted signal identity")
        order = attempt["request"]["order"]
        if (not isinstance(order, dict)
            or not isinstance(order.get("instrument"), str)
            or not re.fullmatch(
                r"[A-Z0-9]{3,8}_[A-Z0-9]{3,8}", order["instrument"]
            )):
            raise TraceError("Unsafe requested instrument")
        try:
            for value in (
                order["units"], attempt["risk_pct"],
                *(attempt.get(x) for x in ("fill_price", "filled_units")
                  if attempt.get(x) is not None)
            ):
                numeric = Decimal(str(value))
                if not numeric.is_finite():
                    raise ValueError("Nonfinite plan amount")
            if order.get("priceBound") is not None:
                bound = Decimal(str(order["priceBound"]))
                if not bound.is_finite() or bound <= 0:
                    raise ValueError("Bad price bound")
        except (TypeError, KeyError, InvalidOperation, ValueError) as exc:
            raise TraceError("Unverified requested numeric values") from exc
        record = None
        if lifecycle is not None and trade_id in lifecycle["trades"]:
            record = TradeRecord.from_dict(lifecycle["trades"][trade_id])
            if record.attempt_id != attempt_id:
                raise TraceError("Trade ownership mismatch")
        if attempt["status"] == "FILLED" and trade_id is None:
            phase = "BROKER_FILL_WITHOUT_NEW_TRADE"
        elif attempt["status"] == "FILLED":
            phase = "TRADE_VERIFIED" if attempt.get("protection_verified") else "FILL_UNPROTECTED"
        else:
            phase = attempt["status"]
        if record is not None and record.closed:
            if not record.result_counted or trade_id not in lifecycle["closed_results"]:
                raise TraceError("Closed trade missing confirmed cost/accounting result")
            phase = "CLOSED_CONFIRMED"
        # Only safe, explicitly selected fields may leave this view.
        return {
            "attempt_id": attempt_id,
            "signal_id": attempt["signal_id"],
            "grade": attempt.get("grade"),
            "risk_pct": attempt.get("risk_pct"),
            "candle_times": sorted({r["candle_time"] for r in rows if r["candle_time"]}),
            "requested_instrument": order["instrument"],
            "requested_units": order["units"],
            "requested_price_bound": order.get("priceBound"),
            "broker_order_id": attempt.get("order_id"),
            "broker_trade_id": trade_id,
            "broker_fill_price": attempt.get("fill_price"),
            "broker_fill_units": attempt.get("filled_units"),
            "broker_request_id": (
                attempt.get("request_id")
                if isinstance(attempt.get("request_id"), str)
                and re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,128}", attempt["request_id"]
                ) else None
            ),
            "state": phase,
            "broker_protection_verified": bool(attempt.get("protection_verified")),
            "closing_transaction_ids": (
                list(record.close_transaction_ids) if record is not None else []
            ),
            "closed_confirmed": bool(record is not None and record.closed),
            "cycle_ids": sorted({r["cycle_id"] for r in rows if r["cycle_id"]}),
            "event_ids": [r["event_id"] for r in rows],
            "event_codes": [r["reason_code"] for r in rows if r["reason_code"]],
        }
