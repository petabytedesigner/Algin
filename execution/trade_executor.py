"""Read-only OANDA Practice boundary for Category 1.

The public mutation method signatures are retained for later categories,
but no order / dependent order / trade close implementation lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Optional

import config
from utils.common import safe_request


@dataclass
class OrderRequest:
    """Legacy request structure; cannot be submitted in Category 1."""

    instrument: str
    direction: str
    units: int
    sl_price: float
    tp_price: float
    client_tag: Optional[str] = None
    comment: Optional[str] = None


class BrokerDataError(RuntimeError):
    """Broker GET failed or returned an untrustworthy account snapshot."""


class TradeExecutor:
    """Normalized broker reads; mutations are deliberately unavailable."""

    def __init__(self) -> None:
        self.base_url = config.OANDA_URL
        self.account_id = config.ACCOUNT_ID
        self.api_key = config.API_KEY

    def get_open_trades(self):
        """Return all open trades; never substitute [] for a failed snapshot."""
        url = f"{self.base_url}/accounts/{self.account_id}/openTrades"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = safe_request("GET", url, headers=headers)
        if response is None or response.status_code != 200:
            raise BrokerDataError("Broker openTrades read unavailable")
        try:
            data = response.json()
            trades = data["trades"]
            if not isinstance(trades, list):
                raise ValueError("Missing trade list")
            result = []
            seen = set()
            for trade in trades:
                if not isinstance(trade, dict):
                    raise ValueError("Invalid trade item")
                trade_id = trade["id"]
                instrument = trade["instrument"]
                units = float(trade["currentUnits"])
                entry = float(trade["price"])
                if (
                    not isinstance(trade_id, str) or not trade_id.strip()
                    or trade_id in seen
                    or not isinstance(instrument, str) or not instrument.strip()
                    or not isfinite(units) or units == 0
                    or not isfinite(entry) or entry <= 0
                    or trade.get("state", "OPEN") != "OPEN"
                ):
                    raise ValueError("Inconsistent trade identity or pricing")
                seen.add(trade_id)
                stop_order, target_order = trade.get("stopLossOrder"), trade.get("takeProfitOrder")
                if stop_order is not None and not isinstance(stop_order, dict):
                    raise ValueError("Invalid stopLossOrder")
                if target_order is not None and not isinstance(target_order, dict):
                    raise ValueError("Invalid takeProfitOrder")
                stop = float(stop_order["price"]) if stop_order is not None else None
                target = float(target_order["price"]) if target_order is not None else None
                if (stop is not None and (not isfinite(stop) or stop <= 0)) or (
                    target is not None and (not isfinite(target) or target <= 0)
                ):
                    raise ValueError("Invalid stop/target")
                result.append({
                    "id": trade_id, "instrument": instrument,
                    "units": units, "direction": "LONG" if units > 0 else "SHORT",
                    "price": entry, "sl": stop, "tp": target,
                })
            return result
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise BrokerDataError("Malformed broker openTrades snapshot") from exc

    def get_price(self, instrument):
        """Return bid/ask/mid only for one explicitly tradeable instrument."""
        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = safe_request(
            "GET", url, headers=headers, params={"instruments": instrument}
        )
        if response is None or response.status_code != 200:
            raise BrokerDataError("Broker pricing read unavailable")
        try:
            prices = response.json()["prices"]
            if not isinstance(prices, list):
                raise ValueError("Invalid price list")
            matches = [
                p for p in prices
                if isinstance(p, dict) and p.get("instrument") == instrument
            ]
            if len(matches) != 1 or matches[0].get("tradeable") is not True:
                raise ValueError("Untradeable, missing or duplicated quote")
            bid = float(matches[0]["bids"][0]["price"])
            ask = float(matches[0]["asks"][0]["price"])
            if not (isfinite(bid) and isfinite(ask) and 0 < bid < ask):
                raise ValueError("Invalid bid/ask")
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        except (KeyError, IndexError, ValueError, TypeError, OverflowError) as exc:
            raise BrokerDataError("Malformed broker quote") from exc

    def open_market_order(self, req: OrderRequest) -> Optional[str]:
        """Preserve the signature; no order payload is built or transmitted."""
        raise RuntimeError("Broker order submission unavailable until Categories 2–4")

    def modify_sl_tp(
        self, trade_id: str, sl_price: Optional[float] = None,
        tp_price: Optional[float] = None
    ) -> bool:
        """Preserve the signature; no SL/TP modification is transmitted."""
        raise RuntimeError("Dependent-order mutation unavailable until Categories 3–4")

    def close_trade(self, trade_id: str, units: Optional[int] = None) -> bool:
        """Preserve the signature; no trade close is transmitted."""
        raise RuntimeError("Trade closing unavailable until Categories 3–4")
