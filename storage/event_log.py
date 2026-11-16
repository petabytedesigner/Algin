"""Fail-closed CSV audit and idempotent projection of durable broker evidence.

CSV is an observable projection, never the authority for trade execution.
Only journal/lifecycle evidence may produce confirmed order/management events.
No network access and no broker mutation are performed by this module.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
from utils.time_tools import iso_utc, parse_utc, utc_now

FIELDS = ("timestamp", "event_id", "correlation_id", "instrument", "direction",
          "stage", "event", "outcome", "reason_code", "attempt_id", "order_id",
          "transaction_id", "trade_id", "detail")
SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
STAGES = frozenset(("SIGNAL", "RISK", "REQUEST", "RESULT", "TRANSACTION", "TRADE",
                    "CLOSE", "BLOCK", "HEARTBEAT", "MANAGEMENT"))
OUTCOMES = frozenset(("ATTEMPT", "SUCCESS", "FAILURE", "UNKNOWN", "BLOCKED", "INFO"))
MAX_BYTES = 64 * 1024 * 1024


class AuditError(RuntimeError):
    """Audit is unavailable, malformed or conflicts with durable evidence."""


def _field(value, name, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str) or (
        value and not SAFE.fullmatch(value)
    ) or (required and not value):
        raise AuditError("Invalid audit " + name)
    return value


class EventLog:
    """Account-independent append-only CSV, exclusive writer and fsynced rows.

    Existing rows are validated before appending. An interrupted/torn tail is
    never silently repaired or overwritten. A deterministic event_id can be
    replayed with emit_many_once without producing duplicate rows.
    """

    def __init__(self, directory="csv"):
        self.directory = Path(directory)
        self.path = self.directory / "events.csv"

    def _row(self, *, event_id, correlation_id, stage, event, outcome,
             reason_code, instrument=None, direction=None, attempt_id=None,
             order_id=None, transaction_id=None, trade_id=None, detail=None,
             timestamp=None):
        if stage not in STAGES or outcome not in OUTCOMES:
            raise AuditError("Invalid audit stage/outcome")
        if direction not in (None, "LONG", "SHORT"):
            raise AuditError("Invalid audit direction")
        try:
            stamp = iso_utc(utc_now() if timestamp is None else timestamp)
        except (TypeError, ValueError) as exc:
            raise AuditError("Invalid audit timestamp") from exc
        return {
            "timestamp": stamp,
            "event_id": _field(event_id, "event_id", required=True),
            "correlation_id": _field(correlation_id, "correlation_id", required=True),
            "instrument": _field(instrument, "instrument"),
            "direction": direction or "",
            "stage": stage,
            "event": _field(event, "event", required=True),
            "outcome": outcome,
            "reason_code": _field(reason_code, "reason_code", required=True),
            "attempt_id": _field(attempt_id, "attempt_id"),
            "order_id": _field(order_id, "order_id"),
            "transaction_id": _field(transaction_id, "transaction_id"),
            "trade_id": _field(trade_id, "trade_id"),
            "detail": _field(detail, "detail"),
        }

    def _existing(self):
        if self.path.is_symlink():
            raise AuditError("Audit symlink forbidden")
        if not self.path.exists():
            return {}
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if not size or size > MAX_BYTES:
                    raise AuditError("Empty/oversized audit file: manual recovery required")
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise AuditError("Torn CSV record: manual recovery required")
            with self.path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream, restkey="_EXTRA", strict=True)
                if reader.fieldnames != list(FIELDS):
                    raise AuditError("Incompatible CSV audit schema")
                events = {}
                for row in reader:
                    if set(row) != set(FIELDS) or any(
                        not isinstance(row[k], str) for k in FIELDS
                    ):
                        raise AuditError("Malformed/truncated CSV record")
                    expected = self._row(
                        **{**row, "timestamp": parse_utc(row["timestamp"]),
                           "direction": row["direction"] or None}
                    )
                    if expected != row:
                        raise AuditError("Malformed CSV audit field")
                    key = row["event_id"]
                    if key in events:
                        raise AuditError("Duplicate event ID in existing audit")
                    events[key] = row
            return events
        except (OSError, UnicodeError, csv.Error, ValueError, TypeError) as exc:
            if isinstance(exc, AuditError):
                raise
            raise AuditError("Unreadable or corrupt audit CSV") from exc

    def _append(self, values, *, idempotent):
        if not values:
            return []
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        try:
            fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise AuditError("Audit log locked/unavailable") from exc
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            existing = self._existing()
            new = []
            for row in values:
                key = row["event_id"]
                prior = existing.get(key)
                if prior is not None:
                    if (not idempotent or any(
                        prior[name] != row[name] for name in FIELDS
                        if name != "timestamp"
                    )):
                        raise AuditError("Conflicting or repeated event ID")
                    continue
                existing[key] = row
                new.append(row)
            if not new:
                return values
            if self.path.exists() and self.path.stat().st_size >= MAX_BYTES:
                raise AuditError("Audit file must be rotated with an approved procedure")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            existed = self.path.exists()
            output_fd = os.open(str(self.path), flags, 0o600)
            with os.fdopen(output_fd, "a", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=FIELDS)
                if not existed:
                    writer.writeheader()
                writer.writerows(new)
                output.flush()
                os.fsync(output.fileno())
            if not existed and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(
                    str(self.directory), os.O_RDONLY | os.O_DIRECTORY
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return values
        except (OSError, UnicodeError, csv.Error) as exc:
            raise AuditError("Audit append/durability failure") from exc
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def emit(self, **fields):
        """Append a unique event; duplicate event IDs are errors."""
        row = self._row(**fields)
        self._append([row], idempotent=False)
        return row

    def emit_once(self, **fields):
        """Idempotent write for an event derived from durable evidence."""
        return self.emit_many_once([fields])[0]

    def emit_many_once(self, events):
        """Batch a journal/lifecycle projection under one exclusive lock."""
        if not isinstance(events, (tuple, list)):
            raise AuditError("Audit events must be an ordered sequence")
        rows = [self._row(**event) for event in events]
        return self._append(rows, idempotent=True)


def _identity_id(account_id, *parts):
    if not isinstance(account_id, str) or not account_id:
        raise AuditError("Account identity is required for event projection")
    payload = "\x1f".join((account_id, *[str(x) for x in parts]))
    return "ev-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _ident(value, label):
    return _field(value, label, required=True)


class AuditProjector:
    """Recover observable events from verified durable journal and lifecycle.

    Snapshot projection can run on every restart. It never treats a CSV line
    as execution evidence and cannot authorize any OANDA mutation. Actual
    SL/TP *cause* is intentionally not inferred from a generic closed trade.
    """

    def __init__(self, account_id, log: EventLog, *, config_module=None):
        self.account_id = _ident(account_id, "account_id")
        if not isinstance(log, EventLog):
            raise AuditError("EventLog instance required")
        self.log = log
        if config_module is None:
            import config
            config_module = config
        self.config = config_module

    def _enabled(self, kind):
        mapping = {
            "REQUEST": "CSV_LOG_TRADES",
            "RESULT": "CSV_LOG_TRADES",
            "TRADE": "CSV_LOG_TRADES",
            "CLOSE": "CSV_LOG_TRADES",
            "BE": "CSV_LOG_BE",
            "PARTIAL": "CSV_LOG_PARTIAL",
            "COOLDOWN": "CSV_LOG_COOLDOWN",
        }
        return bool(getattr(self.config, mapping[kind], False))

    def project(self, attempts, state=None, *, transaction_fetcher=None):
        if not isinstance(attempts, (list, tuple)):
            raise AuditError("Journal snapshot must be a sequence")
        if state is not None and (
            not isinstance(state, dict)
            or state.get("account_id") != self.account_id
            or not isinstance(state.get("trades"), dict)
            or not isinstance(state.get("closed_results"), dict)
        ):
            raise AuditError("Unverified or wrong-account lifecycle state")
        events = []
        identities = {}
        for entry in attempts:
            if not isinstance(entry, dict):
                raise AuditError("Malformed durable journal entry")
            attempt = _ident(entry.get("attempt_id"), "attempt_id")
            signal = _ident(entry.get("signal_id"), "signal_id")
            if attempt in identities:
                raise AuditError("Duplicate journal attempt")
            order = entry.get("request", {}).get("order")
            if not isinstance(order, dict):
                raise AuditError("Missing durable order request")
            instrument = _ident(order.get("instrument"), "instrument")
            try:
                from decimal import Decimal
                units = Decimal(str(order["units"]))
                if not units.is_finite() or units == 0:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise AuditError("Unverifiable order direction")
            direction = "LONG" if units > 0 else "SHORT"
            status = entry.get("status")
            if status not in ("PREPARED", "IN_FLIGHT", "UNKNOWN",
                              "FILLED", "REJECTED", "CANCELLED"):
                raise AuditError("Unknown durable attempt state")
            identities[attempt] = (signal, instrument, direction, entry)
            base = {
                "correlation_id": signal, "instrument": instrument,
                "direction": direction, "attempt_id": attempt,
                "order_id": entry.get("order_id"),
                "trade_id": entry.get("trade_id"),
            }
            if self._enabled("REQUEST"):
                events.append({
                    **base,
                    # Immutable intent evidence cannot acquire future broker IDs.
                    "order_id": None, "trade_id": None,
                    "event_id": _identity_id(self.account_id, attempt, "PREPARED"),
                    "stage": "REQUEST", "event": "INTENT_PREPARED",
                    "outcome": "ATTEMPT", "reason_code": "DURABLE_INTENT",
                    "timestamp": _parse_time(entry.get("created_at")),
                })
            if status != "PREPARED" and self._enabled("RESULT"):
                outcomes = {
                    "IN_FLIGHT": ("UNKNOWN", "IN_FLIGHT_UNRESOLVED"),
                    "UNKNOWN": ("UNKNOWN", "BROKER_OUTCOME_UNKNOWN"),
                    "FILLED": ("SUCCESS", "BROKER_FILL_CONFIRMED"),
                    "REJECTED": ("FAILURE", "BROKER_REJECTED"),
                    "CANCELLED": ("FAILURE", "BROKER_CANCELLED")
                }
                outcome, reason = outcomes[status]
                events.append({
                    **base,
                    # Unconfirmed state must not be retroactively enriched
                    # by IDs only learned from a later broker fill.
                    **({"order_id": None, "trade_id": None}
                       if status in ("IN_FLIGHT", "UNKNOWN") else {}),
                    "event_id": _identity_id(self.account_id, attempt, status),
                    "stage": "RESULT", "event": "ORDER_" + status,
                    "outcome": outcome, "reason_code": reason,
                    "timestamp": _parse_time(entry.get("updated_at")),
                })
            if status == "FILLED" and entry.get("protection_verified") is True and self._enabled("TRADE"):
                if not entry.get("trade_id"):
                    raise AuditError("Verified protection without trade identity")
                events.append({
                    **base,
                    "event_id": _identity_id(self.account_id, attempt, "PROTECTION"),
                    "stage": "TRADE", "event": "PROTECTION_VERIFIED",
                    "outcome": "SUCCESS", "reason_code": "BROKER_GET_CONFIRMED",
                    "timestamp": _parse_time(entry.get("updated_at")),
                })
        if state is not None:
            for trade_id, saved in sorted(state["trades"].items()):
                from risk.trade_lifecycle import TradeRecord
                record = TradeRecord.from_dict(saved)
                if record.trade_id != trade_id or record.attempt_id not in identities:
                    raise AuditError("Lifecycle trade missing journal provenance")
                signal, instrument, direction, entry = identities[record.attempt_id]
                if instrument != record.instrument or direction != record.direction:
                    raise AuditError("Lifecycle/journal instrument or direction disagreement")
                base = {
                    "correlation_id": signal,
                    "instrument": instrument, "direction": direction,
                    "attempt_id": record.attempt_id,
                    "order_id": entry.get("order_id"), "trade_id": trade_id,
                }
                if record.be_state in ("INTENT", "UNKNOWN", "CONFIRMED") and self._enabled("BE"):
                    ev = "BE_" + record.be_state
                    events.append({
                        **base, "event_id": _identity_id(self.account_id, trade_id, ev),
                        "stage": "MANAGEMENT", "event": ev,
                        "outcome": _state_outcome(record.be_state),
                        "reason_code": "LIFECYCLE_STATE_" + record.be_state
                    })
                if self._enabled("PARTIAL"):
                    for phase, action in sorted(record.partial_phases.items()):
                        stage = action["state"]
                        ev = "PARTIAL_" + stage
                        events.append({
                            **base,
                            "event_id": _identity_id(self.account_id, trade_id, phase, ev),
                            "stage": "MANAGEMENT", "event": ev,
                            "outcome": _state_outcome(stage),
                            "reason_code": "LIFECYCLE_STATE_" + stage,
                            "detail": _field(phase, "partial phase", required=True)
                        })
            for trade_id, close in sorted(state["closed_results"].items()):
                if trade_id not in state["trades"]:
                    raise AuditError("Orphan closed result")
                from risk.trade_lifecycle import TradeRecord
                rec = TradeRecord.from_dict(state["trades"][trade_id])
                signal, instrument, direction, entry = identities[rec.attempt_id]
                if not rec.closed or not rec.result_counted or rec.trade_id != trade_id:
                    raise AuditError("Unconfirmed trade closure")
                from decimal import Decimal
                try:
                    net_pl = Decimal(str(close["pl"]))
                    if not net_pl.is_finite() or net_pl != rec.net_pl_home:
                        raise ValueError
                except (ValueError, TypeError, KeyError) as exc:
                    raise AuditError("Unverifiable close result") from exc
                tx = _ident(close.get("transaction_id"), "transaction_id")
                # Optional additional independent GET proof of WHY a trade
                # closed. A generic close alone does not prove SL/TP or manual
                # versus bot-initiated close.
                if transaction_fetcher is not None and any((
                    getattr(self.config, "CSV_LOG_SL_HIT", False),
                    getattr(self.config, "CSV_LOG_TP_HIT", False),
                    getattr(self.config, "CSV_LOG_SYSTEM_CLOSE", False)
                )):
                    if not callable(transaction_fetcher):
                        raise AuditError("Transaction evidence fetcher must be callable")
                    try:
                        evidence = transaction_fetcher(tx)
                    except Exception as exc:
                        raise AuditError("Closing transaction GET unavailable") from exc
                    if not isinstance(evidence, dict) or (
                        evidence.get("id") != tx
                        or evidence.get("accountID") != self.account_id
                        or evidence.get("type") != "ORDER_FILL"
                        or not isinstance(evidence.get("tradesClosed"), list)
                        or not any(
                            isinstance(effect, dict)
                            and effect.get("tradeID") == trade_id
                            for effect in evidence["tradesClosed"]
                        )
                    ):
                        raise AuditError("Close reason cannot be attributed to trade")
                    reason = _ident(evidence.get("reason"), "order fill reason")
                    kind = {
                        "STOP_LOSS_ORDER": ("SL_HIT", "CSV_LOG_SL_HIT"),
                        "GUARANTEED_STOP_LOSS_ORDER": ("SL_HIT", "CSV_LOG_SL_HIT"),
                        "TRAILING_STOP_LOSS_ORDER": ("SL_HIT", "CSV_LOG_SL_HIT"),
                        "TAKE_PROFIT_ORDER": ("TP_HIT", "CSV_LOG_TP_HIT"),
                        "MARKET_ORDER_MARGIN_CLOSEOUT": (
                            "SYSTEM_CLOSE", "CSV_LOG_SYSTEM_CLOSE"
                        ),
                    }.get(reason)
                    if kind is not None and getattr(self.config, kind[1], False):
                        events.append({
                            "event_id": _identity_id(
                                self.account_id, trade_id, "CLOSE_CAUSE", tx
                            ),
                            "correlation_id": signal,
                            "instrument": instrument,
                            "direction": direction,
                            "stage": "CLOSE",
                            "event": kind[0],
                            "outcome": "SUCCESS",
                            "reason_code": reason,
                            "attempt_id": rec.attempt_id,
                            "order_id": entry.get("order_id"),
                            "trade_id": trade_id,
                            "transaction_id": tx,
                            "timestamp": _parse_time(close.get("closed_at")),
                        })
                if self._enabled("CLOSE"):
                    events.append({
                        "event_id": _identity_id(self.account_id, trade_id, "CLOSED", tx),
                        "correlation_id": signal,
                        "instrument": instrument, "direction": direction,
                        "stage": "CLOSE", "event": "TRADE_CLOSED",
                        "outcome": "SUCCESS", "reason_code": "BROKER_CLOSE_RECONCILED",
                        "attempt_id": rec.attempt_id,
                        "order_id": entry.get("order_id"), "trade_id": trade_id,
                        "transaction_id": tx,
                        "timestamp": _parse_time(close.get("closed_at"))
                    })
            if self._enabled("COOLDOWN"):
                # A cooldown is attributed only when the lifecycle contains
                # a confirmed losing close with exactly the recorded expiry.
                from decimal import Decimal
                for instrument, expiry in sorted(
                    state.get("cooldowns", {}).items()
                ):
                    expiry_at = _parse_time(expiry)
                    matches = []
                    for trade_id, close in state["closed_results"].items():
                        if close.get("instrument") != instrument:
                            continue
                        try:
                            amount = Decimal(str(close["pl"]))
                        except (TypeError, ValueError, KeyError) as exc:
                            raise AuditError("Invalid cooldown P/L") from exc
                        if not amount.is_finite():
                            raise AuditError("Invalid cooldown P/L")
                        if amount < 0 and (
                            _parse_time(close["closed_at"]) + timedelta(minutes=30)
                            == expiry_at
                        ):
                            matches.append((trade_id, close))
                    # Multiple losses at the same closing time may each
                    # justify a cooldown; retain their separate trade IDs.
                    for trade_id, close in sorted(matches):
                        rec = TradeRecord.from_dict(state["trades"][trade_id])
                        if rec.attempt_id not in identities:
                            raise AuditError("Unattributed cooldown")
                        signal, _, direction, entry = identities[rec.attempt_id]
                        events.append({
                            "event_id": _identity_id(
                                self.account_id, trade_id, "COOLDOWN", expiry
                            ),
                            "correlation_id": signal,
                            "instrument": instrument,
                            "direction": direction,
                            "stage": "BLOCK",
                            "event": "COOLDOWN_STARTED",
                            "outcome": "BLOCKED",
                            "reason_code": "CONFIRMED_LOSS_COOLDOWN",
                            "attempt_id": rec.attempt_id,
                            "order_id": entry.get("order_id"),
                            "trade_id": trade_id,
                            "transaction_id": close.get("transaction_id"),
                            "timestamp": _parse_time(close.get("closed_at")),
                            "detail": _field(expiry, "cooldown expiry", required=True)
                        })
        return self.log.emit_many_once(events)


def _parse_time(value):
    try:
        return parse_utc(value)
    except (ValueError, TypeError) as exc:
        raise AuditError("Durable event has invalid timestamp") from exc


def _state_outcome(value):
    if value == "CONFIRMED":
        return "SUCCESS"
    if value == "UNKNOWN":
        return "UNKNOWN"
    if value == "INTENT":
        return "ATTEMPT"
    raise AuditError("Invalid lifecycle management state")
