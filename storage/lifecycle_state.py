"""Crash-safe account state and a lifetime single-process lease (Category 4).

Files are account/environment-scoped, checksummed, fsynced, atomically replaced.
An orphaned lease is deliberately NOT deleted on restart: reconciliation and
operator intervention are required before another process can own the account.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Callable, Optional

from utils.time_tools import iso_utc, utc_now


class StateError(RuntimeError):
    """Unknown, corrupt, mismatched or concurrently modified lifecycle state."""


_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TRADE = re.compile(r"^[0-9]{1,50}$")


def _identity(value: str, label: str):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StateError(f"Invalid {label}")
    return value


def _canonical(data):
    return json.dumps(data, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _digest(data):
    return hashlib.sha256(_canonical(data)).hexdigest()


def _durable_replace(path: Path, blob: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(12) + ".tmp")
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise StateError("Refusing symlink lifecycle replacement")
        os.replace(temp, path)
        if hasattr(os, "O_DIRECTORY"):
            fd_dir = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd_dir)
            finally:
                os.close(fd_dir)
    except OSError as exc:
        raise StateError("Unable to persist lifecycle state durably") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class ProcessLease:
    """Keep this lease held over the *whole* management process lifetime."""

    def __init__(self, account_id, environment="practice", *, directory=".local"):
        self.account_id = _identity(account_id, "account ID")
        self.environment = _identity(environment, "environment")
        self.path = (Path(directory) / "locks" /
                     (self.environment + "." + self.account_id + ".lease"))
        self.token = None

    def acquire(self):
        if self.token is not None:
            raise StateError("Process lease already acquired")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = secrets.token_hex(24)
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise StateError("Account lease exists: inspect stale process and reconcile first") from exc
        except OSError as exc:
            raise StateError("Cannot acquire account lease") from exc
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.token = token
            return self
        except Exception:
            # If durability was uncertain, preserve the lease to block entry.
            raise

    def release(self):
        if self.token is None:
            raise StateError("No account lease is owned")
        try:
            if self.path.is_symlink() or self.path.read_text(encoding="ascii").strip() != self.token:
                raise StateError("Account lease owner changed: manual recovery required")
            self.path.unlink()
            self.token = None
        except OSError as exc:
            raise StateError("Cannot safely release account lease") from exc

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class LifecycleStore:
    VERSION = 1

    def __init__(self, account_id, environment="practice", *, directory=".local"):
        self.account_id = _identity(account_id, "account ID")
        self.environment = _identity(environment, "environment")
        root = Path(directory) / "lifecycle"
        self.path = root / (environment + "." + account_id + ".json")
        self.lock_path = root / (environment + "." + account_id + ".write.lock")

    @contextmanager
    def _exclusive(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise StateError("Concurrent lifecycle writer or orphaned lock") from exc
        owner = secrets.token_hex(24)
        # A write failure after creating the lock deliberately leaves an
        # orphan: it must never be treated as a completed acquisition.
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(owner + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            yield
        finally:
            # Never unlink a replacement lock belonging to another writer.
            try:
                if (self.lock_path.is_symlink()
                    or self.lock_path.read_text(encoding="ascii").strip() != owner):
                    raise StateError("Lifecycle lock owner changed; manual recovery required")
                self.lock_path.unlink()
            except OSError as exc:
                raise StateError("Cannot safely release lifecycle lock") from exc

    def _validate(self, data):
        legacy_fields = {
            "version", "account_id", "environment", "cursor",
            "trades", "processed_transactions", "counted_closures",
            "closed_results", "cooldowns"
        }
        if (not isinstance(data, dict) or
            set(data) not in (legacy_fields, legacy_fields | {"financial_ledger"})):
            raise StateError("Unknown lifecycle schema")
        if (data["version"] != self.VERSION or data["account_id"] != self.account_id
            or data["environment"] != self.environment):
            raise StateError("Wrong account/environment or incompatible lifecycle version")
        if not isinstance(data["cursor"], str) or not _TRADE.fullmatch(data["cursor"]):
            raise StateError("Invalid transaction cursor")
        if not isinstance(data["trades"], dict):
            raise StateError("Invalid trade dictionary")
        if not all(isinstance(x, list) for x in (
            data["processed_transactions"], data["counted_closures"]
        )):
            raise StateError("Invalid transaction history")
        for key in ("processed_transactions", "counted_closures"):
            values = data[key]
            if len(set(values)) != len(values) or not all(
                isinstance(x, str) and _TRADE.fullmatch(x) for x in values
            ):
                raise StateError("Invalid or duplicated transaction IDs")
            # Transaction IDs are monotonic (not necessarily consecutive).
            # A snapshot must never claim evidence newer than its cursor.
            numeric = [int(x) for x in values]
            if (any(a >= b for a, b in zip(numeric, numeric[1:]))
                or any(x > int(data["cursor"]) for x in numeric)):
                raise StateError("Transaction history/cursor disagreement")
        if not set(data["counted_closures"]).issubset(
            data["processed_transactions"]
        ):
            raise StateError("Counted closure has no processed transaction")
        if "financial_ledger" in data:
            from broker.financial_ledger import (
                FinancialEvidenceError, validate_ledger
            )
            try:
                validate_ledger(
                    data["financial_ledger"], cursor=data["cursor"],
                    processed=data["processed_transactions"],
                )
            except FinancialEvidenceError as exc:
                raise StateError("Financial evidence/cursor mismatch") from exc
        if not isinstance(data["closed_results"], dict) or not isinstance(data["cooldowns"], dict):
            raise StateError("Invalid outcome/cooldown history")
        for trade_id, outcome in data["closed_results"].items():
            if not _TRADE.fullmatch(trade_id) or not isinstance(outcome, dict):
                raise StateError("Invalid completed result identity")
            if set(outcome) != {"transaction_id", "pl", "closed_at", "instrument"}:
                raise StateError("Incomplete completed result")
            if not _TRADE.fullmatch(outcome["transaction_id"]):
                raise StateError("Invalid closing transaction identity")
            from risk.trade_plan import number
            number(outcome["pl"], "realized result")
            from utils.time_tools import parse_utc
            parse_utc(outcome["closed_at"])
        for instrument, timestamp in data["cooldowns"].items():
            if not isinstance(instrument, str) or not instrument:
                raise StateError("Invalid cooldown instrument")
            from utils.time_tools import parse_utc
            parse_utc(timestamp)
        for trade_id, result in data["closed_results"].items():
            if trade_id not in data["trades"]:
                raise StateError("Orphan trade result")
            from risk.trade_lifecycle import TradeRecord
            record = TradeRecord.from_dict(data["trades"][trade_id])
            if (not record.closed or not record.result_counted
                or result["transaction_id"] not in data["counted_closures"]
                or result["transaction_id"] not in data["processed_transactions"]
                or result["transaction_id"] not in record.close_transaction_ids
                or str(record.net_pl_home) != str(result["pl"])
                or record.instrument != result["instrument"]):
                raise StateError("Outcome ledger disagrees with trade")
        for key, record in data["trades"].items():
            if not isinstance(key, str) or not _TRADE.fullmatch(key):
                raise StateError("Invalid local trade ID")
            # Full record validation, including phase and numerical invariants.
            from risk.trade_lifecycle import TradeRecord
            trade = TradeRecord.from_dict(record)
            if trade.trade_id != key:
                raise StateError("Trade record identity mismatch")
        return data

    def load(self):
        # A broken symlink looks absent to exists(), but must block recovery.
        if self.path.is_symlink():
            raise StateError("Symlink lifecycle file forbidden")
        if not self.path.exists():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"data", "sha256"}:
                raise StateError("Malformed lifecycle envelope")
            state = envelope["data"]
            if not isinstance(envelope["sha256"], str) or (
                envelope["sha256"] != _digest(state)
            ):
                raise StateError("Lifecycle checksum mismatch")
            return copy.deepcopy(self._validate(state))
        except (OSError, ValueError, TypeError) as exc:
            raise StateError("Unreadable/corrupt lifecycle file") from exc

    def _write(self, state):
        self._validate(state)
        _durable_replace(self.path, _canonical({
            "data": state, "sha256": _digest(state)
        }))

    def initialize(self, cursor, trades=None):
        if not isinstance(cursor, str) or not _TRADE.fullmatch(cursor):
            raise StateError("Cannot bootstrap without valid broker cursor")
        from broker.financial_ledger import initial_ledger
        initial = {
            "version": self.VERSION, "account_id": self.account_id,
            "environment": self.environment, "cursor": cursor,
            "trades": trades if trades is not None else {},
            "processed_transactions": [], "counted_closures": [],
            "closed_results": {}, "cooldowns": {},
            "financial_ledger": initial_ledger(cursor),
        }
        with self._exclusive():
            if self.path.exists() or self.path.is_symlink():
                raise StateError("Lifecycle already exists; cannot overwrite state")
            self._write(initial)
        return copy.deepcopy(initial)

    def mutate(self, operation: Callable[[dict], None], *, expected_cursor=None):
        """Validate and commit one complete state transition; no partial files."""
        with self._exclusive():
            current = self.load()
            if current is None:
                raise StateError("Lifecycle missing: explicit initial reconcile required")
            if expected_cursor is not None and current["cursor"] != expected_cursor:
                raise StateError("Cursor changed concurrently")
            proposed = copy.deepcopy(current)
            operation(proposed)
            self._write(proposed)
            return copy.deepcopy(proposed)
