"""OANDA REST v20 boundary: reads are pure, mutating requests are never retried.

This module deliberately does not guess whether a lost write response succeeded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
import requests


class BrokerError(RuntimeError):
    pass


class BrokerRejected(BrokerError):
    pass


class OutcomeUnknown(BrokerError):
    """A write may have reached OANDA. Stop and reconcile, never blind-retry."""


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BrokerError("Invalid broker number") from exc
    if not result.is_finite():
        raise BrokerError("Non-finite broker number")
    return result


def timestamp(value: str) -> datetime:
    try:
        value = value.replace("Z", "+00:00")
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("Naive broker timestamp")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BrokerError("Invalid broker timestamp") from exc


def quote_from_pricing(response: dict, instrument: str, now: datetime, max_age_seconds: int = 30) -> dict:
    prices = response.get("prices")
    if not isinstance(prices, list):
        raise BrokerError("Missing pricing list")
    matches = [p for p in prices if isinstance(p, dict) and p.get("instrument") == instrument]
    if len(matches) != 1:
        raise BrokerError("Instrument not found or duplicated in pricing")
    quote = matches[0]
    if quote.get("tradeable") is not True:
        raise BrokerError("Quote not explicitly tradeable")
    age = (now.astimezone(timezone.utc) - timestamp(quote.get("time"))).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise BrokerError("Stale or future broker quote")
    try:
        bid = number(quote["bids"][0]["price"])
        ask = number(quote["asks"][0]["price"])
    except (KeyError, IndexError, TypeError) as exc:
        raise BrokerError("Quote lacks bid or ask liquidity") from exc
    if bid <= 0 or ask <= bid:
        raise BrokerError("Crossed, zero or invalid quote")
    return {"instrument": instrument, "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "time": quote["time"], "age_seconds": age}


class OandaClient:
    """OANDA REST v20 Practice client. No live URL is accepted."""

    def __init__(self, token: str, account_id: str, session=None, timeout: int = 10):
        if not token or not account_id:
            raise BrokerError("OANDA Practice token/account environment variables required")
        self.token = token
        self.account_id = account_id
        self.base_url = "https://api-fxpractice.oanda.com/v3"
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params=None, payload=None) -> dict:
        url = self.base_url + "/accounts/" + self.account_id + path
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "Content-Type": "application/json"}
        writing = method.upper() != "GET"
        try:
            response = self.session.request(method, url, headers=headers, params=params,
                                            json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            if writing:
                raise OutcomeUnknown("Write transport failed; broker reconciliation required") from exc
            raise BrokerError("Read transport failed") from exc

        if not 200 <= response.status_code < 300:
            if writing and response.status_code >= 500:
                raise OutcomeUnknown("Broker server error after write; reconcile")
            if writing and response.status_code in (408, 429):
                raise OutcomeUnknown("Write outcome unknown; reconcile")
            raise BrokerRejected("OANDA returned HTTP " + str(response.status_code))
        try:
            result = response.json()
        except (ValueError, TypeError) as exc:
            if writing:
                raise OutcomeUnknown("Unparseable write response") from exc
            raise BrokerError("Unparseable read response") from exc
        if not isinstance(result, dict):
            if writing:
                raise OutcomeUnknown("Malformed write response")
            raise BrokerError("Malformed read response")
        return result

    def account(self) -> dict:
        result = self._request("GET", "")
        account = result.get("account")
        if not isinstance(account, dict) or account.get("id") != self.account_id:
            raise BrokerError("Account mismatch in broker response")
        return result

    def instruments(self) -> dict:
        return self._request("GET", "/instruments")

    def candles(self, instrument: str, granularity: str = "M15", count: int = 200) -> dict:
        return self._request("GET", "/instruments/" + instrument + "/candles",
                             params={"granularity": granularity, "count": count, "price": "M"})

    def pricing(self, instrument: str) -> dict:
        return self._request("GET", "/pricing",
                             params={"instruments": instrument, "includeHomeConversions": "true"})

    def open_trades(self) -> dict:
        return self._request("GET", "/openTrades")

    def pending_orders(self) -> dict:
        return self._request("GET", "/pendingOrders")

    def changes(self, last_transaction_id: str) -> dict:
        return self._request("GET", "/changes", params={"sinceTransactionID": last_transaction_id})

    def trade(self, trade_id: str) -> dict:
        return self._request("GET", "/trades/" + trade_id)

    def create_order(self, order: dict) -> dict:
        return self._request("POST", "/orders", payload={"order": order})

    def set_dependent_orders(self, trade_id: str, *, stop_loss=None, take_profit=None) -> dict:
        payload = {}
        if stop_loss is not None:
            payload["stopLoss"] = {"price": str(stop_loss)}
        if take_profit is not None:
            payload["takeProfit"] = {"price": str(take_profit)}
        if not payload:
            raise BrokerError("No dependent order change requested")
        # Omitting takeProfit preserves it. Explicit null would CANCEL it.
        return self._request("PUT", "/trades/" + trade_id + "/orders", payload=payload)

    def close_trade(self, trade_id: str, units: str = "ALL") -> dict:
        if units != "ALL" and number(units) <= 0:
            raise BrokerError("Partial close units must be positive even for SHORT")
        return self._request("PUT", "/trades/" + trade_id + "/close", payload={"units": str(units)})


def trade_fill(response: dict) -> dict:
    """Classify a MARKET-order outcome, never use order/transaction ID as trade ID."""
    fill = response.get("orderFillTransaction")
    if not isinstance(fill, dict):
        if response.get("orderCancelTransaction") or response.get("orderRejectTransaction"):
            return {"status": "REJECTED", "trade_id": None}
        raise OutcomeUnknown("No fill/cancel transaction in write response")
    opened = fill.get("tradeOpened")
    if isinstance(opened, dict) and opened.get("tradeID"):
        return {"status": "OPENED", "trade_id": str(opened["tradeID"]),
                "entry": number(opened["price"]) if "price" in opened else None,
                "units": number(opened["units"]) if "units" in opened else None,
                "transaction_id": str(fill.get("id", ""))}
    if fill.get("tradeReduced") or fill.get("tradesClosed"):
        return {"status": "REDUCED_OR_CLOSED", "trade_id": None}
    raise OutcomeUnknown("Fill has no unambiguous opened trade")
