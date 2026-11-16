"""OANDA v20 transaction-provenance financial ledger (read-only).

The ledger records account-home-currency monetary components from immutable
transactions. It never assumes an omitted fee is zero, and never subtracts
halfSpreadCost a second time: that is an execution-cost diagnostic already
reflected in filled prices. Cash transfers are not trading P/L.

A ledger starts strictly after its baseline cursor. It does NOT certify earlier
account or trade history, and incomplete/unallocated events do not yield a
certified per-trade net result.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


class FinancialEvidenceError(ValueError):
    """Malformed, inconsistent or insufficient transaction financial evidence."""


_ID = re.compile(r"^[0-9]{1,50}$")
_FIELDS = ("realized_pl", "financing", "commission", "guaranteed_fee",
           "dividend_adjustment", "half_spread_cost")
_CASH_FIELDS = _FIELDS[:5]
_FINANCIAL = {"ORDER_FILL", "DAILY_FINANCING", "DIVIDEND_ADJUSTMENT"}
_NONFINANCIAL = {
    "CREATE", "CLOSE", "REOPEN", "CLIENT_CONFIGURE",
    "CLIENT_CONFIGURE_REJECT", "MARKET_ORDER", "MARKET_ORDER_REJECT",
    "LIMIT_ORDER", "LIMIT_ORDER_REJECT", "STOP_ORDER",
    "STOP_ORDER_REJECT", "TAKE_PROFIT_ORDER", "TAKE_PROFIT_ORDER_REJECT",
    "STOP_LOSS_ORDER", "STOP_LOSS_ORDER_REJECT",
    "GUARANTEED_STOP_LOSS_ORDER", "GUARANTEED_STOP_LOSS_ORDER_REJECT",
    "TRAILING_STOP_LOSS_ORDER", "TRAILING_STOP_LOSS_ORDER_REJECT",
    "ORDER_CANCEL", "ORDER_CANCEL_REJECT", "ORDER_CLIENT_EXTENSIONS_MODIFY",
    "ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT", "TRADE_CLIENT_EXTENSIONS_MODIFY",
    "TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT", "MARGIN_CALL_ENTER",
    "MARGIN_CALL_EXTEND", "MARGIN_CALL_EXIT", "DELAYED_TRADE_CLOSURE",
    "RESET_RESETTABLE_PL", "FIXED_PRICE_ORDER", "MARKET_IF_TOUCHED_ORDER",
    "MARKET_IF_TOUCHED_ORDER_REJECT", "FIXED_PRICE_ORDER_REJECT",
}


def _id(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FinancialEvidenceError("Invalid " + name)
    return value


def _money(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise FinancialEvidenceError("Invalid " + name)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinancialEvidenceError("Non-decimal " + name) from exc
    if (not amount.is_finite()
        or (nonnegative and amount < 0)
        or abs(amount.adjusted()) > 50):
        raise FinancialEvidenceError("Nonfinite or unreasonable " + name)
    return amount


def _text(value, name, *, nonnegative=False):
    return str(_money(value, name, nonnegative=nonnegative))


def _optional(obj, key, *, nonnegative=False):
    if key not in obj:
        return None
    return _text(obj[key], key, nonnegative=nonnegative)


def _zero():
    return {k: None for k in _FIELDS}


def _net(parts):
    if any(parts[k] is None for k in _CASH_FIELDS):
        return None
    return str(
        _money(parts["realized_pl"], "realized_pl")
        + _money(parts["financing"], "financing")
        - _money(parts["commission"], "commission", nonnegative=True)
        - _money(parts["guaranteed_fee"], "guaranteed_fee", nonnegative=True)
        + _money(parts["dividend_adjustment"], "dividend_adjustment")
    )


def initial_ledger(cursor):
    return {"baseline_cursor": _id(cursor, "baseline cursor"), "events": {}}


def _details(items, kind):
    if not isinstance(items, list):
        raise FinancialEvidenceError("Malformed " + kind + " effects")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise FinancialEvidenceError("Non-object trade effect")
        tid = _id(item.get("tradeID"), "trade ID")
        part = _zero()
        if kind == "OPEN":
            # Opening an order has zero realized P/L and financing.
            part["realized_pl"] = "0"
            part["financing"] = "0"
            part["guaranteed_fee"] = _optional(
                item, "guaranteedExecutionFee", nonnegative=True
            )
            part["half_spread_cost"] = _optional(item, "halfSpreadCost")
        elif kind in ("REDUCE", "CLOSE"):
            part["realized_pl"] = _optional(item, "realizedPL")
            part["financing"] = _optional(item, "financing")
            part["guaranteed_fee"] = _optional(
                item, "guaranteedExecutionFee", nonnegative=True
            )
            part["half_spread_cost"] = _optional(item, "halfSpreadCost")
        elif kind == "FINANCING":
            part["financing"] = _optional(item, "financing")
        elif kind == "DIVIDEND":
            part["dividend_adjustment"] = _optional(item, "dividendAdjustment")
        else:
            raise FinancialEvidenceError("Unrecognized trade effect")
        result.append({"trade_id": tid, "kind": kind, "components": part})
    return result


def _order_fill(tx, parts):
    parts.update({
        "realized_pl": _optional(tx, "pl"),
        "financing": _optional(tx, "financing"),
        "commission": _optional(tx, "commission", nonnegative=True),
        "guaranteed_fee": _optional(
            tx, "guaranteedExecutionFee", nonnegative=True
        ),
        "dividend_adjustment": "0",
        "half_spread_cost": _optional(tx, "halfSpreadCost"),
    })
    effects = []
    opened = tx.get("tradeOpened")
    if opened is not None:
        effects.extend(_details([opened], "OPEN"))
    reduced = tx.get("tradeReduced")
    if reduced is not None:
        effects.extend(_details([reduced], "REDUCE"))
    effects.extend(_details(tx.get("tradesClosed", []), "CLOSE"))
    if len({effect["trade_id"] for effect in effects}) != len(effects):
        raise FinancialEvidenceError("Duplicate trade in one ORDER_FILL")
    # Account pl is the total of the trade reduction/closure realized P/L.
    reductions = [effect for effect in effects if effect["kind"] != "OPEN"]
    if parts["realized_pl"] is not None and all(
        effect["components"]["realized_pl"] is not None for effect in reductions
    ):
        if _money(parts["realized_pl"], "aggregate pl") != sum(
            (_money(e["components"]["realized_pl"], "trade pl") for e in reductions),
            Decimal("0")
        ):
            raise FinancialEvidenceError("ORDER_FILL aggregate/trade P/L mismatch")
    if parts["financing"] is not None and all(
        e["components"]["financing"] is not None for e in effects
    ):
        if _money(parts["financing"], "aggregate financing") != sum(
            (_money(e["components"]["financing"], "trade financing") for e in effects),
            Decimal("0")
        ):
            raise FinancialEvidenceError("ORDER_FILL financing mismatch")
    for field, label in (
        ("guaranteed_fee", "guaranteed execution fee"),
        ("half_spread_cost", "half-spread cost"),
    ):
        if parts[field] is not None and all(
            e["components"][field] is not None for e in effects
        ):
            if _money(parts[field], label) != sum(
                (_money(e["components"][field], label) for e in effects),
                Decimal("0")
            ):
                raise FinancialEvidenceError("ORDER_FILL " + label + " mismatch")
    return effects


def _daily_financing(tx, parts):
    parts.update({"realized_pl": "0",
                  "financing": _optional(tx, "financing"),
                  "commission": "0", "guaranteed_fee": "0",
                  "dividend_adjustment": "0"})
    positions = tx.get("positionFinancings")
    if positions is None:
        return []
    if not isinstance(positions, list):
        raise FinancialEvidenceError("Malformed positionFinancings")
    effects = []
    for position in positions:
        if not isinstance(position, dict):
            raise FinancialEvidenceError("Malformed positionFinancing")
        effects.extend(_details(position.get("openTradeFinancings", []), "FINANCING"))
        if "financing" in position and all(
            e["components"]["financing"] is not None
            for e in effects
        ) and position.get("openTradeFinancings") is not None:
            current = _details(position["openTradeFinancings"], "FINANCING")
            if _money(position["financing"], "position financing") != sum(
                (_money(x["components"]["financing"], "trade financing")
                 for x in current), Decimal("0")
            ):
                raise FinancialEvidenceError("Position financing mismatch")
    if parts["financing"] is not None and all(
        e["components"]["financing"] is not None for e in effects
    ) and all("openTradeFinancings" in p for p in positions):
        if _money(parts["financing"], "daily financing") != sum(
            (_money(e["components"]["financing"], "trade financing") for e in effects),
            Decimal("0")
        ):
            raise FinancialEvidenceError("Daily financing aggregation mismatch")
    return effects


def _dividend(tx, parts):
    parts.update({"realized_pl": "0", "financing": "0",
                  "commission": "0", "guaranteed_fee": "0",
                  "dividend_adjustment": _optional(tx, "dividendAdjustment")})
    children = tx.get("openTradeDividendAdjustments")
    if children is None:
        return []
    effects = _details(children, "DIVIDEND")
    if parts["dividend_adjustment"] is not None and all(
        e["components"]["dividend_adjustment"] is not None for e in effects
    ):
        if _money(parts["dividend_adjustment"], "account dividend") != sum(
            (_money(e["components"]["dividend_adjustment"], "trade dividend")
             for e in effects), Decimal("0")
        ):
            raise FinancialEvidenceError("Dividend aggregate/trade mismatch")
    return effects


def parse_transaction(tx):
    """Normalize financial facts without inventing absent broker amounts."""
    if not isinstance(tx, dict):
        raise FinancialEvidenceError("Invalid transaction")
    _id(tx.get("id"), "transaction ID")
    kind = tx.get("type")
    if not isinstance(kind, str) or not kind:
        raise FinancialEvidenceError("Transaction type missing")
    parts = _zero()
    effects = []
    funding = None
    if kind == "ORDER_FILL":
        effects = _order_fill(tx, parts)
    elif kind == "DAILY_FINANCING":
        effects = _daily_financing(tx, parts)
    elif kind == "DIVIDEND_ADJUSTMENT":
        effects = _dividend(tx, parts)
    elif kind == "TRANSFER_FUNDS":
        funding = _optional(tx, "amount")
    elif kind in _NONFINANCIAL:
        parts.update({key: "0" for key in _CASH_FIELDS})
    else:
        # Unknown future transaction types may carry money: never make up zero.
        pass
    if kind not in _FINANCIAL | {"TRANSFER_FUNDS"} and kind in _NONFINANCIAL:
        for key in ("pl", "financing", "commission", "guaranteedExecutionFee",
                    "dividendAdjustment"):
            if key in tx and _money(tx[key], key) != 0:
                raise FinancialEvidenceError("Unexpected monetary nonfinancial transaction")
    cash_net = None if kind == "TRANSFER_FUNDS" else _net(parts)
    # A complete account total is NOT automatically a complete trade
    # attribution. Children must account for the source's aggregate and
    # fees must be uniquely attributable, otherwise trade net is unknown.
    allocation_complete = True
    if kind == "ORDER_FILL":
        allocation_complete = (
            cash_net is not None
            and (len(effects) == 1 or (
                parts["commission"] == "0" and parts["guaranteed_fee"] == "0"
            ))
            and all(
                x["components"]["realized_pl"] is not None
                and x["components"]["financing"] is not None
                for x in effects
            )
        )
    elif kind in {"DAILY_FINANCING", "DIVIDEND_ADJUSTMENT"}:
        detail_key = (
            "positionFinancings" if kind == "DAILY_FINANCING"
            else "openTradeDividendAdjustments"
        )
        value_key = (
            "financing" if kind == "DAILY_FINANCING"
            else "dividend_adjustment"
        )
        allocation_complete = (
            cash_net is not None and detail_key in tx
            and all(
                x["components"][value_key] is not None for x in effects
            )
            and sum(
                (_money(x["components"][value_key], value_key)
                 for x in effects), Decimal("0")
            ) == _money(parts[value_key], value_key)
        )
    elif kind not in _NONFINANCIAL | {"TRANSFER_FUNDS"}:
        allocation_complete = False
    return {
        "type": kind,
        "components": parts,
        "account_net": cash_net,
        "funds_transfer": funding,
        "trade_effects": effects,
        "allocation_complete": allocation_complete,
    }


def record_transaction(ledger, tx):
    """One transaction per identity; the caller atomically persists with cursor."""
    event_id = _id(tx.get("id"), "transaction ID")
    if event_id in ledger["events"]:
        raise FinancialEvidenceError("Duplicate financial transaction evidence")
    if int(event_id) <= int(ledger["baseline_cursor"]):
        raise FinancialEvidenceError("Financial transaction predates baseline")
    ledger["events"][event_id] = parse_transaction(tx)


def validate_ledger(ledger, *, cursor, processed):
    """Validate schema and re-derived totals when loading a checksummed state."""
    if not isinstance(ledger, dict) or set(ledger) != {"baseline_cursor", "events"}:
        raise FinancialEvidenceError("Invalid financial ledger schema")
    base = _id(ledger["baseline_cursor"], "baseline cursor")
    _id(cursor, "cursor")
    if int(base) > int(cursor) or not isinstance(ledger["events"], dict):
        raise FinancialEvidenceError("Ledger baseline/cursor disagreement")
    if not isinstance(processed, (list, tuple)):
        raise FinancialEvidenceError("Invalid processed transaction history")
    seen = set(processed)
    for tid, event in ledger["events"].items():
        _id(tid, "event ID")
        if int(tid) <= int(base) or int(tid) > int(cursor) or tid not in seen:
            raise FinancialEvidenceError("Financial event uncommitted/out of cursor range")
        if not isinstance(event, dict) or set(event) != {
            "type", "components", "account_net", "funds_transfer",
            "trade_effects", "allocation_complete"
        }:
            raise FinancialEvidenceError("Malformed financial event")
        kind = event["type"]
        if (not isinstance(kind, str) or not kind
            or type(event["allocation_complete"]) is not bool):
            raise FinancialEvidenceError("Invalid financial event kind/allocation")
        parts = event["components"]
        if not isinstance(parts, dict) or set(parts) != set(_FIELDS):
            raise FinancialEvidenceError("Financial components missing")
        for field, amount in parts.items():
            if amount is not None:
                _money(amount, field, nonnegative=field in (
                    "commission", "guaranteed_fee"
                ))
        expected = None if kind == "TRANSFER_FUNDS" else _net(parts)
        if event["account_net"] != expected:
            raise FinancialEvidenceError("Ledger net does not match components")
        transfer = event["funds_transfer"]
        if transfer is not None:
            _money(transfer, "funds transfer")
        if kind != "TRANSFER_FUNDS" and transfer is not None:
            raise FinancialEvidenceError("Funds transfer mixed into trading result")
        if kind == "TRANSFER_FUNDS" and (
            any(parts[k] is not None for k in _FIELDS)
            or event["allocation_complete"] is not True
        ):
            raise FinancialEvidenceError("Transfer funds cannot be trading P/L")
        if kind not in _FINANCIAL | _NONFINANCIAL | {"TRANSFER_FUNDS"} and (
            event["account_net"] is not None or event["allocation_complete"]
        ):
            raise FinancialEvidenceError("Unknown transaction claimed complete")
        effects = event["trade_effects"]
        if not isinstance(effects, list):
            raise FinancialEvidenceError("Malformed trade allocation")
        for effect in effects:
            if not isinstance(effect, dict) or set(effect) != {
                "trade_id", "kind", "components"
            } or effect["kind"] not in (
                "OPEN", "REDUCE", "CLOSE", "FINANCING", "DIVIDEND"
            ):
                raise FinancialEvidenceError("Invalid trade allocation")
            _id(effect["trade_id"], "trade ID")
            trade_parts = effect["components"]
            if not isinstance(trade_parts, dict) or set(trade_parts) != set(_FIELDS):
                raise FinancialEvidenceError("Invalid trade component schema")
            for field, amount in trade_parts.items():
                if amount is not None:
                    _money(amount, field, nonnegative=field in (
                        "commission", "guaranteed_fee"
                    ))
    return ledger


def summary(ledger):
    """Account-level *period* P/L; unavailable if any transaction is incomplete.

    Never presents these period figures as lifetime NAV or as attributed P/L.
    """
    if not isinstance(ledger, dict):
        raise FinancialEvidenceError("Ledger required")
    amounts = []
    incomplete = []
    transfers = []
    for tid, event in sorted(ledger["events"].items(), key=lambda x: int(x[0])):
        if event["type"] == "TRANSFER_FUNDS":
            transfers.append(tid)
        elif event["account_net"] is None:
            incomplete.append(tid)
        else:
            amounts.append(_money(event["account_net"], "account net"))
    return {
        "since_cursor": ledger["baseline_cursor"],
        "as_of_event_count": len(ledger["events"]),
        "complete_since_cursor": not incomplete,
        "net_pl_home_since_cursor": (
            str(sum(amounts, Decimal("0"))) if not incomplete else None
        ),
        "unverified_transaction_ids": tuple(incomplete),
        "excluded_funds_transfer_ids": tuple(transfers),
        "historical_account_pl_complete": False,
    }


def trade_summary(ledger, trade_id):
    """Return complete trade net ONLY with proven opening+closing provenance.

    A transaction's commission cannot safely be apportioned among multiple
    trades; such allocation is refused rather than guessed.
    """
    trade_id = _id(trade_id, "trade ID")
    seen_open = seen_closed = False
    total = Decimal("0")
    incomplete = []
    related = []
    for tid, event in sorted(ledger["events"].items(), key=lambda x: int(x[0])):
        matching = [x for x in event["trade_effects"] if x["trade_id"] == trade_id]
        if (event["type"] in _FINANCIAL
            and not event["allocation_complete"]):
            incomplete.append(tid)
        if not matching:
            # A missing source allocation (including an unknown future
            # broker type) may belong to this trade. Block rather than guess.
            if (event["type"] in _FINANCIAL
                and not event["allocation_complete"]
                or event["type"] not in (
                    _FINANCIAL | _NONFINANCIAL | {"TRANSFER_FUNDS"}
                )):
                if tid not in incomplete:
                    incomplete.append(tid)
            continue
        related.append(tid)
        if any(x["kind"] == "OPEN" for x in matching):
            seen_open = True
        if any(x["kind"] == "CLOSE" for x in matching):
            seen_closed = True
        if (event["account_net"] is None
            or (event["type"] == "ORDER_FILL"
                and len(event["trade_effects"]) != 1
                and (event["components"]["commission"] != "0"
                     or event["components"]["guaranteed_fee"] != "0"))):
            if tid not in incomplete:
                incomplete.append(tid)
            continue
        effect = matching[0]
        part = effect["components"]
        if any(part[k] is None for k in (
            ("realized_pl", "financing") if event["type"] == "ORDER_FILL"
            else ("financing",) if event["type"] == "DAILY_FINANCING"
            else ("dividend_adjustment",)
        )):
            incomplete.append(tid)
            continue
        amount = (
            _money(part["realized_pl"], "trade realized") +
            _money(part["financing"], "trade financing")
            if event["type"] == "ORDER_FILL"
            else _money(part["financing"], "trade financing")
            if event["type"] == "DAILY_FINANCING"
            else _money(part["dividend_adjustment"], "trade dividend")
        )
        if event["type"] == "ORDER_FILL" and len(event["trade_effects"]) == 1:
            amount -= _money(event["components"]["commission"], "commission")
            amount -= _money(event["components"]["guaranteed_fee"], "guaranteed fee")
        total += amount
    complete = seen_open and seen_closed and not incomplete
    return {
        "trade_id": trade_id,
        "since_cursor": ledger["baseline_cursor"],
        "net_pl_home": str(total) if complete else None,
        "complete": complete,
        "related_transaction_ids": tuple(related),
        "unverified_transaction_ids": tuple(incomplete),
    }
