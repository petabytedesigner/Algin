"""Category 28: copy-only archival without altering active trading evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from operations.audit_archive import ArchiveError, EvidenceArchive
from storage.decision_trace import DecisionTrace
from storage.event_log import AuditError, EventLog


NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def add_evidence(folder):
    EventLog(folder).emit(
        event_id="ev-1", correlation_id="cycle-1", stage="SIGNAL",
        event="SIGNAL_REJECTED", outcome="BLOCKED",
        reason_code="TREND_OR_RESET", timestamp=NOW,
    )
    DecisionTrace(folder).append(
        cycle_id="cycle-1", instrument="EUR_USD", direction="LONG",
        candle_time="2026-11-24T11:45:00Z", timestamp=NOW,
        decision="REJECTED", reason_code="TREND_OR_RESET",
        strategy_version="bos-retest-v1",
        inputs={"ema_fast": 1.102, "ema_slow": 1.101,
                "atr": 0.001, "close": 1.103},
        metrics={"trend_strength": .7},
        policy={"ema_fast_period": 20, "ema_slow_period": 50},
        risk_status="NOT_EVALUATED_UNRECONCILED",
        grade=None, score=None, signal_id=None, setup_id=None,
        attempt_id=None, trade_id=None, transaction_id=None,
    )


class Category28ArchiveTests(unittest.TestCase):
    def test_snapshot_both_sources_idempotent_and_no_source_mutation(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            active = {p.name: p.read_bytes() for p in sources.iterdir()}
            archive = EvidenceArchive(sources, dest)
            first = archive.snapshot()
            self.assertEqual(len(first["sources"]), 2)
            self.assertEqual(first["snapshot_id"], archive.snapshot()["snapshot_id"])
            self.assertEqual(len(archive.verify()), 1)
            self.assertEqual({p.name: p.read_bytes() for p in sources.iterdir()}, active)
            found = archive.find(cycle_id="cycle-1")
            self.assertEqual(len(found["events"]), 1)
            self.assertEqual(len(found["decisions"]), 1)
            self.assertEqual(found["decisions"][0]["reason_code"], "TREND_OR_RESET")
            with self.assertRaises(ArchiveError):
                archive.find()

    def test_new_snapshot_reuses_unchanged_blob_and_deduplicates_queries(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            archive = EvidenceArchive(sources, dest)
            first = archive.snapshot()
            EventLog(sources).emit(
                event_id="ev-2", correlation_id="cycle-2", stage="BLOCK",
                event="ENTRY_DENIED", outcome="BLOCKED",
                reason_code="SESSION_BLOCK", timestamp=NOW,
            )
            second = archive.snapshot()
            self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(len(archive.verify()), 2)
            self.assertEqual(len(archive.find(cycle_id="cycle-1")["events"]), 1)
            self.assertEqual(len(archive.find(cycle_id="cycle-2")["events"]), 1)
            self.assertEqual(len(list(dest.glob("*.gz"))), 3)
            self.assertEqual(len(EventLog(sources)._existing()), 2)
            self.assertEqual(len(DecisionTrace(sources).read_all()), 1)

    def test_archived_signal_can_be_found_without_claiming_broker_execution(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            signal_id = "algin-signal-" + "a" * 40
            DecisionTrace(sources).append(
                cycle_id="cycle-2", instrument="EUR_USD", direction="SHORT",
                candle_time="2026-11-24T11:45:00Z", timestamp=NOW,
                decision="OBSERVED", reason_code="OBSERVE_ONLY",
                strategy_version="bos-retest-v1",
                inputs={"ema_fast": 1.101, "ema_slow": 1.102,
                        "atr": .001, "close": 1.100},
                metrics={"trend_strength": .7},
                policy={"grade_b_min": 60},
                risk_status="NOT_EVALUATED_UNRECONCILED",
                grade="B", score=75, signal_id=signal_id, setup_id=None,
                attempt_id=None, trade_id=None, transaction_id=None,
            )
            archive = EvidenceArchive(sources, dest)
            archive.snapshot()
            found = archive.find(signal_id=signal_id)
            self.assertEqual(len(found["decisions"]), 1)
            self.assertEqual(found["decisions"][0]["cycle_id"], "cycle-2")
            self.assertIsNone(found["decisions"][0]["trade_id"])
            self.assertEqual(found["events"], [])

    def test_corrupt_archive_blocks_verification_and_new_snapshots(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            archive = EvidenceArchive(sources, dest)
            snapshot = archive.snapshot()
            original = (sources / "events.csv").read_bytes()
            blob = dest / snapshot["sources"][0]["blob"]
            blob.write_bytes(b"tampered")
            with self.assertRaises(ArchiveError):
                archive.verify()
            with self.assertRaises(ArchiveError):
                archive.snapshot()
            self.assertEqual((sources / "events.csv").read_bytes(), original)

    def test_stale_writer_lock_is_not_stolen(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            lock = sources / "events.lock"
            lock.write_text("another writer")
            with self.assertRaises(ArchiveError):
                EvidenceArchive(sources, dest).snapshot()
            self.assertEqual(lock.read_text(), "another writer")
            self.assertFalse((sources / "decision_trace.lock").exists())

    def test_torn_active_source_rejected_before_archive_write(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            events = sources / "events.csv"
            events.write_bytes(events.read_bytes()[:-1])
            with self.assertRaises(AuditError):
                EvidenceArchive(sources, dest).snapshot()
            self.assertFalse(list(dest.glob("snapshot-*.json")))

    def test_symlink_target_is_rejected_without_touching_target(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            dest.symlink_to(sources, target_is_directory=True)
            with self.assertRaises(ArchiveError):
                EvidenceArchive(sources, dest).snapshot()
            self.assertEqual(len(EventLog(sources)._existing()), 1)

    def test_archive_rejects_manual_manifest_edit(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            archive = EvidenceArchive(sources, dest)
            archive.snapshot()
            manifest = next(dest.glob("snapshot-*.json"))
            content = json.loads(manifest.read_text())
            content["sources"][0]["records"] = 999
            manifest.write_text(json.dumps(content))
            with self.assertRaises(ArchiveError):
                archive.verify()

    def test_missing_intermediate_manifest_breaks_verified_chain(self):
        with TemporaryDirectory() as root:
            sources, dest = Path(root) / "csv", Path(root) / "archive"
            add_evidence(sources)
            archive = EvidenceArchive(sources, dest)
            first = archive.snapshot()
            for index in (2, 3):
                EventLog(sources).emit(
                    event_id=f"ev-{index}", correlation_id=f"cycle-{index}",
                    stage="BLOCK", event="ENTRY_DENIED", outcome="BLOCKED",
                    reason_code="SESSION_BLOCK", timestamp=NOW,
                )
                archive.snapshot()
            self.assertEqual(len(archive.verify()), 3)
            missing = dest / ("snapshot-" + first["snapshot_id"] + ".json")
            missing.unlink()  # Tampering in a disposable test-only directory.
            with self.assertRaises(ArchiveError):
                archive.verify()


if __name__ == "__main__":
    unittest.main()
