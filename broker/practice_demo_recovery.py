"""One guarded full close of the 10 EUR/USD demo units remaining after tx 10.

The previous attempt partially reduced trade 6 successfully. This module
NEVER repeats the entry, partial reduction or an ambiguous close.
"""
from __future__ import annotations
from decimal import Decimal
from pathlib import Path
import json
import os
import sys

from broker.practice_demo_adapter import DemoAdapter, DemoBlocked, ACCOUNT, ATTEMPT
from risk.trade_plan import number

EXPECTED_TRADE = "6"
EXPECTED_PARTIAL_TX = "10"
EXPECTED_REMAINING = Decimal("10")
ARM = "C12_RECOVER_CONFIRMED_PARTIAL_10_UNITS"


def recover(adapter):
    if (adapter.evidence.exists()
        or os.environ.get("ALGIN_PRACTICE_RECOVERY_CONFIRM") != ARM
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise DemoBlocked("FRESH_RECOVERY_AUTHORIZATION_REQUIRED")
    account_data = adapter.get("")
    a = account_data.get("account")
    if not isinstance(a, dict) or a.get("id") != ACCOUNT or a.get("currency") != "USD":
        raise DemoBlocked("RECOVERY_ACCOUNT_MISMATCH")
    # A broker-confirmed flat account means another protective order may
    # already have closed it. Never send a redundant close request.
    if (a.get("trades") == [] and a.get("openTradeCount") == 0):
        adapter.log("RECOVERY_ALREADY_FLAT_NO_WRITE")
        return "ALREADY_FLAT"
    trades = a.get("trades")
    if (not isinstance(trades, list) or len(trades) != 1
        or trades[0].get("id") != EXPECTED_TRADE
        or number(trades[0].get("currentUnits"), "remaining units")
            != EXPECTED_REMAINING
        or a.get("openTradeCount") != 1
        or a.get("pendingOrderCount") != 2
        or account_data.get("lastTransactionID") != EXPECTED_PARTIAL_TX):
        raise DemoBlocked("REMAINING_TRADE_OR_CURSOR_NOT_EXACTLY_VERIFIED")
    t = adapter.get("/trades/" + EXPECTED_TRADE).get("trade")
    if (not isinstance(t, dict)
        or t.get("id") != EXPECTED_TRADE
        or t.get("state") != "OPEN"
        or t.get("instrument") != "EUR_USD"
        or (t.get("clientExtensions") or {}).get("id") != ATTEMPT
        or number(t.get("currentUnits"), "remaining units") != EXPECTED_REMAINING
        or not isinstance(t.get("stopLossOrder"), dict)
        or not isinstance(t.get("takeProfitOrder"), dict)
        or number(t["stopLossOrder"].get("price"), "SL", positive=True) <= 0
        or number(t["takeProfitOrder"].get("price"), "TP", positive=True) <= 0):
        raise DemoBlocked("OWNED_TRADE_OR_SL_TP_NOT_VERIFIED")
    tx = adapter.transaction(EXPECTED_PARTIAL_TX, "ORDER_FILL")
    reduced = tx.get("tradeReduced")
    if (tx.get("reason") != "MARKET_ORDER_TRADE_CLOSE"
        or not isinstance(reduced, dict)
        or reduced.get("tradeID") != EXPECTED_TRADE
        or number(reduced.get("units"), "partial fill") != Decimal("-10")):
        raise DemoBlocked("PARTIAL_TRANSACTION_PROVENANCE_MISMATCH")
    adapter.log("RECOVERY_PREFLIGHT_PROVEN",
                trade_id=EXPECTED_TRADE, remaining_units="10",
                partial_transaction=EXPECTED_PARTIAL_TX)
    answer = adapter.send(
        "PUT", "/trades/" + EXPECTED_TRADE + "/close",
        {"units": "ALL"}, operation="RECOVERY_FULL_CLOSE",
    )
    fill = (answer.body or {}).get("orderFillTransaction")
    closed = fill.get("tradesClosed") if isinstance(fill, dict) else None
    if (answer.status_code != 200
        or not isinstance(closed, list) or len(closed) != 1
        or closed[0].get("tradeID") != EXPECTED_TRADE
        or abs(number(closed[0].get("units"), "closed units"))
            != EXPECTED_REMAINING):
        raise DemoBlocked("FULL_CLOSE_RESPONSE_AMBIGUOUS_NO_RETRY")
    adapter.transaction(fill.get("id"), "ORDER_FILL")
    final = adapter.get("").get("account")
    if (not isinstance(final, dict)
        or final.get("id") != ACCOUNT
        or final.get("trades") != []
        or final.get("openTradeCount") != 0
        or final.get("pendingOrderCount") != 0):
        raise DemoBlocked("FULL_CLOSE_INVENTORY_NOT_CONFIRMED")
    adapter.log("PRACTICE_FULL_CLOSE_RECOVERY_CONFIRMED",
                trade_id=EXPECTED_TRADE, close_transaction=fill["id"],
                final_balance=str(final.get("balance")),
                final_nav=str(final.get("NAV")),
                final_open_trades=0, final_pending_orders=0,
                final_close_fill_pl=str(fill.get("pl")),
                final_close_fill_commission=str(fill.get("commission")),
                final_close_fill_financing=str(fill.get("financing")))
    return "CONFIRMED"


def main():
    try:
        recover(DemoAdapter(evidence=Path(".local/category12_recovery.jsonl")))
        return 0
    except DemoBlocked as e:
        print(json.dumps({"status": "PRACTICE_RECOVERY_BLOCKED",
                          "reason": str(e), "no_retry": True}))
        return 2
    except Exception:
        print(json.dumps({"status": "PRACTICE_RECOVERY_BLOCKED",
                          "reason": "UNEXPECTED_ERROR", "no_retry": True}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
