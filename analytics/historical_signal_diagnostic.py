"""Read-only Category 30 M15 signal-candidate diagnostic.

This deliberately DOES NOT backtest trades or assert economic-calendar,
historical broker-tradeability, position sizing, costs, or profitability.
It scans the frozen, SHA-256-attested OANDA M15 export using production
candle/indicator/Strategy/session rules and writes monthly progress records.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys

import config
from data.candle_validation import CandleDataError, parse_oanda_candles, validate_candles
from data.market_sessions import MarketSessions
from strategy.strategy import Strategy
from utils.time_tools import parse_utc


class DiagnosticError(ValueError):
    """An input or its provenance cannot be used for this offline diagnostic."""


def load_attested_export(path, expected_sha256):
    """Read an extracted JSON member, never trust its filename or status alone."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 30_000_000:
        raise DiagnosticError("Missing/unsafe historical JSON export")
    if (len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise DiagnosticError("Explicit lower-case SHA-256 required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise DiagnosticError("Historical source checksum mismatch")
    try:
        source = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DiagnosticError("Historical export is not JSON") from exc
    if (not isinstance(source, dict)
        or source.get("source") != "OANDA_PRACTICE_V20_GET"
        or source.get("instrument") != "EUR_USD"
        or source.get("granularity") != "M15"
        or source.get("price") != "MBA"
        or source.get("validation_status") != "UNVERIFIED_NOT_BACKTEST_APPROVED"
        or not isinstance(source.get("gaps"), list)
        or not isinstance(source.get("candles"), list)
        or not 50 <= len(source["candles"]) <= 100000):
        raise DiagnosticError("Unexpected/uncertified research source shape")
    return source


def scan_months(source, *, year, month=None, emit=None):
    """Return monthly strategy *candidates*, never executed/broker-approved trades.

    All windows use only candles closed by the historical decision timestamp.
    Monthly checkpointing does not reset the 200-bar indicator lookback.
    """
    if type(year) is not int or not 2023 <= year <= 2026:
        raise DiagnosticError("Unsupported research year")
    if month is not None and (type(month) is not int or not 1 <= month <= 12):
        raise DiagnosticError("Invalid month")
    if (not isinstance(source, dict)
        or source.get("instrument") != "EUR_USD"
        or source.get("granularity") != "M15"):
        raise DiagnosticError("Wrong research source")
    try:
        candles = parse_oanda_candles(source, instrument="EUR_USD")
    except CandleDataError as exc:
        raise DiagnosticError("M15 source candle normalization failed") from exc
    strategy = Strategy()
    sessions = MarketSessions()
    totals = {}
    next_checkpoint = 1

    def monthly_row(m):
        key = f"{year:04d}-{m:02d}"
        return {
            "month": key,
            "status": "RESEARCH_SIGNAL_CANDIDATES_ONLY",
            "historical_news": "UNKNOWN_NOT_BYPASSED",
            "historical_broker_tradeability": "UNKNOWN",
            "trades_executed": 0,
            "profit_calculated": False,
            **dict(sorted(totals.get(key, Counter()).items())),
        }

    for i in range(49, len(candles) - 1):
        current = candles[i]
        at = parse_utc(current["time"]) + timedelta(minutes=15)
        if at.year != year:
            continue
        if month is None:
            while next_checkpoint < at.month:
                row = monthly_row(next_checkpoint)
                if emit is not None:
                    emit(row)
                yield row
                next_checkpoint += 1
        if month is not None and at.month != month:
            continue
        key = f"{at.year:04d}-{at.month:02d}"
        if key not in totals:
            totals[key] = Counter()
        stat = totals[key]
        stat["decision_bars"] += 1
        window = candles[max(0, i - 199):i + 1]
        try:
            window = validate_candles(window, now=at, min_count=50)
        except CandleDataError:
            stat["candle_rejected"] += 1
            continue
        # This is only the schedule part of the runtime gate. Historic
        # broker-tradeable status is UNKNOWN and not asserted as True.
        if not sessions.market_week_open(at) or (
            config.ENABLE_SESSIONS and sessions.active_session(at) is None
        ):
            stat["outside_scheduled_sessions"] += 1
            continue
        stat["schedule_eligible_bars"] += 1
        closes = [c["close"] for c in window]
        fast = strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = strategy.atr_engine.atr(window, period=config.ATR_PERIOD)
        if not atr or atr <= 0:
            stat["indicators_unavailable"] += 1
            continue
        low = min(c["low"] for c in window[:-1][-10:])
        high = max(c["high"] for c in window[:-1][-10:])
        candidates = []
        for direction in ("LONG", "SHORT"):
            found = strategy.evaluate_signal(
                "EUR_USD", direction, window, fast, slow,
                atr, low, high, at,
            )
            if found is not None:
                candidates.append(found)
                stat["raw_" + direction.lower() + "_candidates"] += 1
        if candidates:
            selected = max(candidates, key=lambda s: s.score)
            stat["candidate_decisions"] += 1
            stat["grade_" + selected.grade] += 1

    # Emit all months even if no candidate exists; this makes missing
    # months auditable and makes a failed job's completed months recoverable.
    end_month = 10 if year == 2026 else 12
    for m in range(next_checkpoint, end_month + 1):
        if month is not None and month != m:
            continue
        row = monthly_row(m)
        if emit is not None:
            emit(row)
        yield row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    source = load_attested_export(args.dataset, args.sha256)
    output = Path(args.output_dir)
    if output.is_symlink():
        raise DiagnosticError("Unsafe report output path")
    output.mkdir(parents=True, exist_ok=True)

    def checkpoint(item):
        path = output / (item["month"] + ".json")
        if path.exists() or path.is_symlink():
            raise DiagnosticError("Monthly checkpoint already exists")
        item["dataset_sha256"] = args.sha256
        path.write_text(json.dumps(item, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print("CATEGORY30_MONTH " + json.dumps(item, sort_keys=True), flush=True)

    for _ in scan_months(source, year=args.year, month=args.month, emit=checkpoint):
        pass
    print("CATEGORY30_DIAGNOSTIC_COMPLETE year=" + str(args.year), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (DiagnosticError, OSError) as exc:
        print("CATEGORY30_DIAGNOSTIC_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
