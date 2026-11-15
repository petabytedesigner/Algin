"""OANDA M15 candle ingestion and deterministic data integrity (P50/P51).

Latest incomplete candle is excluded. Earlier incomplete, duplicates,
unordered timestamps, invalid prices and stale/future bars reject series.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from utils.time_tools import parse_utc, require_utc


class CandleDataError(ValueError):
    """Candle history is not safe for a trading signal."""


def _price(value, name):
    if value is None or isinstance(value, bool):
        raise CandleDataError(f"{name}: missing price")
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise CandleDataError(f"{name}: invalid price") from exc
    if not result.is_finite() or result <= 0:
        raise CandleDataError(f"{name}: non-positive or non-finite")
    return float(result)


def _normalize(item):
    if not isinstance(item, dict) or item.get("complete") is not True:
        raise CandleDataError("Candle not confirmed complete")
    try:
        timestamp = parse_utc(item["time"])
        if timestamp.second or timestamp.microsecond or timestamp.minute % 15:
            raise CandleDataError("Unaligned M15 candle timestamp")
        numbers = {name: _price(item[name], name)
                   for name in ("open", "high", "low", "close")}
        if (numbers["low"] > numbers["high"]
            or not numbers["low"] <= numbers["open"] <= numbers["high"]
            or not numbers["low"] <= numbers["close"] <= numbers["high"]):
            raise CandleDataError("OHLC violates high/low bounds")
        return {"time": timestamp.isoformat().replace("+00:00", "Z"),
                "complete": True, **numbers}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CandleDataError):
            raise
        raise CandleDataError("Malformed OHLC/time") from exc


def validate_candles(candles, *, now=None, min_count=1,
                     max_age_minutes=45):
    """Require strictly ordered M15 completed bars and optional freshness.

    Candle timestamps refer to interval starts. A bar can only be evaluated
    after its 15-minute end. Never sort or silently deduplicate source data.
    """
    if (not isinstance(candles, list) or not candles
        or not isinstance(min_count, int) or isinstance(min_count, bool)
        or len(candles) < min_count or min_count < 1):
        raise CandleDataError("Missing/insufficient M15 history")
    out = []
    previous = None
    for candle in candles:
        clean = _normalize(candle)
        stamp = parse_utc(clean["time"])
        if previous is not None and stamp <= previous:
            raise CandleDataError("Unordered or duplicated candles")
        out.append(clean)
        previous = stamp
    if now is not None:
        current = require_utc(now)
        close_time = previous + timedelta(minutes=15)
        if close_time > current:
            raise CandleDataError("Last M15 candle not closed yet")
        if current - close_time > timedelta(minutes=max_age_minutes):
            raise CandleDataError("Stale completed M15 feed")
        for a, b in zip(out[-10:-1], out[-9:]):
            if (parse_utc(b["time"]) - parse_utc(a["time"])
                != timedelta(minutes=15)):
                raise CandleDataError("Gap in most recent M15 sequence")
    return out


def parse_oanda_candles(payload, *, instrument=None, granularity="M15"):
    """Normalize OANDA mid-price OHLC; omit only a final complete=false bar."""
    if granularity != "M15" or not isinstance(payload, dict):
        raise CandleDataError("Unsupported granularity or invalid response")
    if payload.get("granularity", "M15") != "M15":
        raise CandleDataError("Unexpected OANDA candle granularity")
    if instrument is not None and payload.get("instrument", instrument) != instrument:
        raise CandleDataError("OANDA candle instrument mismatch")
    records = payload.get("candles")
    if not isinstance(records, list) or not records:
        raise CandleDataError("No OANDA candle data")
    out = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise CandleDataError("Malformed candle item")
        if item.get("complete") is not True:
            if index == len(records) - 1 and item.get("complete") is False:
                continue
            raise CandleDataError("Unknown/middle incomplete candle")
        mid = item.get("mid")
        if not isinstance(mid, dict):
            raise CandleDataError("Missing OANDA midpoint OHLC")
        try:
            out.append(_normalize({
                "time": item["time"], "complete": True,
                "open": mid["o"], "high": mid["h"],
                "low": mid["l"], "close": mid["c"]
            }))
        except KeyError as exc:
            raise CandleDataError("Missing OANDA OHLC") from exc
    return validate_candles(out)
