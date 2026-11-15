"""Durable M15 signal-cycle gate; claim once per account/instrument/bar (P52).

Checkpoint is committed BEFORE any strategy call. A crash may drop a signal,
never resend it. Corruption or stale writer lock requires manual recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from utils.time_tools import parse_utc


class CandleGateError(RuntimeError):
    pass


_VALID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class CandleGate:
    def __init__(self, account_id, *, directory=".local"):
        if not isinstance(account_id, str) or not _VALID.fullmatch(account_id):
            raise CandleGateError("Invalid account")
        base = Path(directory) / "candle_gates"
        self.path = base / (account_id + ".json")
        self.lock = base / (account_id + ".lock")
        self.account_id = account_id

    @staticmethod
    def _dump(x):
        return json.dumps(x, sort_keys=True, separators=(",", ":")).encode()

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "account_id": self.account_id, "last": {}}
        try:
            if self.path.is_symlink():
                raise CandleGateError("Symlink checkpoint rejected")
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            data = envelope["data"]
            checksum = hashlib.sha256(self._dump(data)).hexdigest()
            if envelope["sha256"] != checksum:
                raise CandleGateError("Checkpoint checksum mismatch")
            if (set(data) != {"version", "account_id", "last"}
                or data["version"] != 1 or data["account_id"] != self.account_id
                or not isinstance(data["last"], dict)):
                raise CandleGateError("Wrong-account/incompatible checkpoint")
            for instrument, stamp in data["last"].items():
                if not isinstance(instrument, str) or not _VALID.fullmatch(instrument):
                    raise CandleGateError("Malformed checkpoint instrument")
                parse_utc(stamp)
            return data
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, CandleGateError):
                raise
            raise CandleGateError("Unreadable checkpoint") from exc

    def _write(self, data):
        temp = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        try:
            checksum = hashlib.sha256(self._dump(data)).hexdigest()
            fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._dump({"data": data, "sha256": checksum}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            if hasattr(os, "O_DIRECTORY"):
                d = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(d)
                finally:
                    os.close(d)
        except OSError as exc:
            raise CandleGateError("Durable checkpoint write failed") from exc
        finally:
            temp.unlink(missing_ok=True)

    def claim(self, instrument, candle_time):
        if not isinstance(instrument, str) or not _VALID.fullmatch(instrument):
            raise CandleGateError("Invalid instrument")
        stamp = parse_utc(candle_time)
        if stamp.second or stamp.microsecond or stamp.minute % 15:
            raise CandleGateError("Not a M15 boundary")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise CandleGateError("Concurrent or orphaned checkpoint lock") from exc
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            data = self._read()
            prior = data["last"].get(instrument)
            if prior is not None and stamp <= parse_utc(prior):
                return False
            data["last"][instrument] = stamp.isoformat().replace("+00:00", "Z")
            self._write(data)
            return True
        finally:
            try:
                self.lock.unlink()
            except OSError:
                pass
