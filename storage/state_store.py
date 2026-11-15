"""Account-scoped atomic JSON journal and single-instance process lock."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path


class StateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, account_id: str, root: str = ".local"):
        if not account_id:
            raise StateError("Missing account identity")
        self.account_id = account_id
        self.root = Path(root)
        self.path = self.root / "state.json"
        self.lock_path = self.root / "instance.lock"

    def initial(self) -> dict:
        return {"version": 2, "account_id": self.account_id, "environment": "practice",
                "trades": {}, "intents": {}, "last_candles": {},
                "last_transaction_id": None, "cooldown": {}, "events": {},
                "risk_limits": {}}

    def load(self) -> dict:
        if not self.path.exists():
            return self.initial()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("State corrupt/unreadable; manual recovery required") from exc
        if not isinstance(data, dict) or data.get("version") != 2 or data.get("account_id") != self.account_id or data.get("environment") != "practice":
            raise StateError("State identity/schema mismatch; refuse to trade")
        for k in ("trades", "intents", "last_candles", "cooldown", "events", "risk_limits"):
            if not isinstance(data.get(k), dict):
                raise StateError("State section missing or corrupt: " + k)
        return data

    def save(self, state: dict) -> None:
        if state.get("account_id") != self.account_id or state.get("environment") != "practice":
            raise StateError("Refuse to write account-mismatched state")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / ("state." + str(os.getpid()) + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            if hasattr(os, "O_DIRECTORY"):
                fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if tmp.exists():
                tmp.unlink()

    @contextmanager
    def single_instance(self):
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StateError("Another instance or stale lock: inspect before unlocking") from exc
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()) + "\n")
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)
