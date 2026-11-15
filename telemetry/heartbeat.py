"""P61 durable heartbeat: alive != cycle-complete != verified broker health.

A recent heartbeat is NOT proof a PID still exists; operators must reconcile
its cycle_id, current process lease and broker state. No credentials or orders.
"""
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets

from utils.time_tools import iso_utc, parse_utc, require_utc, utc_now


class HeartbeatError(RuntimeError):
    """Cannot establish trustworthy process/cycle health."""


STATES = {"ALIVE", "CYCLE_COMPLETE", "BLOCKED"}
BROKER = {"UNKNOWN", "OPEN_TRADES_ONLY", "RECONCILED", "UNVERIFIED"}
CYCLES = {"SCAN", "MANAGEMENT"}
_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class HeartbeatStore:
    def __init__(self, account_id, *, path=None):
        import config
        if not isinstance(account_id, str) or not _ID.fullmatch(account_id):
            raise HeartbeatError("Invalid account")
        configured = config.HEARTBEAT_PATH if path is None else path
        self.account_ref = sha256(account_id.encode()).hexdigest()[:20]
        configured_path = Path(configured)
        # Multiple test/Practice accounts must never share one heartbeat file.
        self.path = configured_path.with_name(
            configured_path.stem + "_" + self.account_ref + configured_path.suffix
        )
        self.lock = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _bytes(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def read(self):
        if self.path.is_symlink() or self.lock.exists() or self.lock.is_symlink():
            raise HeartbeatError("Symlink or concurrent/stale heartbeat writer")
        if not self.path.exists():
            return None
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
            data = body["data"]
            if body["sha256"] != sha256(self._bytes(data)).hexdigest():
                raise HeartbeatError("Heartbeat checksum mismatch")
            if (not isinstance(data, dict)
                or set(data) != {"schema", "account_ref", "cycle_id",
                                 "kind", "state", "broker_state",
                                 "updated_at", "last_complete_at", "pid"}):
                raise HeartbeatError("Unknown heartbeat layout")
            if (data["schema"] != 1 or data["account_ref"] != self.account_ref
                or data["kind"] not in CYCLES or data["state"] not in STATES
                or data["broker_state"] not in BROKER
                or not isinstance(data["pid"], int)
                or not isinstance(data["cycle_id"], str)
                or not _ID.fullmatch(data["cycle_id"])):
                raise HeartbeatError("Wrong-account/invalid heartbeat")
            parse_utc(data["updated_at"])
            if data["last_complete_at"] is not None:
                parse_utc(data["last_complete_at"])
            return dict(data)
        except (OSError, TypeError, ValueError, KeyError) as exc:
            if isinstance(exc, HeartbeatError):
                raise
            raise HeartbeatError("Corrupt heartbeat") from exc

    def _commit(self, data):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise HeartbeatError("Symlink heartbeat forbidden")
        try:
            fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise HeartbeatError("Heartbeat already being written or locked") from exc
        temp = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
            envelope = {"data": data, "sha256": sha256(self._bytes(data)).hexdigest()}
            tmp_fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(tmp_fd, "wb") as stream:
                stream.write(self._bytes(envelope))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            if hasattr(os, "O_DIRECTORY"):
                d = os.open(str(self.path.parent), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(d)
                finally:
                    os.close(d)
        except OSError as exc:
            raise HeartbeatError("Unable to persist heartbeat") from exc
        finally:
            temp.unlink(missing_ok=True)
            try:
                self.lock.unlink()
            except OSError:
                pass

    def start(self, cycle_id: str, *, kind, now=None):
        if kind not in CYCLES or not isinstance(cycle_id, str) or not _ID.fullmatch(cycle_id):
            raise HeartbeatError("Invalid cycle start")
        current = utc_now() if now is None else require_utc(now)
        earlier = self.read()
        if earlier is not None and earlier["state"] == "ALIVE":
            # Process may have crashed: do NOT silently reassert health.
            raise HeartbeatError("Previous unfinished cycle: operator reconciliation")
        data = {
            "schema": 1, "account_ref": self.account_ref,
            "cycle_id": cycle_id, "kind": kind, "state": "ALIVE",
            "broker_state": "UNKNOWN", "updated_at": iso_utc(current),
            "last_complete_at": earlier["last_complete_at"] if earlier else None,
            "pid": os.getpid()
        }
        self._commit(data)
        return data

    def finish(self, cycle_id, *, success, kind, now=None):
        old = self.read()
        if old is None or old["cycle_id"] != cycle_id or old["state"] != "ALIVE":
            raise HeartbeatError("Heartbeat cycle identity mismatch")
        if old["kind"] != kind:
            raise HeartbeatError("Different heartbeat cycle kind")
        current = utc_now() if now is None else require_utc(now)
        data = dict(old)
        data["state"] = "CYCLE_COMPLETE" if success else "BLOCKED"
        data["broker_state"] = (
            "RECONCILED" if kind == "MANAGEMENT" and success
            else "OPEN_TRADES_ONLY" if kind == "SCAN" and success
            else "UNVERIFIED"
        )
        data["updated_at"] = iso_utc(current)
        if success:
            data["last_complete_at"] = data["updated_at"]
        self._commit(data)
        return data

    def status(self, *, now=None, stale_after_seconds=180):
        data = self.read()
        if data is None:
            return {"health": "MISSING", "state": "UNKNOWN"}
        current = utc_now() if now is None else require_utc(now)
        age = current - parse_utc(data["updated_at"])
        if age < timedelta(0) or age > timedelta(seconds=stale_after_seconds):
            health = "STALE"
        elif data["state"] == "BLOCKED":
            health = "BLOCKED"
        else:
            health = "RECENT_NOT_PROCESS_PROOF"
        return {"health": health, "state": data["state"],
                "broker_state": data["broker_state"],
                "age_seconds": age.total_seconds(), "cycle_id": data["cycle_id"]}
