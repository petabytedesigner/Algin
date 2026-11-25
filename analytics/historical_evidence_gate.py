"""Category 30 evidence inventory: read-only, immutable research inputs only.

This gate NEVER derives broker tradability or historical news from OHLC bars,
and NEVER generates orders, trades, backtest P/L or supposed broker fills.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys


PRICE_SHA256 = "b48e0f98a2fe948d83afd1d384ae8b23e28b04c1275425c06b543cf75dce5466"
ORIGINAL_SHA256 = "d16ad13ff69b4a4d56e9feb7a1e2cf26858da33b8a61a3abfa5a0f74707b6c6e"
PRICE_START = "2023-01-03T00:00:00Z"
PRICE_END = "2026-10-31T00:00:00Z"
EXPECTED_RETAINED_BARS = 95320
EXPECTED_SOURCE_BARS = 95418
SIGNAL_RUN_ID = 45645175399
YEAR_COUNTS = {2023: 2, 2024: 6, 2025: 1, 2026: 7}
MONTHS = tuple(
    f"{year:04d}-{month:02d}"
    for year in range(2023, 2027)
    for month in range(1, (10 if year == 2026 else 12) + 1)
)


class EvidenceGateError(ValueError):
    """Research input provenance or its claimed evidence is incomplete."""


def _read(path, *, limit):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
        or path.stat().st_size > limit):
        raise EvidenceGateError("Missing or unsafe research input")
    return path.read_bytes()


def _json(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise EvidenceGateError("Malformed research JSON") from exc


def _utc(value):
    if not isinstance(value, str):
        raise EvidenceGateError("Missing UTC event time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceGateError("Invalid UTC event time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceGateError("Non-UTC research event time")
    return parsed.astimezone(timezone.utc)


def inspect_price(payload_path, manifest_path):
    """Pin the exact Category 29 payload and its separate coverage manifest."""
    raw = _read(payload_path, limit=50_000_000)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PRICE_SHA256:
        raise EvidenceGateError("Category 29 price SHA-256 mismatch")
    payload = _json(raw)
    raw_manifest = _read(manifest_path, limit=1_000_000)
    manifest = _json(raw_manifest)
    if (not isinstance(payload, dict)
        or set(payload) != {"instrument", "granularity", "candles"}
        or payload["instrument"] != "EUR_USD"
        or payload["granularity"] != "M15"
        or not isinstance(payload["candles"], list)
        or len(payload["candles"]) != EXPECTED_RETAINED_BARS):
        raise EvidenceGateError("Attested price shape/count mismatch")
    if (not isinstance(manifest, dict)
        or manifest.get("status") != "RESEARCH_PRICE_COVERAGE_ATTESTED"
        or manifest.get("original_source_sha256") != ORIGINAL_SHA256
        or manifest.get("payload_sha256") != digest
        or manifest.get("retained_candles") != EXPECTED_RETAINED_BARS
        or manifest.get("original_candles") != EXPECTED_SOURCE_BARS
        or manifest.get("excluded_early_candles") != 98
        or manifest.get("research_from") != PRICE_START
        or manifest.get("research_to") != PRICE_END
        or manifest.get("remaining_unexplained_market_open_gaps") != 0
        or manifest.get("holiday_evidence_count") != 6
        or manifest.get("research_only") is not True
        or manifest.get("not_broker_fill_verified") is not True
        or manifest.get("no_prices_synthesized") is not True):
        raise EvidenceGateError("Category 29 evidence manifest mismatch")
    first, last = payload["candles"][0], payload["candles"][-1]
    if (not isinstance(first, dict) or not isinstance(last, dict)
        or first.get("time") != PRICE_START
        or _utc(last.get("time")) >= _utc(PRICE_END)):
        raise EvidenceGateError("Attested price boundary mismatch")
    return {
        "instrument": "EUR_USD", "granularity": "M15",
        "coverage_status": "PRICE_TIME_COVERAGE_ONLY",
        "payload_sha256": digest,
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "retained_bars": len(payload["candles"]),
        "broker_fill_verified": False,
    }


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def inspect_signals(directory):
    """Verify all 46 monthly candidates from the frozen Category 30 run."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceGateError("Missing or unsafe monthly report directory")
    expected = {month + ".json" for month in MONTHS}
    actual = {path.name for path in root.iterdir()}
    if actual != expected:
        raise EvidenceGateError("Missing, duplicate or unexpected month input")
    by_year = Counter()
    directions = Counter()
    grades = Counter()
    setup_ids = set()
    eligible = 0
    chain = hashlib.sha256()
    for month in MONTHS:
        name = month + ".json"
        raw = _read(root / name, limit=100_000)
        chain.update(name.encode("ascii") + b"\0" + hashlib.sha256(raw).digest())
        data = _json(raw)
        if (not isinstance(data, dict)
            or data.get("month") != month
            or data.get("dataset_sha256") != PRICE_SHA256
            or data.get("status") != "RESEARCH_SIGNAL_CANDIDATES_ONLY"
            or data.get("historical_news") != "UNKNOWN_NOT_BYPASSED"
            or data.get("historical_broker_tradeability") != "UNKNOWN"
            or data.get("trades_executed") != 0
            or type(data.get("trades_executed")) is not int
            or data.get("profit_calculated") is not False):
            raise EvidenceGateError("Research status/provenance was promoted")
        count = data.get("candidate_decisions", 0)
        bars = data.get("schedule_eligible_bars", 0)
        decisions = data.get("decision_bars", 0)
        items = data.get("candidate_details")
        if (not _nonnegative_int(count)
            or not _nonnegative_int(bars)
            or not _nonnegative_int(decisions)
            or bars > decisions
            or not isinstance(items, list) or len(items) != count
            or sum(data.get("grade_" + grade, 0) for grade in ("A", "B")) != count):
            raise EvidenceGateError("Inconsistent monthly signal totals")
        eligible += bars
        by_year[int(month[:4])] += count
        for item in items:
            if not isinstance(item, dict):
                raise EvidenceGateError("Malformed candidate detail")
            direction, grade, setup = (
                item.get("direction"), item.get("grade"), item.get("setup_id")
            )
            score = item.get("score")
            if (direction not in ("LONG", "SHORT")
                or grade not in ("A", "B")
                or type(score) not in (int, float)
                or not math.isfinite(score) or not 0 <= score <= 100
                or not isinstance(setup, str)
                or not setup.startswith("algin-setup-")
                or setup in setup_ids
                or _utc(item.get("decision_utc")).strftime("%Y-%m") != month):
                raise EvidenceGateError("Invalid, repeated or future candidate")
            setup_ids.add(setup)
            directions[direction] += 1
            grades[grade] += 1
    if dict(by_year) != YEAR_COUNTS or len(setup_ids) != 16:
        raise EvidenceGateError("Category 30 frozen signal baseline changed")
    return {
        "source_run_id": SIGNAL_RUN_ID,
        "monthly_report_count": len(MONTHS),
        "monthly_archive_sha256": chain.hexdigest(),
        "scheduled_eligible_decisions": eligible,
        "candidate_decisions": len(setup_ids),
        "candidates_by_year": {str(k): by_year[k] for k in YEAR_COUNTS},
        "candidates_by_direction": dict(sorted(directions.items())),
        "grades": dict(sorted(grades.items())),
        "unique_setup_ids": len(setup_ids),
    }


