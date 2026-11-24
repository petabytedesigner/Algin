"""Category 29: GET-only, reproducible OANDA Practice M15 MBA research ingestion.

This module is not imported by the live scanner and cannot place orders.
Missing in-week candles block certification; weekends are never invented.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

import config
from broker.oanda_v20_contract import account_id
from broker.practice_transport import PRACTICE_API
from data.market_sessions import MarketSessions
from utils.common import safe_request
from utils.time_tools import iso_utc, parse_utc, require_utc, utc_now


BAR = timedelta(minutes=15)
MAX_BARS_PER_PAGE = 4000  # Strictly below the OANDA 5,000 response limit.
MAX_PAGES = 500
MAX_RANGE = timedelta(days=5 * 366)
INSTRUMENT = re.compile(r"[A-Z]{3}_[A-Z]{3}\Z", re.ASCII)
OHLC = ("o", "h", "l", "c")


class HistoricalDataError(ValueError):
    """Historical source, completeness, integrity or provenance is invalid."""


def _time(value):
    try:
        stamp = parse_utc(value) if isinstance(value, str) else require_utc(value)
    except (ValueError, TypeError) as exc:
        raise HistoricalDataError("Timestamp must be timezone-aware UTC") from exc
    if stamp.second or stamp.microsecond or stamp.minute % 15:
        raise HistoricalDataError("Historical window/bar must align to M15 UTC")
    return stamp


def _price(value):
    if isinstance(value, bool):
        raise HistoricalDataError("Boolean price forbidden")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HistoricalDataError("Malformed historical price") from exc
    if not result.is_finite() or result <= 0 or result >= Decimal("1000000"):
        raise HistoricalDataError("Nonpositive or nonfinite historical price")
    return str(result)


def _side(raw, name):
    if not isinstance(raw, dict) or set(raw) != set(OHLC):
        raise HistoricalDataError("Missing/unexpected " + name + " OHLC")
    result = {key: _price(raw[key]) for key in OHLC}
    values = {key: Decimal(result[key]) for key in OHLC}
    if (values["l"] > values["h"]
        or not values["l"] <= values["o"] <= values["h"]
        or not values["l"] <= values["c"] <= values["h"]):
        raise HistoricalDataError("Invalid " + name + " OHLC bounds")
    return result


def _candle(item):
    if not isinstance(item, dict) or item.get("complete") is not True:
        raise HistoricalDataError("Incomplete/malformed historical candle")
    try:
        stamp = _time(item["time"])
        sides = {side: _side(item[side], side) for side in ("mid", "bid", "ask")}
        volume = item["volume"]
    except (KeyError, TypeError) as exc:
        raise HistoricalDataError("Missing candle evidence") from exc
    if not isinstance(volume, int) or isinstance(volume, bool) or volume < 0:
        raise HistoricalDataError("Invalid historical volume")
    for point in ("o", "c"):
        if Decimal(sides["bid"][point]) >= Decimal(sides["ask"][point]):
            raise HistoricalDataError("Crossed/locked historical bid-ask")
    return {"time": iso_utc(stamp), "complete": True, "volume": volume, **sides}


def _gaps(candles, start, end):
    """Classify absent M15 intervals without filling or changing prices."""
    known = [start] + [parse_utc(c["time"]) + BAR for c in candles]
    next_times = [parse_utc(c["time"]) for c in candles] + [end]
    gaps = []
    market = MarketSessions()
    for left, right in zip(known, next_times):
        if right < left or (right - left) % BAR:
            raise HistoricalDataError("Unordered or off-grid M15 data")
        missing = int((right - left) / BAR)
        if not missing:
            continue
        open_missing = sum(
            market.market_week_open(left + step * BAR)
            for step in range(missing)
        )
        gaps.append({
            "from": iso_utc(left), "to": iso_utc(right),
            "missing_bars": missing,
            "classification": (
                "UNVERIFIED_MARKET_OPEN_GAP" if open_missing else "WEEKEND_CLOSED"
            ),
        })
    return gaps


def validate_dataset(dataset):
    """Validate downloaded provenance and continuity, including on re-read."""
    if (not isinstance(dataset, dict)
        or set(dataset) != {"version", "source", "instrument", "granularity",
                            "price", "smooth", "from", "to", "candles", "gaps",
                            "request_pages"}
        or dataset["version"] != 1
        or dataset["source"] != "OANDA_PRACTICE_V20_GET"
        or dataset["granularity"] != "M15"
        or dataset["price"] != "MBA" or dataset["smooth"] is not False
        or not isinstance(dataset["instrument"], str)
        or not INSTRUMENT.fullmatch(dataset["instrument"])):
        raise HistoricalDataError("Unidentified historical dataset")
    start, end = _time(dataset["from"]), _time(dataset["to"])
    if not start < end or end - start > MAX_RANGE:
        raise HistoricalDataError("Invalid historical dataset interval")
    if (not isinstance(dataset["request_pages"], int)
        or isinstance(dataset["request_pages"], bool)
        or not 1 <= dataset["request_pages"] <= MAX_PAGES):
        raise HistoricalDataError("Invalid request page count")
    records = dataset["candles"]
    if not isinstance(records, list) or not records or len(records) > 200000:
        raise HistoricalDataError("Empty/oversized historical dataset")
    previous = None
    for item in records:
        canonical = _candle(item)
        if item != canonical:
            raise HistoricalDataError("Noncanonical/modified historical candle")
        stamp = parse_utc(item["time"])
        if stamp < start or stamp >= end or (previous is not None and stamp <= previous):
            raise HistoricalDataError("Out-of-range or duplicate historical candle")
        previous = stamp
    gaps = _gaps(records, start, end)
    if dataset["gaps"] != gaps:
        raise HistoricalDataError("Incomplete/unverified gap provenance")
    if any(g["classification"] != "WEEKEND_CLOSED" for g in gaps):
        raise HistoricalDataError("Unverified market-open gaps: backtest blocked")
    return dataset


def fetch_range(instrument, start, end, *, requester=None,
                max_bars_per_page=MAX_BARS_PER_PAGE, now=None):
    """Download completed M15 MBA bars using bounded explicit GET windows.

    No auto-repair of duplicate/missing bars. No account-changing HTTP.
    The caller must separately persist and attest to the returned dataset.
    """
    start, end = _time(start), _time(end)
    current = utc_now() if now is None else require_utc(now)
    if (not isinstance(instrument, str) or not INSTRUMENT.fullmatch(instrument)
        or not start < end or end > current
        or end - start > MAX_RANGE
        or not isinstance(max_bars_per_page, int)
        or isinstance(max_bars_per_page, bool)
        or not 1 <= max_bars_per_page <= MAX_BARS_PER_PAGE):
        raise HistoricalDataError("Invalid historical download request")
    if (config.OANDA_URL != PRACTICE_API or not config.API_KEY
        or not account_id(config.ACCOUNT_ID)):
        raise HistoricalDataError("OANDA Practice GET credentials unavailable")
    get = safe_request if requester is None else requester
    url = (PRACTICE_API + "/accounts/" + config.ACCOUNT_ID
           + "/instruments/" + instrument + "/candles")
    headers = {"Authorization": "Bearer " + config.API_KEY}
    result = []
    cursor = start
    pages = 0
    while cursor < end:
        if pages >= MAX_PAGES:
            raise HistoricalDataError("Historical request page limit")
        stop = min(end, cursor + BAR * max_bars_per_page)
        params = {
            "from": iso_utc(cursor), "to": iso_utc(stop),
            "granularity": "M15", "price": "MBA",
            "smooth": "false", "includeFirst": "true",
        }
        try:
            response = get("GET", url, headers=headers, params=params)
            if response is None or response.status_code != 200:
                raise HistoricalDataError("OANDA historical GET unavailable")
            payload = response.json()
        except HistoricalDataError:
            raise
        except Exception as exc:
            # Do not leak raw broker bodies, URLs, headers or credentials.
            raise HistoricalDataError("OANDA historical GET failed") from exc
        if (not isinstance(payload, dict)
            or payload.get("instrument") != instrument
            or payload.get("granularity") != "M15"
            or not isinstance(payload.get("candles"), list)
            or len(payload["candles"]) > 5000):
            raise HistoricalDataError("Unexpected OANDA historical response")
        prior = parse_utc(result[-1]["time"]) if result else None
        for item in payload["candles"]:
            candle = _candle(item)
            stamp = parse_utc(candle["time"])
            if (stamp < cursor or stamp >= stop
                or (prior is not None and stamp <= prior)):
                raise HistoricalDataError("Duplicate/out-of-window historical bar")
            prior = stamp
            result.append(candle)
        cursor = stop
        pages += 1
    dataset = {
        "version": 1, "source": "OANDA_PRACTICE_V20_GET",
        "instrument": instrument, "granularity": "M15",
        "price": "MBA", "smooth": False,
        "from": iso_utc(start), "to": iso_utc(end),
        "candles": result, "gaps": _gaps(result, start, end),
        "request_pages": pages,
    }
    return validate_dataset(dataset)


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def save_dataset(dataset, directory=".local/historical"):
    """Content-addressed, fsynced, immutable file. Never overwrite evidence."""
    validate_dataset(dataset)
    folder = Path(directory)
    if folder.is_symlink():
        raise HistoricalDataError("Symlink dataset directory forbidden")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = _canonical(dataset) + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    path = folder / (dataset["instrument"] + "_M15_MBA_" + digest + ".json")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != raw:
            raise HistoricalDataError("Conflicting historical dataset identity")
        return path
    candidate = folder / (".staging-" + secrets.token_hex(16))
    try:
        with os.fdopen(os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() or path.is_symlink():
            raise HistoricalDataError("Concurrent historical dataset publication")
        os.replace(candidate, path)
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return path
    finally:
        if candidate.exists():
            candidate.unlink()


def load_dataset(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise HistoricalDataError("Untrusted historical dataset path")
    if path.stat().st_size > 120 * 1024 * 1024:
        raise HistoricalDataError("Oversized historical dataset")
    raw = path.read_bytes()
    if not raw.endswith(b"\n"):
        raise HistoricalDataError("Truncated/oversized historical dataset")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise HistoricalDataError("Unreadable historical dataset") from exc
    if not isinstance(data, dict) or not isinstance(data.get("instrument"), str):
        raise HistoricalDataError("Malformed historical dataset")
    expected = data["instrument"] + "_M15_MBA_" + hashlib.sha256(raw).hexdigest() + ".json"
    if path.name != expected:
        raise HistoricalDataError("Historical dataset checksum mismatch")
    return validate_dataset(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="GET-only OANDA Practice MBA M15 history")
    parser.add_argument("--instrument", default="EUR_USD")
    parser.add_argument("--start", required=True, help="UTC aligned ISO8601, inclusive")
    parser.add_argument("--end", required=True, help="UTC aligned ISO8601, exclusive")
    parser.add_argument("--out", default=".local/historical")
    args = parser.parse_args(argv)
    try:
        data = fetch_range(args.instrument, args.start, args.end)
        path = save_dataset(data, args.out)
    except HistoricalDataError:
        parser.exit(3, "HISTORICAL_DATA_BLOCKED\n")
    print(json.dumps({"path": str(path), "sha256": path.stem.rsplit("_", 1)[-1],
                      "candles": len(data["candles"]),
                      "pages": data["request_pages"],
                      "gaps": len(data["gaps"]),
                      "backtest_input_verified": True}, sort_keys=True))


if __name__ == "__main__":
    main()
