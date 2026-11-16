"""Strict, read-only OANDA REST v20 contract helpers.

No HTTP calls, broker writes or credential access in this module. The older
Price.status field is deprecated by OANDA; an explicit Price.tradeable boolean
takes precedence. Missing/contradictory transaction evidence always blocks
cursor advancement. Transaction IDs need not be consecutive.
"""
from __future__ import annotations

import re


class OandaContractError(ValueError):
    """Broker payload cannot establish the required v20 facts."""


_TRANSACTION_ID = re.compile(r"[0-9]{1,50}\Z", re.ASCII)
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9-]{1,128}\Z", re.ASCII)


def transaction_id(value) -> bool:
    return isinstance(value, str) and _TRANSACTION_ID.fullmatch(value) is not None


def account_id(value) -> bool:
    return isinstance(value, str) and _ACCOUNT_ID.fullmatch(value) is not None


def price_tradeable(price: object) -> bool:
    """Prefer current ClientPrice.tradeable; legacy status only as fallback.

    A deprecated 'status' must not override an explicitly reported boolean.
    An invalid non-boolean tradeable value fails closed instead of silently
    falling back to status.
    """
    if not isinstance(price, dict):
        return False
    if "tradeable" in price:
        return price["tradeable"] is True
    return price.get("status") == "tradeable"


def validate_sinceid_batch(payload: object, *, account: str,
                           previous_cursor: str, target_cursor: str) -> tuple[dict, ...]:
    """Validate GET /transactions/sinceid against one account snapshot.

    OANDA IDs increase but may have gaps, so the last ID must equal the
    already-read account cursor; sequential (+1) increments are NOT required.
    Transaction.accountID must match. Broker account changes between GETs,
    duplicate/out-of-order items, truncated ranges and empty advanced ranges
    all block the caller before any durable cursor commit.
    """
    if (not account_id(account) or not transaction_id(previous_cursor)
        or not transaction_id(target_cursor)
        or int(target_cursor) <= int(previous_cursor)):
        raise OandaContractError("Invalid sinceid account or cursor range")
    if not isinstance(payload, dict):
        raise OandaContractError("Transaction payload is not a JSON object")
    if payload.get("lastTransactionID") != target_cursor:
        raise OandaContractError("Broker cursor moved during reconciliation")
    txs = payload.get("transactions")
    if not isinstance(txs, list) or not txs:
        raise OandaContractError("Broker returned empty/incomplete sinceid batch")
    previous = int(previous_cursor)
    seen = set()
    for tx in txs:
        if not isinstance(tx, dict):
            raise OandaContractError("Non-object transaction in sinceid range")
        tid = tx.get("id")
        if (not transaction_id(tid)
            or tx.get("accountID") != account
            or not isinstance(tx.get("type"), str)
            or not tx["type"]):
            raise OandaContractError("Transaction ID/account/type unverified")
        n = int(tid)
        if tid in seen or n <= previous or n > int(target_cursor):
            raise OandaContractError("Duplicate, out-of-order or invalid transaction")
        seen.add(tid)
        previous = n
    if previous != int(target_cursor):
        raise OandaContractError("Transaction batch does not reach account cursor")
    return tuple(txs)
