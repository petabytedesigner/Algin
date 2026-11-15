"""Category-2 broker GET adapters for validated risk-planning inputs.

These independent GETs are NOT an atomic account snapshot. In particular,
pending/unknown order outcomes cannot be inferred from the account alone.
Never mark exposure or loss history verified inside this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import config
from risk.trade_plan import (
    AccountFunds, InstrumentRules, PlanRejected, QuoteConversions, number,
)
from utils.common import safe_request
from utils.time_tools import parse_utc


def _get_json(path: str, *, params=None) -> dict:
    if not config.API_KEY or not config.ACCOUNT_ID:
        raise PlanRejected("OANDA Practice credentials unavailable")
    response = safe_request(
        "GET", f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}{path}",
        headers={"Authorization": f"Bearer {config.API_KEY}"},
        params=params, retries=2,
    )
    if response is None or response.status_code != 200:
        raise PlanRejected("Broker GET failed; no trade plan is permitted")
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise PlanRejected("Invalid broker JSON") from exc
    if not isinstance(payload, dict):
        raise PlanRejected("Broker payload is not a JSON object")
    return payload


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PlanRejected(f"{name} invalid integer")
    return value


@dataclass(frozen=True)
class BrokerAccountRead:
    funds: AccountFunds
    open_trades: tuple[dict, ...]
    pending_orders: tuple[dict, ...]


def get_account_read() -> BrokerAccountRead:
    """OANDA GET /accounts/{accountID}, not summary: includes open/pending lists."""
    payload = _get_json("")
    try:
        raw = payload["account"]
        tx = payload["lastTransactionID"]
        if not isinstance(raw, dict) or not isinstance(tx, str) or not tx:
            raise PlanRejected("Account details or transaction ID missing")
        if raw["id"] != config.ACCOUNT_ID:
            raise PlanRejected("Account identity mismatch")
        trades = raw["trades"]
        orders = raw["orders"]
        if not isinstance(trades, list) or not isinstance(orders, list) or (
            not all(isinstance(item, dict) for item in trades + orders)
        ):
            raise PlanRejected("Incomplete broker open trades/pending orders")
        open_count = _integer(raw["openTradeCount"], "openTradeCount")
        pending_count = _integer(raw["pendingOrderCount"], "pendingOrderCount")
        if open_count != len(trades) or pending_count != len(orders):
            raise PlanRejected("Broker trade/order count mismatch")
        funds = AccountFunds(
            account_id=raw["id"], currency=raw["currency"],
            balance=number(raw["balance"], "balance", positive=True),
            nav=number(raw["NAV"], "NAV", positive=True),
            margin_available=number(raw["marginAvailable"], "marginAvailable"),
            margin_used=number(raw["marginUsed"], "marginUsed"),
            broker_open_count=open_count, broker_pending_count=pending_count,
            transaction_id=tx,
        )
        return BrokerAccountRead(funds, tuple(trades), tuple(orders))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, PlanRejected):
            raise
        raise PlanRejected("Malformed OANDA Account details") from exc


def get_instrument_rules(instrument: str) -> InstrumentRules:
    """Use account-permitted instrument metadata; no guessed pip/unit precision."""
    payload = _get_json("/instruments", params={"instruments": instrument})
    try:
        items = payload["instruments"]
        if not isinstance(items, list):
            raise PlanRejected("Invalid instrument listing")
        matched = [i for i in items if isinstance(i, dict) and i.get("name") == instrument]
        if len(matched) != 1:
            raise PlanRejected("Instrument missing or ambiguous")
        raw = matched[0]
        return InstrumentRules(
            instrument=raw["name"], kind=raw["type"],
            pip_location=_integer_signed(raw["pipLocation"], "pipLocation"),
            display_precision=_integer(raw["displayPrecision"], "displayPrecision"),
            units_precision=_integer(raw["tradeUnitsPrecision"], "tradeUnitsPrecision"),
            minimum_trade_size=number(raw["minimumTradeSize"], "minimumTradeSize", positive=True),
            maximum_order_units=number(raw["maximumOrderUnits"], "maximumOrderUnits", positive=True),
            maximum_position_size=number(raw["maximumPositionSize"], "maximumPositionSize"),
            margin_rate=number(raw["marginRate"], "marginRate", positive=True),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PlanRejected):
            raise
        raise PlanRejected("Malformed instrument specification") from exc


def _integer_signed(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanRejected(f"{name}: invalid integer")
    return value


def _conversion(records: list, currency: str, field: str, home: str) -> Decimal:
    if currency == home:
        return Decimal("1")
    matches = [record for record in records if (
        isinstance(record, dict) and record.get("currency") == currency
    )]
    if len(matches) != 1:
        raise PlanRejected(f"Home conversion missing or ambiguous: {currency}")
    try:
        return number(matches[0][field], f"{currency}.{field}", positive=True)
    except KeyError as exc:
        raise PlanRejected(f"Missing home conversion factor: {currency}.{field}") from exc


def get_quote_conversions(instrument: str, account_currency: str) -> QuoteConversions:
    """Use includeHomeConversions=true; ignore deprecated per-price factors."""
    payload = _get_json(
        "/pricing", params={
            "instruments": instrument,
            "includeHomeConversions": "true",
        }
    )
    try:
        raw_prices = payload["prices"]
        conversions = payload["homeConversions"]
        if not isinstance(raw_prices, list) or not isinstance(conversions, list):
            raise PlanRejected("Missing homeConversions list")
        prices = [p for p in raw_prices if (
            isinstance(p, dict) and p.get("instrument") == instrument
        )]
        if len(prices) != 1 or not (
            (prices[0].get("status") == "tradeable"
             and prices[0].get("tradeable", True) is not False)
            if "status" in prices[0]
            else prices[0].get("tradeable") is True
        ):
            raise PlanRejected("Price missing, ambiguous or not tradeable")
        quote = prices[0]
        base, home_quote = instrument.split("_", 1)
        if not base or not home_quote or len(account_currency) != 3:
            raise PlanRejected("Unsupported instrument/account currency")
        bid = number(quote["bids"][0]["price"], "bid", positive=True)
        ask = number(quote["asks"][0]["price"], "ask", positive=True)
        loss_factor = _conversion(conversions, home_quote, "accountLoss", account_currency)
        position_factor = _conversion(conversions, base, "positionValue", account_currency)
        return QuoteConversions(
            instrument=instrument, bid=bid, ask=ask,
            timestamp=parse_utc(quote["time"]),
            quote_loss_to_home=loss_factor,
            base_value_to_home=position_factor,
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, PlanRejected):
            raise
        raise PlanRejected("Malformed pricing or conversion data") from exc
