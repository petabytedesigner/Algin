"""Crash-safe Category-3 intent ledger. Does not authorize broker submissions.

A record is fsynced before the first network mutation. Unique signal IDs
cannot be reused. Locked/partial/corrupt or different-account state fails closed.
A stale lock is NEVER auto-removed; the operator must inspect it and reconcile.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Optional

from utils.time_tools import iso_utc, parse_utc, utc_now


class JournalError(RuntimeError):
    """Persistence integrity, duplicate intent or conflicting process."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TERMINAL = frozenset({"REJECTED", "CANCELLED"})
_STATES = frozenset({"PREPARED", "IN_FLIGHT", "UNKNOWN", "FILLED", *_TERMINAL})


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise JournalError(f"Invalid {field}")
    return value


def _request_digest(request: dict) -> str:
    """Hash the exact, finite JSON request shape durably stored before POST."""
    if not isinstance(request, dict) or not isinstance(request.get("order"), dict):
        raise JournalError("Missing or invalid order request")
    try:
        data = json.dumps(
            request, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise JournalError("Non-JSON or non-finite order request") from exc
    return hashlib.sha256(data).hexdigest()


class IntentJournal:
    """Exclusive local writer for one account; caller provides stable signal IDs."""

    def __init__(self, account_id: str, *, directory: Optional[str] = None):
        self.account_id = _identity(account_id, "account ID")
        root = Path(directory or ".local/execution_intents")
        self.path = root / (account_id + ".json")
        self.lock_path = root / (account_id + ".lock")

    @contextlib.contextmanager
    def _locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise JournalError("Journal already locked; manual recovery required") from exc
        except OSError as exc:
            raise JournalError("Unable to acquire durable journal lock") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()} account={self.account_id}\n")
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            # If the process crashes, the lock remains and blocks all writes.
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def _load(self) -> dict:
        # A dangling symlink has exists() == False. Check it FIRST.
        if self.path.is_symlink():
            raise JournalError("Refusing symlink journal")
        if not self.path.exists():
            return {"version": 1, "account_id": self.account_id, "attempts": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("account_id") != self.account_id
                or not isinstance(data.get("attempts"), dict)):
                raise JournalError("Invalid or different-account journal")
            signals = set()
            for key, attempt in data["attempts"].items():
                _identity(key, "attempt ID")
                if (not isinstance(attempt, dict)
                    or attempt.get("attempt_id") != key
                    or attempt.get("status") not in _STATES):
                    raise JournalError("Malformed journal attempt")
                signal = _identity(attempt.get("signal_id"), "signal ID")
                if signal in signals:
                    raise JournalError("Duplicate durable signal")
                signals.add(signal)
                digest = attempt.get("request_hash")
                if (not isinstance(digest, str) or len(digest) != 64
                    or not hmac.compare_digest(
                        digest, _request_digest(attempt.get("request"))
                    )):
                    raise JournalError("Durable order request hash mismatch")
                for key_time in ("created_at", "updated_at"):
                    if not isinstance(attempt.get(key_time), str):
                        raise JournalError("Missing journal timestamp")
                if parse_utc(attempt["updated_at"]) < parse_utc(attempt["created_at"]):
                    raise JournalError("Journal timestamps regressed")
                if type(attempt.get("protection_verified")) is not bool:
                    raise JournalError("Invalid journal protection marker")
            return data
        except JournalError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise JournalError("Corrupt or unreadable journal") from exc

    def _store(self, data: dict):
        # Same-filesystem replacement; fsync file and containing directory.
        import secrets
        temp = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        fd = None
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump(data, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.is_symlink():
                raise JournalError("Refusing symlink journal replacement")
            os.replace(temp, self.path)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise JournalError("Cannot durably persist broker intent") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def snapshot(self) -> tuple[dict, ...]:
        # A conflicting writer/leftover lock makes a snapshot untrustworthy.
        with self._locked():
            return tuple(dict(x) for x in self._load()["attempts"].values())

    def begin(
        self, *, signal_id: str, attempt_id: str,
        request: dict, risk_pct: str, margin_home: str, grade=None
    ) -> dict:
        _identity(signal_id, "signal ID")
        _identity(attempt_id, "attempt ID")
        if grade is not None and grade not in ("A", "B"):
            raise JournalError("Invalid trade grade")
        digest = _request_digest(request)
        with self._locked():
            data = self._load()
            existing = data["attempts"]
            if (attempt_id in existing or any(
                x["signal_id"] == signal_id for x in existing.values()
            )):
                raise JournalError("Duplicate signal/attempt: broker resend prohibited")
            # Category 4 must reconcile prior fills/unknowns before new orders.
            if any(x["status"] not in _TERMINAL for x in existing.values()):
                raise JournalError("Unresolved/filled trade in account journal")
            now = iso_utc(utc_now())
            entry = {
                "signal_id": signal_id, "attempt_id": attempt_id,
                "request_hash": digest, "request": request,
                "status": "PREPARED", "risk_pct": str(risk_pct),
                "margin_home": str(margin_home), "grade": grade,
                "created_at": now, "updated_at": now,
                "request_id": None, "order_id": None, "trade_id": None,
                "protection_verified": False,
            }
            existing[attempt_id] = entry
            self._store(data)
            return dict(entry)

    def transition(self, attempt_id: str, status: str, **fields) -> dict:
        _identity(attempt_id, "attempt ID")
        if status not in _STATES:
            raise JournalError("Invalid intent state")
        allowed = {
            "PREPARED": {"IN_FLIGHT", "UNKNOWN"},
            "IN_FLIGHT": {"UNKNOWN", "FILLED", "REJECTED", "CANCELLED"},
            "UNKNOWN": {"UNKNOWN", "FILLED", "REJECTED", "CANCELLED"},
            "FILLED": {"FILLED"},
            "REJECTED": set(),
            "CANCELLED": set(),
        }
        safe_fields = {
            "request_id", "order_id", "trade_id", "status_note",
            "filled_units", "fill_price", "trade_reduced", "trades_closed",
            "protection_verified",
        }
        if not set(fields).issubset(safe_fields):
            raise JournalError("Unapproved journal fields")
        with self._locked():
            data = self._load()
            entry = data["attempts"].get(attempt_id)
            if not entry:
                raise JournalError("Missing durable intent")
            if status not in allowed[entry["status"]]:
                raise JournalError("Illegal state transition")
            entry.update(fields)
            entry["status"] = status
            entry["updated_at"] = iso_utc(utc_now())
            self._store(data)
            return dict(entry)

    def get(self, attempt_id: str) -> dict:
        _identity(attempt_id, "attempt ID")
        with self._locked():
            entry = self._load()["attempts"].get(attempt_id)
            if entry is None:
                raise JournalError("Unknown attempt")
            return dict(entry)