def build_report(price, signals):
    """Only a readiness inventory; never a modeled profit result."""
    blockers = [
        "HISTORICAL_ECONOMIC_CALENDAR_UNVERIFIED",
        "BROKER_TRADEABILITY_PER_BAR_UNKNOWN",
        "ACCOUNT_CURRENCY_CONVERSION_PER_BAR_UNVERIFIED",
        "HISTORICAL_COMMISSION_FINANCING_UNVERIFIED",
        "SLIPPAGE_AND_ORDER_FILL_UNVERIFIED",
        "HOLDOUT_INSUFFICIENT_CANDIDATE_SAMPLE",
    ]
    return {
        "schema": "CATEGORY30_EVIDENCE_READINESS_V1",
        "research_only": True,
        "source": {"price": price, "signals": signals},
        "evidence_status": {
            "bid_ask_m15_ohlc": "ATTESTED_PRICE_COVERAGE_ONLY",
            "historical_economic_calendar": "NOT_ATTESTED",
            "per_bar_broker_tradeability": "UNKNOWN",
            "per_bar_account_currency_conversions": "NOT_ATTESTED",
            "commissions_and_financing": "UNVERIFIED",
            "execution_slippage_and_fills": "UNVERIFIED",
        },
        "frozen_holdout": {
            "development_years": [2023, 2024, 2025],
            "reserved_year": 2026,
            "development_candidates": sum(YEAR_COUNTS[y] for y in (2023, 2024, 2025)),
            "reserved_candidates": YEAR_COUNTS[2026],
            "statistically_sufficient": False,
        },
        "backtest_readiness": {
            "realistic_net_pnl_allowed": False,
            "simulation_assumptions_not_yet_supplied": True,
            "trade_count": 0,
            "profit_calculated": False,
            "blocking_reasons": blockers,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--signals-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = build_report(
        inspect_price(args.dataset, args.manifest),
        inspect_signals(args.signals_dir),
    )
    destination = Path(args.output)
    if destination.exists() or destination.is_symlink():
        raise EvidenceGateError("Evidence report must not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    with os.fdopen(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    print("CATEGORY30_EVIDENCE_GATE " + json.dumps({
        "status": "BLOCKED_EVIDENCE_MISSING",
        "candles": report["source"]["price"]["retained_bars"],
        "candidates": report["source"]["signals"]["candidate_decisions"],
        "missing": report["backtest_readiness"]["blocking_reasons"],
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (EvidenceGateError, OSError, TypeError, ValueError) as exc:
        print("CATEGORY30_EVIDENCE_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
