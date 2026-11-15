"""Category-6 append-only audit events with a single stable CSV schema.

No raw broker request/response payloads, tokens, arbitrary notes or stack traces
are accepted. Persist each event once using deterministic IDs and fsync; a
conflicting ID, corrupt CSV, or concurrent/stale writer stops recording.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Optional
import config
from utils.time_tools import iso_utc, parse_utc, utc_now


class AuditError(RuntimeError):
    """Audit history cannot be trusted: fail closed; never silently repair."""


FIELDS = (
    "schema", "event_id", "utc_time", "account_ref", "cycle_id",
    "signal_id", "attempt_id", "order_id", "transaction_id", "trade_id",
    "instrument", "candle_time", "event_type", "stage", "outcome",
    "reason_code", "direction", "grade", "requested_price", "executed_price",
    "units", "risk_pct", "spread_pips", "slippage_pips"
)
EVENT_FLAGS = {
    "SIGNAL": "CSV_LOG_SIGNALS",
    "SIGNAL_BLOCK": "CSV_LOG_SIGNALS",
    "RISK": "CSV_LOG_SIGNALS",
    "ORDER_ATTEMPT": "CSV_LOG_TRADES",
    "ORDER_RESULT": "CSV_LOG_TRADES",
    "TRADE": "CSV_LOG_TRADES",
    "CLOSE": "CSV_LOG_TRADES",
    "PARTIAL": "CSV_LOG_PARTIAL",
    "BE": "CSV_LOG_BE",
    "NEWS_BLOCK": "CSV_LOG_NEWS_BLOCK",
    "SESSION_BLOCK": "CSV_LOG_SESSION_BLOCK",
    "COOLDOWN": "CSV_LOG_COOLDOWN",
    "SL_HIT": "CSV_LOG_SL_HIT",
    "TP_HIT": "CSV_LOG_TP_HIT",
    "MANUAL_CLOSE": "CSV_LOG_MANUAL_CLOSE",
    "SYSTEM_CLOSE": "CSV_LOG_SYSTEM_CLOSE",
}
OUTCOMES = {
    "OBSERVED", "BLOCKED", "SKIPPED", "PREPARED", "IN_FLIGHT",
    "UNKNOWN", "FILLED", "REJECTED", "CANCELLED", "CONFIRMED",
    "VERIFIED", "UNVERIFIED", "CLOSED"
}
STAGES = {
    "FEED", "SESSION", "NEWS", "STRATEGY", "SCORING", "RISK",
    "PREFLIGHT", "BROKER", "RECONCILIATION", "MANAGEMENT", "ACCOUNTING"
}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
INSTRUMENT = re.compile(r"^[A-Z0-9]{3,8}_[A-Z0-9]{3,8}$")
NUMERIC_FIELDS = {"requested_price", "executed_price", "units",
                  "risk_pct", "spread_pips", "slippage_pips"}


def event_id(*values):
    """Stable privacy-preserving deduplication key for a logical event."""
    if not values or not all(
        isinstance(x, str) and x and len(x) <= 256 for x in values
    ):
        raise AuditError("Missing event identity components")
    return "ev-" + hashlib.sha256("\x1f".join(values).encode()).hexdigest()[:40]


def _clean_numeric(value, key):
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        raise AuditError(f"Boolean is not a {key} number")
    try:
        n = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise AuditError(f"Invalid {key}") from exc
    if not n.is_finite():
        raise AuditError(f"Non-finite {key}")
    return format(n, "f")


def _safe_row(row):
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        raise AuditError("Unrecognized event schema")
    result = {}
    for key in FIELDS:
        raw = row[key]
        if key in NUMERIC_FIELDS:
            result[key] = _clean_numeric(raw, key)
            continue
        if raw in (None, ""):
            result[key] = ""
        elif not isinstance(raw, str):
            raise AuditError(f"Non-string identifier: {key}")
        else:
            result[key] = raw
    if result["schema"] != "1":
        raise AuditError("Unsupported audit schema")
    for key in ("event_id", "account_ref"):
        if not ID.fullmatch(result[key]):
            raise AuditError(f"Invalid {key}")
    for key in ("cycle_id", "signal_id", "attempt_id", "order_id",
                "transaction_id", "trade_id"):
        if result[key] and not ID.fullmatch(result[key]):
            raise AuditError(f"Unsafe {key}")
    for key in ("utc_time", "candle_time"):
        if key == "utc_time" or result[key]:
            try:
                parse_utc(result[key])
            except (ValueError, TypeError) as exc:
                raise AuditError(f"Invalid UTC timestamp: {key}") from exc
    if result["instrument"] and not INSTRUMENT.fullmatch(result["instrument"]):
        raise AuditError("Invalid instrument")
    if result["event_type"] not in EVENT_FLAGS:
        raise AuditError("Unrecognized event type")
    if result["stage"] not in STAGES or result["outcome"] not in OUTCOMES:
        raise AuditError("Invalid stage or outcome")
    if result["reason_code"] and not CODE.fullmatch(result["reason_code"]):
        raise AuditError("Reason must be a fixed code, not raw error text")
    if result["direction"] not in ("", "LONG", "SHORT"):
        raise AuditError("Invalid direction")
    if result["grade"] not in ("", "A", "B", "C", "D"):
        raise AuditError("Invalid grade")
    return result


class AuditEvents:
    """Append-only, local, account-separated CSV with monotonic event IDs."""

    def __init__(self, account_id: str, *, directory: Optional[str] = None):
        if not isinstance(account_id, str) or not ID.fullmatch(account_id):
            raise AuditError("Invalid account identity")
        self.account_ref = hashlib.sha256(account_id.encode()).hexdigest()[:20]
        root = Path(config.CSV_PATH if directory is None else directory)
        self.path = root / ("events_" + self.account_ref + ".csv")
        self.lock = root / ("events_" + self.account_ref + ".lock")

    def _read_unlocked(self):
        if self.path.is_symlink():
            raise AuditError("Symlink CSV rejected")
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != FIELDS:
                    raise AuditError("Audit CSV header mismatch")
                result = []
                unique = set()
                for item in reader:
                    if None in item or any(value is None for value in item.values()):
                        raise AuditError("Truncated audit CSV row")
                    row = _safe_row(item)
                    if row["account_ref"] != self.account_ref:
                        raise AuditError("Cross-account audit history")
                    if row["event_id"] in unique:
                        raise AuditError("Duplicate historical audit event")
                    unique.add(row["event_id"])
                    result.append(row)
                return result
        except (OSError, UnicodeError, csv.Error) as exc:
            raise AuditError("Unreadable or malformed CSV") from exc

    def read(self):
        # Caller must not mistake a stale lock for a consistent full snapshot.
        if self.lock.exists() or self.lock.is_symlink():
            raise AuditError("Concurrent/stale audit writer")
        return self._read_unlocked()

    def emit(self, kind, *, eid, stage, outcome, reason_code="",
             cycle_id="", signal_id="", attempt_id="", order_id="",
             transaction_id="", trade_id="", instrument="", candle_time="",
             direction="", grade="", requested_price=None,
             executed_price=None, units=None, risk_pct=None,
             spread_pips=None, slippage_pips=None, at=None):
        if kind not in EVENT_FLAGS:
            raise AuditError("Unrecognized audit event")
        if (not config.CSV_ENABLED
            or not getattr(config, EVENT_FLAGS[kind], False)):
            return False
        row = _safe_row({
            "schema": "1", "event_id": eid,
            "utc_time": iso_utc(utc_now() if at is None else at),
            "account_ref": self.account_ref, "cycle_id": cycle_id,
            "signal_id": signal_id, "attempt_id": attempt_id,
            "order_id": order_id, "transaction_id": transaction_id,
            "trade_id": trade_id, "instrument": instrument,
            "candle_time": candle_time, "event_type": kind,
            "stage": stage, "outcome": outcome, "reason_code": reason_code,
            "direction": direction, "grade": grade,
            "requested_price": requested_price,
            "executed_price": executed_price, "units": units,
            "risk_pct": risk_pct, "spread_pips": spread_pips,
            "slippage_pips": slippage_pips,
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(self.lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise AuditError("Audit CSV write lock unavailable") from exc
        try:
            with os.fdopen(fd, "w", encoding="ascii") as lock:
                lock.write(str(os.getpid()))
                lock.flush()
                os.fsync(lock.fileno())
            previous = self._read_unlocked()
            found = next((r for r in previous if r["event_id"] == eid), None)
            if found is not None:
                # Replay is permitted only as the exact original event.
                comparison = dict(row)
                comparison["utc_time"] = found["utc_time"]
                if found != comparison:
                    raise AuditError("Conflicting event ID: audit tampering/duplicate")
                return False
            new = not self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
                if new:
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            if new and hasattr(os, "O_DIRECTORY"):
                d = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(d)
                finally:
                    os.close(d)
            return True
        except OSError as exc:
            raise AuditError("Audit event write failed; operation blocked") from exc
        finally:
            try:
                self.lock.unlink()
            except OSError:
                pass
