"""Validated, completed OANDA candles and account-specific quote access."""
from __future__ import annotations
from datetime import datetime, timezone
from math import isfinite
from broker.oanda_client import BrokerError, number, quote_from_pricing, timestamp


class DataHandler:
    def __init__(self, client):
        self.client = client

    def get_candles(self, instrument: str, granularity: str = "M15", count: int = 200):
        response = self.client.candles(instrument, granularity, count)
        if response.get("instrument") != instrument or response.get("granularity") != granularity:
            raise BrokerError("Candle instrument/granularity mismatch")
        raw = response.get("candles")
        if not isinstance(raw, list):
            raise BrokerError("Missing candle array")
        result = []
        previous_time = None
        for candle in raw:
            if not isinstance(candle, dict):
                raise BrokerError("Malformed candle")
            if candle.get("complete") is not True:
                # Latest in-progress candle is not a valid signal input.
                continue
            try:
                t = timestamp(candle["time"])
                mid = candle["mid"]
                o, h, l, c = (float(number(mid[k])) for k in ("o", "h", "l", "c"))
            except (KeyError, TypeError, ValueError) as exc:
                raise BrokerError("Malformed OHLC data") from exc
            if previous_time is not None and t <= previous_time:
                raise BrokerError("Duplicate or out-of-order candle timestamps")
            previous_time = t
            if not (all(isfinite(v) and v > 0 for v in (o, h, l, c)) and
                    l <= min(o, c) <= max(o, c) <= h):
                raise BrokerError("Invalid OHLC relationships")
            result.append({"time": t.isoformat(), "open": o, "high": h,
                           "low": l, "close": c})
        if len(result) < 60:
            raise BrokerError("Insufficient completed candle history")
        return result

    def get_quote_and_conversions(self, instrument: str):
        response = self.client.pricing(instrument)
        quote = quote_from_pricing(response, instrument, datetime.now(timezone.utc))
        return quote, response
