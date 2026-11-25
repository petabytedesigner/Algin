"""Frozen Category 30 candidate-only price-path scenarios (not a backtest).

Uses OANDA historical bid/ask OHLC for **hypothetical** next-bar entries
after 16 fixed candidate signals. Never claims historical broker tradability,
news clearance, fills, account-currency returns or net profitability.
The research policy is ATR-only because separately verified structure stop
boundaries and executable sizing facts are not supplied by these artifacts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
import json
import math
import os
from pathlib import Path
import sys

import config
from analytics.historical_evidence_gate import (
    EvidenceGateError, MONTHS, _utc, inspect_price, inspect_signals,
)
from data.candle_validation import CandleDataError, parse_oanda_candles, validate_candles
from strategy.atr_engine import ATREngine


PIP = Decimal("0.0001")
PRICE_STEP = Decimal("0.00001")
R_STEP = Decimal("0.000001")
SLIPPAGE_LEVELS = (0, 1, 2)


class PricePathError(ValueError):
    """A hypothetical price-path study cannot be proven from these bars."""


def _price(value, label):
    if value is None or isinstance(value, bool):
        raise PricePathError("Missing price: " + label)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PricePathError("Malformed price: " + label) from exc
    if not number.is_finite() or number <= 0:
        raise PricePathError("Nonpositive/nonfinite price: " + label)
    return number


def _ohlc(raw, side):
    member = raw.get(side) if isinstance(raw, dict) else None
    if not isinstance(member, dict):
        raise PricePathError("Missing " + side + " historical OHLC")
    pts = {k: _price(member.get(k), side + "." + k)
           for k in ("o", "h", "l", "c")}
    if not pts["l"] <= pts["o"] <= pts["h"] or not pts["l"] <= pts["c"] <= pts["h"]:
        raise PricePathError("Invalid historical OHLC bounds")
    return pts


def _result(status, **details):
    return {"status": status, **details}


def trace_path(bars, entry_index, *, direction, atr, slippage_pips):
    """Conservative SL-first hypothetical path, no broker fill evidence.

    The planned stop/target are fixed from next-bar executable OPEN, before
    adverse entry slippage. Historical bid/ask includes sampled spread; it
    is not subtracted a second time. Commission/financing are not modelled.
    """
    if (direction not in ("LONG", "SHORT")
        or type(entry_index) is not int or not 0 <= entry_index < len(bars)
        or type(slippage_pips) is not int or slippage_pips not in SLIPPAGE_LEVELS):
        raise PricePathError("Invalid scenario index, side or slippage")
    atr = _price(atr, "closed-bar ATR")
    enter = bars[entry_index]
    if entry_index > 0 and (
        _utc(enter["time"]) - _utc(bars[entry_index - 1]["time"])
        != timedelta(minutes=15)
    ):
        return _result("NO_ENTRY_AT_NEXT_BAR_GAP")
    ask, bid = _ohlc(enter, "ask"), _ohlc(enter, "bid")
    spread = (ask["o"] - bid["o"]) / PIP
    if spread <= 0:
        raise PricePathError("Crossed historical entry quote")
    if spread > Decimal(str(config.MAX_SPREAD_PIPS)):
        return _result("NO_ENTRY_SPREAD_OVER_CAP", spread_pips=str(spread))
    planned_entry = ask["o"] if direction == "LONG" else bid["o"]
    distance = max(
        atr * Decimal(str(config.ATR_STOP_MULTIPLE)),
        PIP * Decimal(str(config.MIN_DISTANCE_PIPS)),
    )
    ratio = Decimal(str(config.RISK_REWARD_RATIO))
    if direction == "LONG":
        stop = (planned_entry - distance).quantize(PRICE_STEP, rounding=ROUND_FLOOR)
        target = (planned_entry + (planned_entry - stop) * ratio).quantize(
            PRICE_STEP, rounding=ROUND_CEILING
        )
    else:
        stop = (planned_entry + distance).quantize(PRICE_STEP, rounding=ROUND_CEILING)
        target = (planned_entry - (stop - planned_entry) * ratio).quantize(
            PRICE_STEP, rounding=ROUND_FLOOR
        )
    if stop <= 0 or target <= 0:
        return _result("NO_ENTRY_INVALID_ATR_DISTANCE")
    adverse = PIP * slippage_pips
    actual_entry = planned_entry + adverse if direction == "LONG" else planned_entry - adverse
    if actual_entry <= 0:
        return _result("NO_ENTRY_INVALID_SLIPPAGE")
    initial_risk = abs(actual_entry - stop)
    if initial_risk <= 0:
        raise PricePathError("Nonpositive hypothetical R denominator")
    prev = None
    for i in range(entry_index, len(bars)):
        bar = bars[i]
        stamp = _utc(bar["time"])
        if prev is not None and stamp - prev != timedelta(minutes=15):
            return _result("UNRESOLVED_PRICE_HISTORY_GAP", observed_bars=i-entry_index)
        prev = stamp
        side = _ohlc(bar, "bid" if direction == "LONG" else "ask")
        # Gap through stop is resolved at the less favorable open. Target is
        # filled at its limit price with no favorable improvement assumed.
        if direction == "LONG":
            if side["o"] <= stop:
                decision, exit_price = "SL_GAP", side["o"]
            elif side["o"] >= target:
                decision, exit_price = "TP", target
            elif side["l"] <= stop:
                decision, exit_price = "SL", stop
            elif side["h"] >= target:
                decision, exit_price = "TP", target
            else:
                continue
            if decision != "TP":
                exit_price -= adverse
            gross_r = (exit_price - actual_entry) / initial_risk
        else:
            if side["o"] >= stop:
                decision, exit_price = "SL_GAP", side["o"]
            elif side["o"] <= target:
                decision, exit_price = "TP", target
            elif side["h"] >= stop:
                decision, exit_price = "SL", stop
            elif side["l"] <= target:
                decision, exit_price = "TP", target
            else:
                continue
            if decision != "TP":
                exit_price += adverse
            gross_r = (actual_entry - exit_price) / initial_risk
        return _result(
            "RESOLVED_HYPOTHETICAL_PRICE_PATH",
            close_reason=decision,
            gross_price_path_r_before_commission_financing=str(gross_r.quantize(R_STEP)),
            bars_observed=i - entry_index + 1,
            entry_spread_pips=str(spread),
        )
    return _result("UNRESOLVED_END_OF_PRICE_HISTORY")


def analyze(payload, monthly_root):
    """Price-only diagnostics for the immutable 16 candidate timestamps."""
    signal_report = inspect_signals(monthly_root)
    mid = parse_oanda_candles(payload, instrument="EUR_USD")
    if len(mid) != len(payload["candles"]):
        raise PricePathError("Historical midpoint normalization changed length")
    index_by_time = {_utc(bar["time"]): i for i, bar in enumerate(payload["candles"])}
    if len(index_by_time) != len(mid):
        raise PricePathError("Duplicate research price bar")
    candidates = []
    for month in MONTHS:
        item = json.loads((Path(monthly_root) / (month + ".json")).read_bytes())
        candidates.extend(item["candidate_details"])
    if len(candidates) != signal_report["candidate_decisions"]:
        raise PricePathError("Candidate count differs from attested signal archive")
    outcomes = []
    for candidate in candidates:
        decision = _utc(candidate["decision_utc"])
        index = index_by_time.get(decision)
        base = {
            "setup_id": candidate["setup_id"],
            "decision_utc": candidate["decision_utc"],
            "direction": candidate["direction"], "grade": candidate["grade"],
        }
        if index is None or index < 50:
            outcomes.append({**base, "scenarios": {
                str(x): _result("NO_NEXT_BAR_OR_INSUFFICIENT_LOOKBACK")
                for x in SLIPPAGE_LEVELS
            }})
            continue
        closed = mid[max(0, index-200):index]
        try:
            window = validate_candles(closed, now=decision, min_count=50)
            atr = ATREngine().atr(window, period=config.ATR_PERIOD)
        except CandleDataError:
            atr = 0.0
        if not math.isfinite(atr) or atr <= 0:
            scenarios = {str(x): _result("NO_ENTRY_UNVERIFIED_CLOSED_HISTORY")
                         for x in SLIPPAGE_LEVELS}
        else:
            scenarios = {str(x): trace_path(
                payload["candles"], index,
                direction=candidate["direction"], atr=atr, slippage_pips=x,
            ) for x in SLIPPAGE_LEVELS}
        outcomes.append({**base, "scenarios": scenarios})
    overview = {}
    for slippage in SLIPPAGE_LEVELS:
        statuses = Counter(x["scenarios"][str(slippage)]["status"] for x in outcomes)
        realized = [Decimal(x["scenarios"][str(slippage)][
            "gross_price_path_r_before_commission_financing"])
            for x in outcomes if x["scenarios"][str(slippage)]["status"]
            == "RESOLVED_HYPOTHETICAL_PRICE_PATH"]
        overview[str(slippage)] = {
            "status_counts": dict(sorted(statuses.items())),
            "resolved_scenario_count": len(realized),
            "aggregate_gross_price_path_r_excluding_unresolved": (
                str(sum(realized).quantize(R_STEP)) if realized else None
            ),
            "net_pnl_calculated": False,
        }
    return {
        "schema": "CATEGORY30_HYPOTHETICAL_PRICE_PATH_V1",
        "research_only": True,
        "source": {
            "signals_run": signal_report["source_run_id"],
            "source_monthly_archive_sha256": signal_report["monthly_archive_sha256"],
            "source_price_sha256": "b48e0f98a2fe948d83afd1d384ae8b23e28b04c1275425c06b543cf75dce5466",
        },
        "analysis_scope": "ATR_ONLY_CANDIDATE_PRICE_PATH_NOT_PRODUCTION_REPLAY",
        "assumptions": {
            "entry": "next M15 bid/ask open if gap-free and spread <= production cap",
            "slippage_pips_entry_and_stop_exit": list(SLIPPAGE_LEVELS),
            "exit_priority": "SL_FIRST_IF_SAME_M15_BAR",
            "stop": "max(1.5*ATR14, 3 pips), rounded to 0.00001",
            "target": "3R from quote-planned entry, not guaranteed broker fill",
            "management": "HOLD_TO_SL_OR_TP_ONLY",
            "tradeability": "UNVERIFIED_HYPOTHETICAL",
            "news": "UNKNOWN_NOT_BYPASSED_FOR_PRODUCTION",
            "commission_and_financing": "NOT_MODELLED_NOT_NET",
            "account_currency_and_exposure": "NOT_MODELLED",
        },
        "candidates": len(outcomes),
        "scenario_overview": overview,
        "outcomes": outcomes,
        "broker_verified": False,
        "realistic_net_pnl_calculated": False,
        "statistically_sufficient": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--signals-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    inspect_price(args.dataset, args.manifest)
    price = json.loads(Path(args.dataset).read_bytes())
    report = analyze(price, args.signals_dir)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise PricePathError("Price-path report cannot overwrite existing data")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
        file.write(encoded)
        file.flush()
        os.fsync(file.fileno())
    print("CATEGORY30_CANDIDATE_PATHS " + json.dumps({
        "candidates": report["candidates"],
        "scenarios": report["scenario_overview"],
        "broker_verified": False,
        "net_pnl_calculated": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (PricePathError, EvidenceGateError, CandleDataError, OSError,
            ValueError, TypeError) as exc:
        print("CATEGORY30_CANDIDATE_PATHS_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
