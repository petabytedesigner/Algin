"""Single-instance read-only Termux forward observation supervisor.

Starts the existing main.py read-only loop, records broker-facing liveness,
and STOPS (no automatic restart) on a failed/stale/foreign heartbeat or
ambiguous account state. It never places orders or claims trading returns.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

import config
from broker.practice_transport import PRACTICE_API, _PRACTICE_WRITES_RELEASED
from operations.forward_journal import (
    ForwardJournal, ForwardEvidenceError, report_runs,
)
from storage.heartbeat import Heartbeat, HeartbeatError
from utils.time_tools import parse_utc, utc_now

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ACCOUNT = "101-001-37674492-003"
EXPECTED_KEYS = frozenset(("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"))
NO_TRADE_GATE = frozenset(("SESSION_BLOCK",))


class ForwardBlocked(RuntimeError):
    """Operator action is required; NEVER restart automatically."""


def verify_readonly():
    if (config.OANDA_URL != PRACTICE_API
        or config.ORDER_WRITES_ENABLED is not False
        or _PRACTICE_WRITES_RELEASED is not False):
        raise ForwardBlocked("READ_ONLY_GATES_FAILED")


def account_environment(repo=REPO_ROOT, *, environ=None):
    """Account from environment or existing sample file; NEVER shell-source."""
    verify_readonly()
    env = dict(os.environ if environ is None else environ)
    direct = {key: env.get(key) for key in EXPECTED_KEYS}
    if any(direct.values()) and not all(direct.values()):
        raise ForwardBlocked("PARTIAL_ENV_CREDENTIALS")
    if not all(direct.values()):
        path = Path(repo) / ".env.example"
        if path.is_symlink() or not path.is_file():
            raise ForwardBlocked("DEMO_CONFIG_NOT_AVAILABLE")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            pairs = {}
            if len(lines) > 100:
                raise ForwardBlocked("DEMO_CONFIG_TOO_LARGE")
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.count("=") != 1:
                    raise ForwardBlocked("DEMO_CONFIG_INVALID")
                key, val = (x.strip() for x in line.split("=", 1))
                if key not in EXPECTED_KEYS or key in pairs:
                    raise ForwardBlocked("DEMO_CONFIG_INVALID")
                pairs[key] = val
            direct = pairs
        except (OSError, UnicodeError):
            raise ForwardBlocked("DEMO_CONFIG_UNREADABLE") from None
    token = direct.get("OANDA_API_TOKEN")
    account = direct.get("OANDA_ACCOUNT_ID")
    if (account != EXPECTED_ACCOUNT
        or not isinstance(token, str) or len(token) < 20
        or any(c.isspace() for c in token)
        or len(token) > 2048):
        raise ForwardBlocked("DEMO_ACCOUNT_OR_TOKEN_INVALID")
    env["OANDA_API_TOKEN"] = token
    env["OANDA_ACCOUNT_ID"] = account
    return env


class ExclusiveSupervisor:
    """Orphan lock is a signal to inspect; never auto-delete an unknown PID."""
    def __init__(self, root=REPO_ROOT):
        self.path = Path(root) / ".local" / "forward-supervisor.lock"
        self.identity = None
        self.owner = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ForwardBlocked("SUPERVISOR_LOCK_SYMLINK")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except OSError:
            raise ForwardBlocked("SUPERVISOR_ALREADY_RUNNING_OR_ORPHAN_LOCK") from None
        try:
            self.identity = os.fstat(fd)
            self.owner = str(os.getpid()) + ":" + os.urandom(16).hex()
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(self.owner + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # An unconfirmed lock is intentionally left for operator review.
            raise ForwardBlocked("SUPERVISOR_LOCK_DURABILITY_UNKNOWN") from None
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            current = self.path.lstat()
            if (self.path.is_symlink()
                or (current.st_dev, current.st_ino) !=
                    (self.identity.st_dev, self.identity.st_ino)
                or self.path.read_text(encoding="ascii").strip() != self.owner):
                raise ForwardBlocked("SUPERVISOR_LOCK_OWNERSHIP_CHANGED")
            self.path.unlink()
        except OSError:
            raise ForwardBlocked("SUPERVISOR_LOCK_RELEASE_UNVERIFIED") from None
        return False


def _new_runtime_log(journal):
    path = journal.path.with_suffix(".runtime.log")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        raise ForwardBlocked("RUNTIME_LOG_UNAVAILABLE") from None


class ForwardSupervisor:
    def __init__(self, *, root=REPO_ROOT, interval=30, startup_grace=120,
                 max_age=180, samples=0, clock=None, sleeper=None,
                 launcher=None, heartbeat=None, journal_factory=None):
        if (type(interval) is not int or interval <= 0 or interval > 600
            or type(startup_grace) is not int or startup_grace <= 0
            or type(max_age) is not int or max_age < interval*2
            or type(samples) is not int or samples < 0):
            raise ForwardBlocked("INVALID_MONITOR_TIMING")
        self.root = Path(root)
        self.interval = interval
        self.startup_grace = startup_grace
        self.max_age = max_age
        self.samples = samples
        self.clock = clock or utc_now
        self.sleeper = sleeper or time.sleep
        self.launcher = launcher or subprocess.Popen
        self.heartbeat = heartbeat or Heartbeat(
            self.root / config.HEARTBEAT_PATH
        )
        self.journal_factory = journal_factory or (
            lambda: ForwardJournal(self.root / ".local" / "forward-runs")
        )

    @staticmethod
    def _terminate(proc):
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

    def _inspect(self, proc, started):
        # A heartbeat from an earlier process does not prove THIS child.
        try:
            snapshot = self.heartbeat.read()
            stamp = parse_utc(snapshot["timestamp"])
            now = self.clock()
            if stamp < started - timedelta(seconds=5):
                return "PENDING", "OLD_HEARTBEAT", -1
            if snapshot["pid"] != proc.pid:
                return "BLOCKED", "FOREIGN_HEARTBEAT", -1
            health = self.heartbeat.inspect(
                now=now, max_age_seconds=self.max_age
            )
            if health["health"] == "STALE":
                return "BLOCKED", "STALE_HEARTBEAT", health["age_seconds"]
            if health["state"] == "BLOCKED":
                if health["reason_code"] in NO_TRADE_GATE:
                    return "GATE", health["reason_code"], health["age_seconds"]
                return "BLOCKED", "BOT_" + health["reason_code"], health["age_seconds"]
            if health["state"] in ("ACTIVE", "CYCLE_COMPLETE"):
                return "SAMPLE", health["state"], health["age_seconds"]
            return "BLOCKED", "INVALID_HEARTBEAT_STATE", -1
        except HeartbeatError:
            return "PENDING", "HEARTBEAT_UNAVAILABLE", -1

    def run(self, *, env):
        verify_readonly()
        if (not isinstance(env, dict)
            or env.get("OANDA_ACCOUNT_ID") != EXPECTED_ACCOUNT
            or not env.get("OANDA_API_TOKEN")
            or not (self.root / "main.py").is_file()):
            raise ForwardBlocked("FORWARD_RUNTIME_INPUTS_INVALID")
        with ExclusiveSupervisor(self.root):
            journal = self.journal_factory()
            proc = None
            output = None
            try:
                output = _new_runtime_log(journal)
                proc = self.launcher(
                    [sys.executable, "-u", "main.py"],
                    cwd=str(self.root), env=env,
                    stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT,
                )
                started = self.clock()
                journal.emit("START", "OBSERVE_ONLY", pid=proc.pid, now=started)
                valid = 0
                polls = 0
                while True:
                    self.sleeper(self.interval)
                    if proc.poll() is not None:
                        journal.emit("BLOCKED", "BOT_PROCESS_EXITED",
                                     pid=proc.pid, now=self.clock())
                        return 3
                    status, reason, age = self._inspect(proc, started)
                    if status == "PENDING":
                        if self.clock()-started >= timedelta(seconds=self.startup_grace):
                            journal.emit("BLOCKED", "STARTUP_NO_HEARTBEAT",
                                         pid=proc.pid, now=self.clock())
                            return 3
                    elif status == "BLOCKED":
                        # Do not retry/restart even after a transient 401;
                        # explicit operator diagnosis and reconciler required.
                        journal.emit("BLOCKED", reason, pid=proc.pid,
                                     age_seconds=age, now=self.clock())
                        return 3
                    else:
                        journal.emit(status, reason, pid=proc.pid,
                                     age_seconds=age, now=self.clock())
                        valid += 1
                    polls += 1
                    if self.samples and polls >= self.samples:
                        if not valid:
                            journal.emit("BLOCKED", "NO_HEALTHY_OBSERVATION",
                                         pid=proc.pid, now=self.clock())
                            return 3
                        journal.emit("STOP", "SAMPLE_WINDOW_COMPLETE",
                                     pid=proc.pid, now=self.clock())
                        return 0
                    if output.tell() > 16 * 1024 * 1024:
                        journal.emit("BLOCKED", "RUNTIME_LOG_SIZE_LIMIT",
                                     pid=proc.pid, now=self.clock())
                        return 3
            except KeyboardInterrupt:
                if proc is not None and not journal.terminal:
                    journal.emit("STOP", "OPERATOR_INTERRUPT",
                                 pid=proc.pid, now=self.clock())
                return 0
            except (OSError, ForwardBlocked, ForwardEvidenceError):
                if proc is not None and not journal.terminal:
                    journal.emit("BLOCKED", "SUPERVISOR_RUNTIME_ERROR",
                                 pid=proc.pid, now=self.clock())
                raise
            finally:
                self._terminate(proc)
                if output is not None:
                    output.close()
                journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Algin Termux read-only forward supervisor (no broker writes)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="Offline policy and module check; no credentials loaded")
    group.add_argument("--run", action="store_true",
                       help="Supervise existing OBSERVE_ONLY main.py continuously")
    group.add_argument("--report", action="store_true",
                       help="Validate local run evidence; no broker calls")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--samples", type=int, default=0,
                        help="0=continuous; N=bounded observed samples")
    args = parser.parse_args(argv)
    try:
        verify_readonly()
        if args.check:
            print(json.dumps({"status": "OFFLINE_POLICY_PASS",
                              "broker_mutations": 0}, sort_keys=True))
            return 0
        if args.report:
            print(json.dumps(report_runs(REPO_ROOT / ".local" / "forward-runs"),
                             sort_keys=True))
            return 0
        env = account_environment(REPO_ROOT)
        code = ForwardSupervisor(interval=args.interval,
                                 samples=args.samples).run(env=env)
        print(json.dumps({"status": "MONITORING_WINDOW_COMPLETE" if code == 0
                          else "MONITORING_BLOCKED", "broker_mutations": 0},
                         sort_keys=True))
        return code
    except (ForwardBlocked, ForwardEvidenceError) as exc:
        # All messages are static diagnostic codes; no token/HTTP payload.
        print(json.dumps({"status": "FORWARD_BLOCKED",
                          "reason": str(exc), "broker_mutations": 0},
                         sort_keys=True))
        return 3
    except Exception:
        print(json.dumps({"status": "FORWARD_BLOCKED",
                          "reason": "UNEXPECTED_SUPERVISOR_ERROR",
                          "broker_mutations": 0}, sort_keys=True))
        return 3


if __name__ == "__main__":
    sys.exit(main())
