"""Category 29: offline research-only attestation of one frozen OANDA export.

This does not alter the strict production collector or MarketSessions. Missing
market-open candles are never synthesized. Dated holiday evidence is checked
against *exact* gaps; the uncertain opening of 2023 is excluded explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from data.historical_oanda import HistoricalDataError, _candle, _gaps, _time
from utils.time_tools import iso_utc, parse_utc


ORIGINAL_SHA256 = "d16ad13ff69b4a4d56e9feb7a1e2cf26858da33b8a61a3abfa5a0f74707b6c6e"
ORIGINAL_START = "2023-01-01T00:00:00Z"
RESEARCH_START = "2023-01-03T00:00:00Z"
RESEARCH_END = "2026-10-31T00:00:00Z"

# Broker-published historical FX holiday schedules (not guesses about
# arbitrary dates). Exact absent-candle envelopes are frozen for this export.
HOLIDAY_GAPS = (
    ("2023-12-22T22:00:00Z", "2023-12-25T22:00:00Z",
     "https://www.oanda.com/eu-en/news/holidays-in-the-following-week"),
    ("2023-12-29T22:00:00Z", "2024-01-01T22:00:00Z",
     "https://www.oanda.com/eu-en/news/holidays-in-the-following-week"),
    ("2024-12-24T22:00:00Z", "2024-12-25T22:00:00Z",
     "https://www.oanda.com/eu-en/news/trading-hours-during-christmas-and-new-year"),
    ("2024-12-31T22:00:00Z", "2025-01-01T22:00:00Z",
     "https://www.oanda.com/eu-en/news/trading-hours-during-christmas-and-new-year"),
    ("2025-12-24T22:00:00Z", "2025-12-25T22:00:00Z",
     "https://www.oanda.com/us-en/trading/holiday-trading-hours/"),
    ("2025-12-31T22:00:00Z", "2026-01-01T22:00:00Z",
     "https://www.oanda.com/us-en/trading/holiday-trading-hours/"),
)


class ResearchAttestationError(ValueError):
    """Source, evidence or bounded historical coverage is unverifiable."""


def _canonical(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def attest_research_export(source, *, source_sha256,
                           research_start=RESEARCH_START,
                           expected_holidays=HOLIDAY_GAPS):
    """Return a replay-compatible price payload and separate evidence manifest.

    It is a dataset COVERAGE attestation, not historical fill or performance
    certification. Date trimming is specified before looking at strategy data.
    """
    if (not isinstance(source, dict)
        or set(source) != {"candles", "from", "gaps", "granularity",
                           "instrument", "price", "source", "to",
                           "validation_status"}
        or source["source"] != "OANDA_PRACTICE_V20_GET"
        or source["validation_status"] != "UNVERIFIED_NOT_BACKTEST_APPROVED"
        or source["instrument"] != "EUR_USD"
        or source["granularity"] != "M15"
        or source["price"] != "MBA"
        or not isinstance(source["candles"], list)
        or not source["candles"]):
        raise ResearchAttestationError("Unrecognized original OANDA export")
    start, end, cutoff = (
        _time(source["from"]), _time(source["to"]), _time(research_start)
    )
    if not start < cutoff < end:
        raise ResearchAttestationError("Invalid documented exclusion boundary")
    if (not isinstance(expected_holidays, (tuple, list))
        or any(not isinstance(x, (tuple, list)) or len(x) != 3
               for x in expected_holidays)):
        raise ResearchAttestationError("Missing complete holiday evidence")
    expected = {}
    for left, right, link in expected_holidays:
        if (not isinstance(link, str)
            or not link.startswith("https://www.oanda.com/")
            or _time(left) >= _time(right)
            or (left, right) in expected):
            raise ResearchAttestationError("Unverifiable holiday interval")
        expected[(left, right)] = link

    records = source["candles"]
    last = None
    for bar in records:
        try:
            if _candle(bar) != bar:
                raise ResearchAttestationError("Noncanonical OANDA price")
            stamp = parse_utc(bar["time"])
        except (HistoricalDataError, KeyError, TypeError, ValueError) as exc:
            raise ResearchAttestationError("Invalid OANDA source bar") from exc
        if (stamp < start or stamp >= end
            or (last is not None and stamp <= last)):
            raise ResearchAttestationError("Duplicate/out-of-window M15 bar")
        last = stamp
    try:
        original_gaps = _gaps(records, start, end)
    except HistoricalDataError as exc:
        raise ResearchAttestationError("Original timeline invalid") from exc
    if source["gaps"] != original_gaps:
        raise ResearchAttestationError("Original gap ledger was modified")

    kept = [bar for bar in records if parse_utc(bar["time"]) >= cutoff]
    if not kept or parse_utc(kept[0]["time"]) != cutoff:
        raise ResearchAttestationError("Research start has no complete M15 bar")
    try:
        gaps = _gaps(kept, cutoff, end)
    except HistoricalDataError as exc:
        raise ResearchAttestationError("Selected research timeline invalid") from exc
    unmatched = set(expected)
    annotated = []
    for gap in gaps:
        if gap["classification"] == "WEEKEND_CLOSED":
            annotated.append({**gap, "reason": "WEEKEND_CLOSED"})
            continue
        key = gap["from"], gap["to"]
        if key not in unmatched:
            raise ResearchAttestationError(
                "Market-open gap has no exactly matched broker-holiday evidence"
            )
        unmatched.remove(key)
        annotated.append({
            **gap, "reason": "DOCUMENTED_HOLIDAY_ENVELOPE",
            "reference": expected[key],
        })
    if unmatched:
        raise ResearchAttestationError("Expected broker holiday not observed")

    payload = {
        "instrument": source["instrument"],
        "granularity": source["granularity"],
        "candles": kept,
    }
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    manifest = {
        "status": "RESEARCH_PRICE_COVERAGE_ATTESTED",
        "not_broker_fill_verified": True,
        "research_only": True,
        "original_source_sha256": source_sha256,
        "original_validation_status": source["validation_status"],
        "original_from": source["from"],
        "research_from": iso_utc(cutoff),
        "research_to": source["to"],
        "original_candles": len(records),
        "retained_candles": len(kept),
        "excluded_early_candles": len(records) - len(kept),
        "exclusion_reason": "Ambiguous New Year 2023 opening and two M15 no-M1 gaps",
        "remaining_unexplained_market_open_gaps": 0,
        "holiday_evidence_count": len(expected),
        "gaps": annotated,
        "payload_sha256": digest,
        "no_prices_synthesized": True,
        "unverified_external_inputs": [
            "historical economic calendar", "broker tradeability per bar",
            "home-currency conversions", "financing", "commission",
            "slippage and fill probability",
        ],
    }
    return payload, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    if args.sha256 != ORIGINAL_SHA256:
        raise ResearchAttestationError("Research input source SHA must be frozen")
    source_path = Path(args.source)
    if (source_path.is_symlink() or not source_path.is_file()
        or source_path.stat().st_size > 120_000_000):
        raise ResearchAttestationError("Missing/oversized historical source")
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ResearchAttestationError("Source checksum mismatch")
    source = json.loads(raw)
    if (not isinstance(source, dict)
        or source.get("from") != ORIGINAL_START
        or source.get("to") != RESEARCH_END):
        raise ResearchAttestationError("Source UTC window was changed")

    payload, report = attest_research_export(
        source, source_sha256=args.sha256
    )
    destination = Path(args.out_dir)
    if destination.is_symlink():
        raise ResearchAttestationError("Symlink export directory forbidden")
    destination.mkdir(parents=True, exist_ok=True)
    name = "EUR_USD_M15_MBA_RESEARCH_ATTESTED_" + report["payload_sha256"]
    for path, data in (
        (destination / (name + ".json"), payload),
        (destination / (name + "_MANIFEST.json"), report),
    ):
        if path.exists() or path.is_symlink():
            raise ResearchAttestationError("Attested artifact may not be overwritten")
        raw_output = _canonical(data)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            fh.write(raw_output)
            fh.flush()
            os.fsync(fh.fileno())
    print("CATEGORY29_RESEARCH_ATTESTED " + json.dumps({
        "retained": report["retained_candles"],
        "excluded": report["excluded_early_candles"],
        "holidays": report["holiday_evidence_count"],
        "unexplained_gaps": report["remaining_unexplained_market_open_gaps"],
        "sha256": report["payload_sha256"],
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ResearchAttestationError, HistoricalDataError,
            OSError, ValueError, TypeError) as exc:
        print("CATEGORY29_ATTESTATION_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
