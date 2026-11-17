"""Tamper-evident local forward-watch evidence; never broker authority.

Each run gets a new fsynced JSONL file. The hash chain rejects torn tails,
reordered/deleted events and malformed records; no auto-repair or data loss.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import stat
from uuid import uuid4

from utils.time_tools import iso_utc, parse_utc, utc_now

MAX_RUN_BYTES = 8 * 1024 * 1024
EVENTS = frozenset(("START", "SAMPLE", "LIVENESS", "GATE", "STOP", "BLOCKED"))
_LEGACY_EVENTS = EVENTS - {"LIVENESS"}
_SAFE = re.compile(r"[A-Z0-9_]{1,64}\Z", re.ASCII)
_CYCLE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_RUN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_FIELDS = frozenset(("version", "run", "seq", "stamp", "event", "reason",
                     "pid", "age_seconds", "prev", "sha256"))
_FIELDS_V2 = _FIELDS | {"cycle_id"}


class ForwardEvidenceError(RuntimeError):
    """Local status evidence cannot be trusted or durably saved."""


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def read_run(path):
    path = Path(path)
    if path.is_symlink():
        raise ForwardEvidenceError("Forward journal symlink forbidden")
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_RUN_BYTES:
            raise ForwardEvidenceError("Missing or oversized journal")
        raw = path.read_bytes()
    except OSError:
        raise ForwardEvidenceError("Forward journal unreadable") from None
    if not raw.endswith(b"\n"):
        raise ForwardEvidenceError("Torn journal tail")
    run = path.stem
    if _RUN.fullmatch(run) is None:
        raise ForwardEvidenceError("Invalid forward run name")
    prev = "0" * 64
    out = []
    schema_version = None
    previous_stamp = None
    completed_ids = set()
    try:
        for index, line in enumerate(raw.splitlines()):
            rec = json.loads(line)
            if not isinstance(rec, dict) or type(rec.get("version")) is not int:
                raise ForwardEvidenceError("Invalid forward journal version")
            version = rec["version"]
            if version not in (1, 2):
                raise ForwardEvidenceError("Unsupported forward journal version")
            if schema_version is None:
                schema_version = version
            fields = _FIELDS if version == 1 else _FIELDS_V2
            events = _LEGACY_EVENTS if version == 1 else EVENTS
            if (version != schema_version or frozenset(rec) != fields
                or rec["run"] != run
                or type(rec["seq"]) is not int or rec["seq"] != index
                or rec["event"] not in events
                or not isinstance(rec["reason"], str)
                or _SAFE.fullmatch(rec["reason"]) is None
                or type(rec["pid"]) is not int or rec["pid"] <= 0
                or type(rec["age_seconds"]) is not int
                or rec["age_seconds"] < -1
                or rec["prev"] != prev):
                raise ForwardEvidenceError("Invalid journal schema or sequence")
            stamp = parse_utc(rec["stamp"])
            if previous_stamp is not None and stamp < previous_stamp:
                raise ForwardEvidenceError("Forward journal time regressed")
            previous_stamp = stamp
            if version == 2:
                cid = rec["cycle_id"]
                if (not isinstance(cid, str)
                    or (cid and _CYCLE.fullmatch(cid) is None)):
                    raise ForwardEvidenceError("Invalid forward cycle ID")
                if rec["event"] in ("SAMPLE", "LIVENESS") and not cid:
                    raise ForwardEvidenceError("Missing forward cycle ID")
                if rec["event"] == "SAMPLE":
                    if rec["reason"] != "CYCLE_COMPLETE" or cid in completed_ids:
                        raise ForwardEvidenceError("Duplicate or invalid completed cycle")
                    completed_ids.add(cid)
                if (rec["event"] == "LIVENESS"
                    and rec["reason"] not in ("ACTIVE", "CYCLE_ALREADY_OBSERVED")):
                    raise ForwardEvidenceError("Invalid liveness reason")
            signed = {k: v for k, v in rec.items() if k != "sha256"}
            digest = hashlib.sha256(_canonical(signed)).hexdigest()
            if rec["sha256"] != digest:
                raise ForwardEvidenceError("Journal hash chain mismatch")
            prev = digest
            out.append(rec)
        if not out or out[0]["event"] != "START":
            raise ForwardEvidenceError("Forward run missing START evidence")
        if any(x["event"] == "START" for x in out[1:]):
            raise ForwardEvidenceError("Duplicate forward START")
        if any(x["event"] in ("STOP", "BLOCKED") for x in out[:-1]):
            raise ForwardEvidenceError("Journal contains post-terminal evidence")
        return tuple(out)
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise ForwardEvidenceError("Malformed forward journal") from None


class ForwardJournal:
    def __init__(self, directory=".local/forward-runs", *, run_id=None):
        self.run_id = run_id or uuid4().hex
        if _RUN.fullmatch(self.run_id) is None:
            raise ForwardEvidenceError("Invalid forward run ID")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ForwardEvidenceError("Forward evidence directory symlink")
        self.path = self.directory / (self.run_id + ".jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.fd = os.open(str(self.path), flags, 0o600)
        except OSError:
            raise ForwardEvidenceError("Cannot create exclusive forward evidence") from None
        self.seq = 0
        self.prev = "0" * 64
        self.bytes_written = 0
        self.terminal = False
        self.completed_cycle_ids = set()

    def emit(self, event, reason, *, pid=None, age_seconds=-1, now=None,
             cycle_id=""):
        if (self.terminal or event not in EVENTS
            or not isinstance(reason, str)
            or _SAFE.fullmatch(reason) is None
            or type(age_seconds) is not int or age_seconds < -1
            or not isinstance(cycle_id, str)
            or (cycle_id and _CYCLE.fullmatch(cycle_id) is None)):
            raise ForwardEvidenceError("Invalid forward evidence transition")
        if event in ("SAMPLE", "LIVENESS") and not cycle_id:
            raise ForwardEvidenceError("Missing completed/liveness cycle ID")
        if event == "SAMPLE" and (
            reason != "CYCLE_COMPLETE"
            or cycle_id in self.completed_cycle_ids
        ):
            raise ForwardEvidenceError("Duplicate or invalid completed cycle")
        if event == "LIVENESS" and reason not in (
            "ACTIVE", "CYCLE_ALREADY_OBSERVED"
        ):
            raise ForwardEvidenceError("Invalid forward liveness reason")
        current = os.getpid() if pid is None else pid
        if type(current) is not int or current <= 0:
            raise ForwardEvidenceError("Invalid monitored PID")
        rec = {
            "version": 2, "run": self.run_id, "seq": self.seq,
            "stamp": iso_utc(utc_now() if now is None else now),
            "event": event, "reason": reason, "cycle_id": cycle_id,
            "pid": current, "age_seconds": age_seconds,
            "prev": self.prev,
        }
        rec["sha256"] = hashlib.sha256(_canonical(rec)).hexdigest()
        blob = _canonical(rec) + b"\n"
        if self.bytes_written + len(blob) > MAX_RUN_BYTES:
            raise ForwardEvidenceError("Forward evidence exceeds maximum size")
        try:
            written = os.write(self.fd, blob)
            if written != len(blob):
                raise ForwardEvidenceError("Partial forward evidence write")
            os.fsync(self.fd)
        except OSError:
            raise ForwardEvidenceError("Forward event durability failed") from None
        self.seq += 1
        self.prev = rec["sha256"]
        self.bytes_written += len(blob)
        if event == "SAMPLE":
            self.completed_cycle_ids.add(cycle_id)
        self.terminal = event in ("STOP", "BLOCKED")
        return rec

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def report_runs(directory=".local/forward-runs"):
    """Separate process liveness, unique completed scans and broker proof.

    Historical v1 SAMPLE rows have no cycle identity; they remain readable
    but are never promoted into verified unique completed cycles.
    """
    root = Path(directory)
    if not root.exists():
        return {
            "status": "NO_OBSERVATION_EVIDENCE", "runs": 0,
            "completed_runs": 0, "blocked_runs": 0, "interrupted_runs": 0,
            "samples": 0, "healthy_samples": 0, "liveness_samples": 0,
            "completed_cycle_samples": 0, "gated_samples": 0,
            "legacy_unverified_samples": 0, "recent_block_reason": None,
            "broker_verified_cycles": 0,
            "broker_validation_status": "NOT_ATTESTED_BY_HEARTBEAT",
            "performance": "NOT_VERIFIED",
            "uptime_24h": "NOT_MEASURED_ON_DEVICE",
        }
    if root.is_symlink() or not root.is_dir():
        raise ForwardEvidenceError("Unsafe forward history directory")
    paths = sorted(root.iterdir())
    if len(paths) > 1000 or any(p.suffix != ".jsonl" for p in paths):
        raise ForwardEvidenceError("Unexpected/oversized forward history")
    results = [read_run(p) for p in paths]
    observations = [
        row for run in results for row in run
        if row["event"] in ("SAMPLE", "LIVENESS", "GATE")
    ]
    distinct_completed = {
        row["cycle_id"] for row in observations
        if row["event"] == "SAMPLE" and row["version"] == 2
    }
    blocked = [
        (parse_utc(run[-1]["stamp"]), run[0]["run"], run[-1]["reason"])
        for run in results if run[-1]["event"] == "BLOCKED"
    ]
    incomplete = sum(run[-1]["event"] not in ("STOP", "BLOCKED") for run in results)
    return {
        "status": ("UNVERIFIED_INTERRUPTED_RUNS" if incomplete else
                   "MONITORING_EVIDENCE_ONLY" if results else
                   "NO_OBSERVATION_EVIDENCE"),
        "runs": len(results),
        "completed_runs": sum(run[-1]["event"] == "STOP" for run in results),
        "blocked_runs": len(blocked), "interrupted_runs": incomplete,
        "samples": len(observations),
        "healthy_samples": len(distinct_completed),
        "completed_cycle_samples": len(distinct_completed),
        "liveness_samples": sum(
            row["event"] == "LIVENESS" for row in observations
        ),
        "gated_samples": sum(row["event"] == "GATE" for row in observations),
        "legacy_unverified_samples": sum(
            row["event"] == "SAMPLE" and row["version"] == 1
            for row in observations
        ),
        "recent_block_reason": max(blocked)[2] if blocked else None,
        "broker_verified_cycles": 0,
        "broker_validation_status": "NOT_ATTESTED_BY_HEARTBEAT",
        "performance": "NOT_VERIFIED",
        "uptime_24h": "NOT_MEASURED_ON_DEVICE",
    }
