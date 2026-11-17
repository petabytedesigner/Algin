"""Phase D Category 18: accurate forward liveness and unique cycle evidence.

All inputs are synthetic; no broker request, credentials or trading actions.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import unittest

from operations.forward_journal import (
    ForwardEvidenceError, ForwardJournal, read_run, report_runs,
)
from operations.forward_supervisor import ForwardSupervisor
from test_phase_c_category13_supervisor import (
    Clock, FakeChild, FakeHeartbeat, TOKEN, EXPECTED_ACCOUNT, NOW,
)


class UniqueHeartbeatSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "main.py").write_text("# synthetic, never launched\n")
        self.clock = Clock()
        self.child = FakeChild()
        self.hb = FakeHeartbeat(self.clock, self.child)
        self.env = {"OANDA_ACCOUNT_ID": EXPECTED_ACCOUNT,
                    "OANDA_API_TOKEN": TOKEN}
        self.launch_count = 0

    def launch(self, *args, **kwargs):
        self.launch_count += 1
        return self.child

    def watch(self, *, samples=3, sleeper=None):
        return ForwardSupervisor(
            root=self.root, samples=samples, interval=30,
            clock=self.clock.now, sleeper=sleeper or self.clock.sleep,
            launcher=self.launch, heartbeat=self.hb,
        ).run(env=self.env)

    def summary(self):
        return report_runs(self.root / ".local" / "forward-runs")

    def test_active_heartbeats_never_count_as_completed_cycles(self):
        self.hb.state = "ACTIVE"
        self.assertEqual(self.watch(), 3)
        report = self.summary()
        self.assertEqual(report["samples"], 3)
        self.assertEqual(report["liveness_samples"], 3)
        self.assertEqual(report["healthy_samples"], 0)
        self.assertEqual(report["completed_cycle_samples"], 0)
        self.assertEqual(report["recent_block_reason"], "NO_COMPLETED_CYCLE")
        self.assertEqual(report["broker_verified_cycles"], 0)
        self.assertEqual(self.launch_count, 1)
        self.assertEqual(self.child.terminations, 1)

    def test_repeated_same_completed_heartbeat_is_one_cycle(self):
        self.hb.cycle_id_override = "same-cycle"
        self.assertEqual(self.watch(samples=4), 0)
        report = self.summary()
        self.assertEqual(report["samples"], 4)
        self.assertEqual(report["liveness_samples"], 3)
        self.assertEqual(report["healthy_samples"], 1)
        self.assertEqual(report["completed_cycle_samples"], 1)
        self.assertEqual(report["completed_runs"], 1)
        rows = read_run(next((self.root / ".local" / "forward-runs").glob("*.jsonl")))
        self.assertEqual([x["event"] for x in rows],
                         ["START", "SAMPLE", "LIVENESS", "LIVENESS", "LIVENESS", "STOP"])
        self.assertEqual(rows[2]["reason"], "CYCLE_ALREADY_OBSERVED")

    def test_active_then_two_distinct_completed_cycles(self):
        self.hb.state = "ACTIVE"
        calls = [0]
        def tick(seconds):
            self.clock.sleep(seconds)
            calls[0] += 1
            if calls[0] == 2:
                self.hb.state = "CYCLE_COMPLETE"
        self.assertEqual(self.watch(samples=3, sleeper=tick), 0)
        summary = self.summary()
        self.assertEqual(summary["liveness_samples"], 1)
        self.assertEqual(summary["completed_cycle_samples"], 2)
        self.assertEqual(summary["healthy_samples"], 2)

    def test_empty_cycle_id_fails_closed_without_healthy_sample(self):
        self.hb.cycle_id_override = ""
        self.assertEqual(self.watch(samples=3), 3)
        report = self.summary()
        self.assertEqual(report["healthy_samples"], 0)
        self.assertEqual(report["recent_block_reason"], "INVALID_CYCLE_ID")

    def test_gated_window_stays_a_gate_not_a_verified_cycle(self):
        self.hb.state = "BLOCKED"
        self.hb.reason = "SESSION_BLOCK"
        self.assertEqual(self.watch(samples=2), 0)
        report = self.summary()
        self.assertEqual(report["gated_samples"], 2)
        self.assertEqual(report["healthy_samples"], 0)
        self.assertEqual(report["broker_verified_cycles"], 0)
        self.assertEqual(report["broker_validation_status"],
                         "NOT_ATTESTED_BY_HEARTBEAT")


class ForwardEvidenceV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_duplicate_completed_id_rejected_before_write(self):
        j = ForwardJournal(self.folder)
        try:
            j.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            j.emit("SAMPLE", "CYCLE_COMPLETE", now=NOW, cycle_id="first")
            with self.assertRaises(ForwardEvidenceError):
                j.emit("SAMPLE", "CYCLE_COMPLETE", now=NOW, cycle_id="first")
            j.emit("STOP", "SAMPLE_WINDOW_COMPLETE", now=NOW)
        finally:
            j.close()
        rows = read_run(j.path)
        self.assertEqual([x["event"] for x in rows], ["START", "SAMPLE", "STOP"])
        self.assertEqual(report_runs(self.folder)["completed_cycle_samples"], 1)

    def test_legacy_v1_readable_but_not_mislabeled_healthy(self):
        run_id = "b" * 32
        path = self.folder / (run_id + ".jsonl")
        previous = "0" * 64
        lines = []
        for i, (event, reason) in enumerate((
            ("START", "SUPERVISOR_LAUNCHING"),
            ("SAMPLE", "ACTIVE"),
            ("STOP", "SAMPLE_WINDOW_COMPLETE"),
        )):
            row = {
                "version": 1, "run": run_id, "seq": i,
                "stamp": "2026-11-17T12:00:00Z", "event": event,
                "reason": reason, "pid": 101, "age_seconds": -1,
                "prev": previous,
            }
            data = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            previous = hashlib.sha256(data).hexdigest()
            row["sha256"] = previous
            lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        path.write_text("\n".join(lines) + "\n")
        self.assertEqual(len(read_run(path)), 3)
        report = report_runs(self.folder)
        self.assertEqual(report["samples"], 1)
        self.assertEqual(report["legacy_unverified_samples"], 1)
        self.assertEqual(report["healthy_samples"], 0)
        self.assertEqual(report["broker_verified_cycles"], 0)

    def test_latest_block_uses_timestamp_not_random_filename(self):
        older = ForwardJournal(self.folder, run_id="f"*32)
        newer = ForwardJournal(self.folder, run_id="a"*32)
        try:
            older.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            older.emit("BLOCKED", "STALE_HEARTBEAT",
                       now=NOW + timedelta(seconds=5))
            newer.emit("START", "SUPERVISOR_LAUNCHING",
                       now=NOW + timedelta(seconds=10))
            newer.emit("BLOCKED", "BOT_BROKER_UNAVAILABLE",
                       now=NOW + timedelta(seconds=20))
        finally:
            older.close()
            newer.close()
        summary = report_runs(self.folder)
        self.assertEqual(summary["blocked_runs"], 2)
        self.assertEqual(summary["recent_block_reason"], "BOT_BROKER_UNAVAILABLE")

    def test_reordered_time_rejected_even_when_hash_chain_valid(self):
        j = ForwardJournal(self.folder)
        try:
            j.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            j.emit("LIVENESS", "ACTIVE", cycle_id="first",
                   now=NOW - timedelta(seconds=1))
        finally:
            j.close()
        with self.assertRaises(ForwardEvidenceError):
            read_run(j.path)

    def test_no_cycle_id_cannot_emit_completed_evidence(self):
        j = ForwardJournal(self.folder)
        try:
            j.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            with self.assertRaises(ForwardEvidenceError):
                j.emit("SAMPLE", "CYCLE_COMPLETE", now=NOW)
            with self.assertRaises(ForwardEvidenceError):
                j.emit("LIVENESS", "ACTIVE", now=NOW)
            with self.assertRaises(ForwardEvidenceError):
                j.emit("SAMPLE", "ACTIVE", now=NOW, cycle_id="fake")
            j.emit("BLOCKED", "NO_COMPLETED_CYCLE", now=NOW)
        finally:
            j.close()
        self.assertEqual(report_runs(self.folder)["healthy_samples"], 0)

    def test_global_cycle_ids_not_double_counted_across_runs(self):
        for idx in ("1"*32, "2"*32):
            j = ForwardJournal(self.folder, run_id=idx)
            try:
                j.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
                j.emit("SAMPLE", "CYCLE_COMPLETE", now=NOW, cycle_id="shared")
                j.emit("STOP", "SAMPLE_WINDOW_COMPLETE", now=NOW)
            finally:
                j.close()
        result = report_runs(self.folder)
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["completed_cycle_samples"], 1)
        self.assertEqual(result["completed_runs"], 2)

    def test_heartbeat_success_is_not_broker_attestation_or_24h(self):
        j = ForwardJournal(self.folder)
        try:
            j.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            j.emit("SAMPLE", "CYCLE_COMPLETE", now=NOW, cycle_id="once")
            j.emit("STOP", "SAMPLE_WINDOW_COMPLETE", now=NOW)
        finally:
            j.close()
        r = report_runs(self.folder)
        self.assertEqual(r["completed_cycle_samples"], 1)
        self.assertEqual(r["broker_verified_cycles"], 0)
        self.assertEqual(r["broker_validation_status"], "NOT_ATTESTED_BY_HEARTBEAT")
        self.assertEqual(r["uptime_24h"], "NOT_MEASURED_ON_DEVICE")
        self.assertEqual(r["performance"], "NOT_VERIFIED")


if __name__ == "__main__":
    unittest.main()
