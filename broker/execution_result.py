"""Strict OANDA mutation outcome classification.

A 201 or 400 HTTP status alone is NOT proof of fill or reject. Only the
correct transaction variant can establish a terminal broker outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional

from risk.trade_plan import number


class Outcome(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExecutionResult:
    outcome: Outcome
    attempt_id: str
    request_id: Optional[str] = None
    order_id: Optional[str] = None
    trade_id: Optional[str] = None
    fill_price: Optional[Decimal] = None
    filled_units: Optional[Decimal] = None
    trade_reduced: Optional[dict] = None
    trades_closed: tuple[dict, ...] = ()
    protection_verified: bool = False
    reason: str = "No authoritative broker evidence"


def _id(value):
    if isinstance(value, str) and value and len(value) <= 128:
        return value
    return None


def _broker_id(value):
    # OANDA REST transaction, trade and order IDs are numeric strings.
    if isinstance(value, str) and value.isascii() and value.isdecimal() and 1 <= len(value) <= 50:
        return value
    return None


def _open_info(fill: dict) -> Optional[str]:
    data = fill.get("tradeOpened")
    if data is None:
        return None
    if not isinstance(data, dict) or not _broker_id(data.get("tradeID")):
        raise ValueError("Malformed tradeOpened; no tradeID certainty")
    number(data["units"], "opened units", zero_ok=False)
    return data["tradeID"]


def classify_response(reply, attempt_id: str) -> ExecutionResult:
    """Explicit terminal states; malformed JSON/errors remain UNKNOWN."""
    request_id = _id(getattr(reply, "request_id", None))
    status = getattr(reply, "status_code", None)
    body = getattr(reply, "body", None)
    unknown = lambda why: ExecutionResult(
        Outcome.UNKNOWN, attempt_id, request_id=request_id, reason=why
    )
    if not isinstance(body, dict):
        return unknown("Invalid or absent broker JSON")
    tx_fill = body.get("orderFillTransaction")
    tx_cancel = body.get("orderCancelTransaction")
    tx_reject = body.get("orderRejectTransaction")
    if sum(x is not None for x in (tx_fill, tx_cancel, tx_reject)) > 1:
        return unknown("Conflicting broker transaction outcomes")
    if status in (200, 201) and tx_fill is not None:
        if not isinstance(tx_fill, dict):
            return unknown("Malformed orderFillTransaction")
        try:
            if tx_fill.get("type") != "ORDER_FILL" or not _broker_id(tx_fill.get("id")):
                return unknown("Missing/invalid fill transaction ID or type")
            units = number(tx_fill["units"], "filled units", zero_ok=False)
            trade_id = _open_info(tx_fill)
            reduced = tx_fill.get("tradeReduced")
            closed = tx_fill.get("tradesClosed", [])
            # OANDA deprecated OrderFillTransaction.price. Exact prices now
            # live in tradeOpened/tradeReduced/tradesClosed. Never mistake
            # fullVWAP for a specific trade's execution price; it can differ
            # with guaranteed stop-loss clamping. Legacy price is supported
            # only when the per-trade price is absent.
            opened = tx_fill.get("tradeOpened")
            if opened is not None:
                if "price" in opened:
                    raw_price = opened["price"]
                else:
                    raw_price = tx_fill.get("price")
            elif reduced is not None:
                raw_price = reduced.get("price", tx_fill.get("price"))
            elif isinstance(closed, list) and len(closed) == 1 and isinstance(closed[0], dict):
                raw_price = closed[0].get("price", tx_fill.get("price"))
            else:
                raw_price = tx_fill.get("price")
            price = number(raw_price, "trade execution price", positive=True)
            if reduced is not None:
                if not isinstance(reduced, dict) or not _broker_id(reduced.get("tradeID")):
                    return unknown("Malformed tradeReduced")
                number(reduced["units"], "reduced units", zero_ok=False)
            if not isinstance(closed, list) or not all(
                isinstance(t, dict) and _broker_id(t.get("tradeID")) for t in closed
            ):
                return unknown("Malformed tradesClosed")
            for t in closed:
                number(t["units"], "closed units", zero_ok=False)
            if trade_id is None and reduced is None and not closed:
                return unknown("Fill did not describe opened/reduced/closed trade")
            create = body.get("orderCreateTransaction")
            if create is not None and not isinstance(create, dict):
                return unknown("Invalid orderCreateTransaction")
            create_id = create.get("id") if create else None
            order_id = tx_fill.get("orderID") or create_id
            if not _broker_id(order_id) or (create_id and create_id != order_id):
                return unknown("Unreliable order identity")
            return ExecutionResult(
                Outcome.FILLED, attempt_id, request_id, order_id, trade_id,
                price, units, reduced, tuple(closed),
                protection_verified=False,
                reason="Broker orderFillTransaction verified; protection pending",
            )
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            return unknown("Malformed fill details")
    if status in (200, 201) and tx_cancel is not None:
        if (not isinstance(tx_cancel, dict)
            or tx_cancel.get("type") != "ORDER_CANCEL"
            or not _broker_id(tx_cancel.get("id"))):
            return unknown("Invalid cancel transaction")
        order_id = tx_cancel.get("orderID")
        if not _broker_id(order_id):
            return unknown("Cancellation lacks valid broker order ID")
        return ExecutionResult(
            Outcome.CANCELLED, attempt_id, request_id,
            order_id=_broker_id(order_id), reason="Order cancellation transaction verified"
        )
    if status in (400, 404) and tx_reject is not None:
        if (not isinstance(tx_reject, dict)
            or not _broker_id(tx_reject.get("id"))
            or tx_reject.get("type") not in (
                "MARKET_ORDER_REJECT", "ORDER_REJECT"
            )):
            return unknown("Unverified broker order rejection")
        return ExecutionResult(
            Outcome.REJECTED, attempt_id, request_id,
            reason="Order-reject transaction verified"
        )
    if status == 429:
        return unknown("HTTP 429 rate limited; execution state unresolved")
    if isinstance(status, int) and status >= 500:
        return unknown("HTTP 5xx; execution state unresolved")
    return unknown(f"HTTP {status} without authoritative broker transaction")
