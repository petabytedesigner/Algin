"""Category 27: bounded, explicit, read-only decision evidence.

Separate from the immutable Category-6 CSV schema and from the authoritative
broker intent journal. Never store arbitrary broker/strategy payloads or keys.
No trace is authorization to execute a trade.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from utils.time_tools import iso_utc, parse_utc


class TraceError(RuntimeError):
    """Decision evidence is invalid, inconsistent or not durable."""


_IDS = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_REASONS = re.compile(r"[A-Z0-9_]{1,64}\Z", re.ASCII)
_INSTRUMENT = re.compile(r"[A-Z0-9]{3,8}_[A-Z0-9]{3,8}\Z", re.ASCII)
_INPUTS = frozenset(("ema_fast", "ema_slow", "atr", "close", "bid",
                     "ask", "spread_pips"))
_METRICS = frozenset(("trend_strength", "structure_quality", "retest_quality",
                      "candle_quality", "ema_alignment", "atr_normalized",
                      "pattern_quality", "in_reset_zone"))
_POLICY = frozenset((
    "ema_fast_period", "ema_slow_period", "atr_period", "max_retest_bars",
    "grade_a_min", "grade_b_min", "grade_c_min", "max_spread_pips",
    "risk_grade_a_pct", "risk_grade_b_pct", "max_total_risk_pct",
    "max_daily_loss_pct", "max_drawdown_pct", "min_distance_pips",
))
_FIELDS = frozenset((
    "cycle_id", "instrument", "direction", "candle_time", "timestamp",
    "decision", "reason_code", "strategy_version", "inputs", "metrics",
    "policy", "risk_status", "grade", "score", "signal_id", "setup_id",
    "attempt_id", "trade_id", "transaction_id",
))
_ZERO = "0" * 64
MAX_BYTES = 64 * 1024 * 1024


def _id(value, key, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or _IDS.fullmatch(value) is None:
        raise TraceError("Invalid decision identity " + key)
    return value


def _number(value, key, *, unit_interval=False, positive=False):
    if isinstance(value, bool):
        raise TraceError("Invalid decision number " + key)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TraceError("Invalid decision number " + key) from exc
    if (not number.is_finite() or (positive and number <= 0)
        or (not positive and number < 0)
        or (unit_interval and number > 1)):
        raise TraceError("Out-of-range decision number " + key)
    return str(number)


def _numeric_map(value, names, key, *, unit_interval=False):
    if not isinstance(value, dict) or set(value) - names:
        raise TraceError("Unapproved decision field " + key)
    result = {}
    for name, item in value.items():
        if name == "in_reset_zone":
            if type(item) is not bool:
                raise TraceError("Invalid reset-zone decision flag")
            result[name] = item
        else:
            result[name] = _number(
                item, key + "." + name,
                unit_interval=unit_interval,
                positive=(key == "inputs" and name not in ("spread_pips",)),
            )
    return dict(sorted(result.items()))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _payload(data):
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise TraceError("Unknown or missing decision fields")
    cycle = _id(data["cycle_id"], "cycle_id")
    instrument = data["instrument"]
    if not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument):
        raise TraceError("Invalid instrument")
    direction = data["direction"]
    if direction not in ("LONG", "SHORT"):
        raise TraceError("Invalid direction")
    try:
        stamp = iso_utc(parse_utc(data["timestamp"]) if isinstance(data["timestamp"], str)
                        else data["timestamp"])
        candle = iso_utc(parse_utc(data["candle_time"]))
    except (TypeError, ValueError) as exc:
        raise TraceError("Invalid decision UTC time") from exc
    decision = data["decision"]
    if decision not in ("REJECTED", "OBSERVED"):
        raise TraceError("Invalid decision state")
    reason = data["reason_code"]
    if not isinstance(reason, str) or not _REASONS.fullmatch(reason):
        raise TraceError("Invalid decision reason")
    version = _id(data["strategy_version"], "strategy_version")
    if data["risk_status"] != "NOT_EVALUATED_UNRECONCILED":
        raise TraceError("Decision trace cannot claim risk approval")
    grade = data["grade"]
    if grade not in (None, "A", "B", "C", "D"):
        raise TraceError("Invalid decision grade")
    score = (None if data["score"] is None else _number(data["score"], "score"))
    if score is not None and Decimal(score) > 100:
        raise TraceError("Score outside 0..100")
    if decision == "OBSERVED" and (grade not in ("A", "B")
                                   or score is None or reason != "OBSERVE_ONLY"):
        raise TraceError("Unverified observed signal")
    if decision == "REJECTED" and reason == "OBSERVE_ONLY":
        raise TraceError("Inconsistent rejection")
    ids = {key: _id(data[key], key, optional=True) for key in (
        "signal_id", "setup_id", "attempt_id", "trade_id", "transaction_id"
    )}
    # This is pre-risk, read-only evidence. Transaction and execution IDs
    # belong to the verified intent journal/lifecycle, never to a claim
    # supplied by the signal scanner.
    if any(ids[key] is not None for key in
           ("attempt_id", "trade_id", "transaction_id")):
        raise TraceError("Execution identities require broker verification")
    if ids["setup_id"] is not None and not re.fullmatch(
        r"algin-setup-[0-9a-f]{40}", ids["setup_id"]
    ):
        raise TraceError("Invalid stable setup identity")
    if ids["signal_id"] is not None and not re.fullmatch(
        r"algin-signal-[0-9a-f]{40}", ids["signal_id"]
    ):
        raise TraceError("Invalid stable signal identity")
    return {
        "version": 1,
        "trace_id": "decision-" + hashlib.sha256(
            _canonical([cycle, instrument, direction, candle])
        ).hexdigest()[:40],
        "cycle_id": cycle, "instrument": instrument, "direction": direction,
        "timestamp": stamp, "candle_time": candle,
        "decision": decision, "reason_code": reason,
        "strategy_version": version,
        "inputs": _numeric_map(data["inputs"], _INPUTS, "inputs"),
        "metrics": _numeric_map(data["metrics"], _METRICS, "metrics", unit_interval=True),
        "policy": _numeric_map(data["policy"], _POLICY, "policy"),
        "risk_status": "NOT_EVALUATED_UNRECONCILED",
        "grade": grade, "score": score, **ids,
    }


class DecisionTrace:
    """Fsynced JSONL; each row checksummed and linked to its predecessor."""

    def __init__(self, directory="csv"):
        self.directory = Path(directory)
        self.path = self.directory / "decision_trace.jsonl"
        self.lock = self.directory / "decision_trace.lock"

    def _read(self):
        if self.path.is_symlink():
            raise TraceError("Trace symlink forbidden")
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_bytes()
            if not raw or len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
                raise TraceError("Incomplete/oversized decision trace")
            rows = []
            prior = _ZERO
            seen = set()
            for line in raw.splitlines():
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != (_FIELDS - {"timestamp"}) | {
                    "version", "trace_id", "timestamp", "previous", "checksum"
                }:
                    raise TraceError("Malformed decision trace row")
                check = row.pop("checksum")
                if row["previous"] != prior or not isinstance(check, str) or (
                    hashlib.sha256(_canonical(row)).hexdigest() != check
                ):
                    raise TraceError("Decision trace integrity mismatch")
                # Stored values are independently revalidated on read.
                payload = _payload({k: row[k] for k in _FIELDS})
                if any(row[k] != payload[k] for k in payload):
                    raise TraceError("Decision trace validation mismatch")
                if row["trace_id"] in seen:
                    raise TraceError("Duplicate decision trace identity")
                seen.add(row["trace_id"])
                row["checksum"] = check
                rows.append(row)
                prior = check
            return rows
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, TraceError):
                raise
            raise TraceError("Unreadable decision trace") from exc

    def read_all(self):
        """Read-only forensic verification. No source is silently repaired."""
        return tuple(self._read())

    def append(self, **data):
        payload = _payload(data)  # Allowlist and finiteness before any write.
        if self.directory.is_symlink():
            raise TraceError("Trace directory symlink forbidden")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise TraceError("Trace writer already active or unavailable") from exc
        lock_owner = secrets.token_hex(24)
        lock_ready = False
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(lock_owner)
                handle.flush()
                os.fsync(handle.fileno())
            lock_ready = True
            rows = self._read()
            for row in rows:
                if row["trace_id"] == payload["trace_id"]:
                    if all(row[k] == payload[k] for k in payload):
                        return dict(row)
                    raise TraceError("Conflicting decision for the same candle/cycle")
            previous = rows[-1]["checksum"] if rows else _ZERO
            new = {**payload, "previous": previous}
            new["checksum"] = hashlib.sha256(_canonical(new)).hexdigest()
            line = _canonical(new) + b"\n"
            if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                raise TraceError("Decision trace rotation required")
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            existed = self.path.exists()
            with os.fdopen(os.open(str(self.path), flags, 0o600), "ab") as output:
                output.write(line)
                output.flush()
                os.fsync(output.fileno())
            if not existed and hasattr(os, "O_DIRECTORY"):
                folder = os.open(str(self.directory), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(folder)
                finally:
                    os.close(folder)
            return dict(new)
        except OSError as exc:
            raise TraceError("Decision trace durability failure") from exc
        finally:
            # If lock ownership cannot be verified, never unlink it blindly.
            if lock_ready:
                try:
                    if self.lock.is_symlink() or self.lock.read_text(encoding="ascii") != lock_owner:
                        raise TraceError("Decision trace lock ownership changed")
                    self.lock.unlink()
                except OSError as exc:
                    raise TraceError("Unable to release decision trace lock") from exc
