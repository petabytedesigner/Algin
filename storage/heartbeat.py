"""Durable local heartbeat and read-only watchdog inspection.

Heartbeat is status telemetry, NOT broker-account authority or permission to
trade. External monitors can run main.py --heartbeat-check without writes.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets

from utils.time_tools import iso_utc, parse_utc, require_utc, utc_now


class HeartbeatError(RuntimeError):
    pass


STATES = frozenset(("ACTIVE", "CYCLE_COMPLETE", "BLOCKED"))
_REQUIRED = {"version", "state", "reason_code", "cycle_id", "timestamp", "pid"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _id(value):
    return (isinstance(value, str) and len(value) <= 128
            and all(c.isalnum() or c in "_.:-" for c in value))


class Heartbeat:
    def __init__(self, path=".local/heartbeat.json"):
        self.path = Path(path)

    def write(self, state, *, reason_code="NONE", cycle_id=""):
        if state not in STATES or not _id(reason_code) or not _id(cycle_id):
            raise HeartbeatError("Invalid heartbeat state/identifier")
        data = {
            "version": 1, "state": state, "reason_code": reason_code,
            "cycle_id": cycle_id, "timestamp": iso_utc(utc_now()),
            "pid": os.getpid(),
        }
        envelope = {
            "data": data, "sha256": hashlib.sha256(_canonical(data)).hexdigest()
        }
        if self.path.is_symlink():
            raise HeartbeatError("Heartbeat symlink forbidden")
        temp = self.path.with_name(
            self.path.name + "." + secrets.token_hex(8) + ".tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.is_symlink():
                raise HeartbeatError("Heartbeat symlink forbidden")
            os.replace(temp, self.path)
            if hasattr(os, "O_DIRECTORY"):
                parent = os.open(
                    str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY
                )
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except OSError as exc:
            raise HeartbeatError("Heartbeat durability failure") from exc
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return data

    def read(self):
        """Validate checksum, schema, timezone and process identity."""
        if self.path.is_symlink():
            raise HeartbeatError("Heartbeat symlink forbidden")
        if not self.path.exists():
            raise HeartbeatError("Heartbeat missing")
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"data", "sha256"}:
                raise HeartbeatError("Invalid heartbeat envelope")
            data = envelope["data"]
            if not isinstance(data, dict) or set(data) != _REQUIRED:
                raise HeartbeatError("Invalid heartbeat schema")
            if (envelope["sha256"] != hashlib.sha256(
                _canonical(data)
            ).hexdigest()):
                raise HeartbeatError("Heartbeat checksum mismatch")
            if (data["version"] != 1 or data["state"] not in STATES
                or not _id(data["reason_code"]) or not _id(data["cycle_id"])
                or type(data["pid"]) is not int or data["pid"] <= 0):
                raise HeartbeatError("Invalid heartbeat payload")
            parse_utc(data["timestamp"])
            return dict(data)
        except HeartbeatError:
            raise
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise HeartbeatError("Unreadable or invalid heartbeat") from exc

    def inspect(self, *, now=None, max_age_seconds=180):
        """No mutation. ACTIVE is a recent signal, not a process-liveness proof.

        The caller may run this periodically from a separate monitoring
        process; a stale ACTIVE/CYCLE_COMPLETE/BLOCKED is never healthy.
        """
        if (type(max_age_seconds) is not int or max_age_seconds <= 0):
            raise HeartbeatError("Invalid heartbeat maximum age")
        current = utc_now() if now is None else require_utc(now)
        state = self.read()
        stamp = parse_utc(state["timestamp"])
        if stamp > current + timedelta(seconds=5):
            raise HeartbeatError("Heartbeat from untrusted future clock")
        age = current - stamp
        if age > timedelta(seconds=max_age_seconds):
            health = "STALE"
        elif state["state"] == "BLOCKED":
            health = "BLOCKED"
        else:
            health = state["state"]
        return {
            "health": health,
            "state": state["state"],
            "reason_code": state["reason_code"],
            "age_seconds": max(0, int(age.total_seconds())),
            "cycle_id": state["cycle_id"],
        }
