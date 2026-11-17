"""One EUR/USD OANDA Practice technical exercise, 20 units max.

Open with broker-native SL/TP, prove fill via GET, optionally BE only if
market moved favorably, then close 10 units and finally ALL. Never retries
a POST/PUT with unknown outcome. Will not activate production trading.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os
import sys

import config
from broker.account_instruments import parse_account_instruments
from broker.execution_result import classify_response, Outcome
from broker.execution_validation import parse_market_quote
from broker.practice_readonly_probe import PracticeReadOnlyProbe
from broker.practice_demo_adapter import (
    DemoAdapter, DemoBlocked, ACCOUNT, ATTEMPT,
)
from risk.trade_plan import number
from utils.time_tools import utc_now

UNITS = Decimal("20")
HARD_RISK_USD = Decimal("0.25")
ARM = "EXPLICIT_C12_DEMO_ONLY_ONE_SHOT"


def expected_absence(adapter):
    try:
        r = adapter.reader.read("/orders/@" + ATTEMPT)
    except Exception:
        raise DemoBlocked("ORDER_DEDUPLICATION_GET_FAILED") from None
    if getattr(r, "status_code", None) == 200:
        raise DemoBlocked("ONE_SHOT_ORDER_ALREADY_EXISTS")
    data = getattr(r, "body", None)
    if (getattr(r, "status_code", None) != 404
        or not isinstance(data, dict)
        or "order" not in str(data.get("errorMessage", "")).lower()):
        raise DemoBlocked("ORDER_ABSENCE_NOT_PROVEN")


def preflight(adapter, *, verify_network=True):
    adapter.gates()
    if verify_network:
        result = PracticeReadOnlyProbe(expected_account_id=ACCOUNT).run()
        if (result.source != "OANDA_PRACTICE_NETWORK"
            or not result.zero_open_trades
            or not result.zero_pending_orders
            or result.eur_usd_quote_status != "QUOTE_VERIFIED"
            or result.transaction_history_status != "UNCHANGED_CURSOR"
            or result.mt4_associated
            or not result.account_nav_positive):
            raise DemoBlocked("AUTHENTICATED_ACCOUNT_PREFLIGHT_BLOCKED")
    expected_absence(adapter)
    snapshot = adapter.get("")
    account = snapshot.get("account")
    if (not isinstance(account, dict) or account.get("id") != ACCOUNT
        or account.get("currency") != "USD"
        or account.get("trades") != [] or account.get("orders") != []
        or account.get("openTradeCount") != 0
        or account.get("pendingOrderCount") != 0
        or number(account.get("NAV"), "NAV", positive=True) < Decimal("1000")
        or number(account.get("marginAvailable"), "margin") < Decimal("10")):
        raise DemoBlocked("ACCOUNT_CHANGED_OR_MARGIN_UNVERIFIED")
    catalog = parse_account_instruments(adapter.get("/instruments"))
    rules = catalog.get("EUR_USD")
    if rules is None or rules.kind != "CURRENCY":
        raise DemoBlocked("EURUSD_NOT_ACCOUNT_AUTHORIZED")
    if (rules.minimum_trade_size > UNITS
        or rules.maximum_order_units < UNITS
        or (rules.maximum_position_size > 0
            and rules.maximum_position_size < UNITS)
        or UNITS != UNITS.quantize(rules.unit_step)):
        raise DemoBlocked("DEMO_SIZE_NOT_PERMITTED")
    quote = parse_market_quote(
        adapter.get("/pricing", params={"instruments": "EUR_USD"}),
        "EUR_USD", rules, now=utc_now(),
        max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        max_spread_pips=Decimal(str(config.MAX_SPREAD_PIPS)),
    )
    pip = rules.pip_size
    stop = (quote.bid - 40*pip).quantize(rules.price_step)
    target = (quote.ask + 140*pip).quantize(rules.price_step)
    bound = (quote.ask + pip).quantize(rules.price_step)
    if (stop <= 0 or stop >= quote.bid - 10*pip
        or target <= quote.ask + 10*pip or bound >= target
        or (bound-stop)*UNITS > HARD_RISK_USD):
        raise DemoBlocked("TECHNICAL_ORDER_WORST_RISK_BLOCKED")
    payload = {"order": {
        "instrument": "EUR_USD", "units": "20",
        "type": "MARKET", "timeInForce": "FOK",
        "positionFill": "OPEN_ONLY", "priceBound": str(bound),
        "stopLossOnFill": {"price": str(stop)},
        "takeProfitOnFill": {"price": str(target)},
        "clientExtensions": {
            "id": ATTEMPT, "tag": "algin", "comment": "c12_demo",
        },
        "tradeClientExtensions": {
            "id": ATTEMPT, "tag": "algin", "comment": "c12_demo",
        },
    }}
    return payload, rules, quote, stop, target, bound


def exercise(adapter, *, verify_network=True):
    if (adapter.evidence.exists()
        or os.environ.get("ALGIN_PRACTICE_ONE_SHOT_CONFIRM") != ARM
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise DemoBlocked("NO_FRESH_SINGLE_RUN_AUTHORIZATION")
    payload, rules, quote, stop, target, bound = preflight(
        adapter, verify_network=verify_network
    )
    adapter.log(
        "PREFLIGHT_REAL_PRACTICE_PASS",
        spread_pips=str(quote.spread_pips), units="20",
        worst_price_bound_loss_usd=str((bound-stop)*UNITS),
    )
    result = adapter.send("POST", "/orders", payload, operation="OPEN")
    opened = classify_response(result, ATTEMPT)
    if opened.outcome == Outcome.CANCELLED:
        adapter.log("OPEN_CANCELLED_BY_BROKER_NO_POSITION")
        return "CANCELLED"
    if (opened.outcome != Outcome.FILLED
        or opened.trade_id is None
        or opened.filled_units != UNITS
        or opened.trade_reduced is not None
        or opened.trades_closed):
        adapter.log("OPEN_OUTCOME_UNVERIFIED_NO_RETRY",
                    classified=opened.outcome.value)
        raise DemoBlocked("OPEN_NOT_VERIFIED_NO_RETRY")
    fill = result.body.get("orderFillTransaction", {})
    adapter.transaction(fill.get("id"), "ORDER_FILL")
    tid = opened.trade_id
    if not isinstance(tid, str) or not tid.isdecimal():
        raise DemoBlocked("OPEN_TRADE_ID_UNVERIFIED")
    trade = adapter.owned_trade(tid, UNITS, stop, target)
    if number(trade.get("price"), "trade entry") != opened.fill_price:
        raise DemoBlocked("GET_EXECUTION_PRICE_MISMATCH")
    adapter.log("PRACTICE_OPEN_PROTECTED_CONFIRMED",
                trade_id=tid, fill_tx=fill["id"],
                order_id=opened.order_id)
    # BE is submitted only after a real favorable move of >10 pip.
    fresh = parse_market_quote(
        adapter.get("/pricing", params={"instruments": "EUR_USD"}),
        "EUR_USD", rules, now=utc_now(),
        max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        max_spread_pips=Decimal(str(config.MAX_SPREAD_PIPS)),
    )
    if fresh.bid > opened.fill_price + Decimal("10")*rules.pip_size:
        new_stop = opened.fill_price
        update = adapter.send(
            "PUT", "/trades/" + tid + "/orders",
            {"stopLoss": {"price": str(new_stop)}},
            operation="BREAK_EVEN",
        )
        tx = update.body.get("stopLossOrderTransaction")
        if (update.status_code != 200 or not isinstance(tx, dict)
            or tx.get("type") != "STOP_LOSS_ORDER"
            or tx.get("tradeID") != tid):
            raise DemoBlocked("BE_OUTCOME_UNVERIFIED_NO_RETRY")
        adapter.transaction(tx.get("id"), "STOP_LOSS_ORDER")
        stop = new_stop
        adapter.owned_trade(tid, UNITS, stop, target)
        adapter.log("PRACTICE_BREAK_EVEN_CONFIRMED", tx=tx["id"])
    else:
        adapter.log("BREAK_EVEN_SKIPPED_PRICE_CONDITION")
    # Recheck ownership and unchanged protection before each close action.
    adapter.owned_trade(tid, UNITS, stop, target)
    half = adapter.send(
        "PUT", "/trades/" + tid + "/close",
        {"units": "10"}, operation="PARTIAL_CLOSE",
    )
    partial_fill = half.body.get("orderFillTransaction")
    reduction = (partial_fill or {}).get("tradeReduced")
    if (half.status_code != 200 or not isinstance(reduction, dict)
        or reduction.get("tradeID") != tid
        or number(reduction.get("units"), "reduction") != Decimal("10")):
        raise DemoBlocked("PARTIAL_OUTCOME_UNVERIFIED_NO_RETRY")
    adapter.transaction(partial_fill.get("id"), "ORDER_FILL")
    adapter.owned_trade(tid, Decimal("10"), stop, target)
    adapter.log("PRACTICE_PARTIAL_CLOSE_CONFIRMED",
                tx=partial_fill["id"], remaining_units="10")
    final = adapter.send(
        "PUT", "/trades/" + tid + "/close",
        {"units": "ALL"}, operation="FULL_CLOSE",
    )
    final_fill = final.body.get("orderFillTransaction")
    closes = (final_fill or {}).get("tradesClosed")
    if (final.status_code != 200 or not isinstance(closes, list)
        or len(closes) != 1 or closes[0].get("tradeID") != tid
        or number(closes[0].get("units"), "closing units") != Decimal("10")):
        raise DemoBlocked("FULL_CLOSE_UNVERIFIED_NO_RETRY")
    adapter.transaction(final_fill.get("id"), "ORDER_FILL")
    a = adapter.get("").get("account", {})
    if (a.get("id") != ACCOUNT or a.get("openTradeCount") != 0
        or a.get("trades") != []):
        raise DemoBlocked("FINAL_INVENTORY_UNVERIFIED")
    adapter.log(
        "PRACTICE_FULL_CYCLE_CONFIRMED", trade_id=tid,
        final_tx=final_fill["id"], open_trades=0,
        final_balance=str(a.get("balance")),
        final_nav=str(a.get("NAV")),
        broker_final_fill_pl=str(final_fill.get("pl", "UNAVAILABLE")),
        broker_final_fill_commission=str(
            final_fill.get("commission", "UNAVAILABLE")
        ),
    )
    return "CONFIRMED"


def main():
    try:
        exercise(DemoAdapter())
        return 0
    except DemoBlocked as exc:
        print(json.dumps({"status": "DEMO_ONE_SHOT_BLOCKED",
                          "reason": str(exc), "no_blind_retry": True}))
        return 2
    except Exception:
        print(json.dumps({"status": "DEMO_ONE_SHOT_BLOCKED",
                          "reason": "UNEXPECTED_ERROR",
                          "no_blind_retry": True}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
