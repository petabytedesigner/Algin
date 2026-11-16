"""Phase A category 02: offline-only durable recovery regressions."""
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from broker.account_reconciler import AccountReconciler
from broker.practice_transport import BrokerReply
from storage.intent_journal import IntentJournal, JournalError
from storage.lifecycle_state import LifecycleStore, ProcessLease, StateError, _digest


ORDER = {"order": {"instrument": "EUR_USD", "units": "10"}}


class FakeAccount:
    def __init__(self):
        self.cursor, self.orders, self.transactions = "100", [], []
        self.calls = []

    def read(self, path, *, params=None):
        self.calls.append((path, params))
        if path == "":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "account": {"id": "acct-1", "openTradeCount": 0,
                            "pendingOrderCount": len(self.orders),
                            "trades": [], "orders": self.orders}
            })
        if path == "/transactions/sinceid":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "transactions": self.transactions,
            })
        raise AssertionError("Unexpected broker GET: " + path)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)

    def begin(self):
        return self.journal.begin(
            signal_id="sig-1", attempt_id="attempt-1", request=ORDER,
            risk_pct="0.5", margin_home="5", grade="B"
        )

    def test_digest_survives_restart(self):
        self.begin()
        record = IntentJournal("acct-1", directory=self.temp.name).get("attempt-1")
        self.assertEqual(record["request"], ORDER)
        self.assertEqual(len(record["request_hash"]), 64)

    def test_request_payload_tampering_is_blocked_without_mutation(self):
        self.begin()
        data = json.loads(self.journal.path.read_text())
        data["attempts"]["attempt-1"]["request"]["order"]["units"] = "9999"
        self.journal.path.write_text(json.dumps(data))
        original = self.journal.path.read_bytes()
        with self.assertRaises(JournalError):
            self.journal.transition("attempt-1", "IN_FLIGHT")
        self.assertEqual(self.journal.path.read_bytes(), original)

    def test_forged_digest_and_duplicate_signal_block(self):
        self.begin()
        data = json.loads(self.journal.path.read_text())
        data["attempts"]["attempt-1"]["request_hash"] = "f" * 64
        self.journal.path.write_text(json.dumps(data))
        with self.assertRaises(JournalError):
            self.journal.snapshot()
        # Restore a valid record; then duplicate a signal under a second ID.
        data["attempts"]["attempt-1"]["request_hash"] = self.begin_digest
        self.journal.path.write_text(json.dumps(data))
        second = dict(data["attempts"]["attempt-1"])
        second["attempt_id"] = "attempt-2"
        data["attempts"]["attempt-2"] = second
        self.journal.path.write_text(json.dumps(data))
        with self.assertRaises(JournalError):
            self.journal.snapshot()

    @property
    def begin_digest(self):
        import hashlib
        return hashlib.sha256(
            json.dumps(ORDER, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def test_dangling_symlink_is_not_interpreted_as_empty(self):
        self.journal.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal.path.symlink_to(Path(self.temp.name) / "not-created")
        with self.assertRaises(JournalError):
            self.begin()
        self.assertTrue(self.journal.path.is_symlink())

    def test_nonfinite_or_unserializable_request_blocks_before_write(self):
        for value in (float("nan"), object()):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(JournalError):
                    self.journal.begin(
                        signal_id="s", attempt_id="a",
                        request={"order": {"units": value}},
                        risk_pct="1", margin_home="5"
                    )
        self.assertFalse(self.journal.path.exists())

    def test_stale_or_replaced_lock_is_never_deleted(self):
        self.journal.lock_path.parent.mkdir(parents=True)
        self.journal.lock_path.write_text("orphan")
        with self.assertRaises(JournalError):
            self.journal.snapshot()
        self.assertEqual(self.journal.lock_path.read_text(), "orphan")
        self.journal.lock_path.unlink()
        self.begin()
        with self.assertRaises(JournalError):
            with self.journal._locked():
                self.journal.lock_path.write_text("other-owner")
        self.assertEqual(self.journal.lock_path.read_text(), "other-owner")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifecycleStore("acct-1", directory=self.temp.name)
        self.store.initialize("100")

    def forged_state(self, edit):
        envelope = json.loads(self.store.path.read_text())
        edit(envelope["data"])
        envelope["sha256"] = _digest(envelope["data"])
        self.store.path.write_text(json.dumps(envelope))

    def test_processed_transactions_cannot_exceed_cursor(self):
        self.forged_state(lambda s: s["processed_transactions"].append("101"))
        with self.assertRaises(StateError):
            self.store.load()

    def test_processed_transactions_must_be_monotonic(self):
        self.forged_state(
            lambda s: s["processed_transactions"].extend(["99", "98"])
        )
        with self.assertRaises(StateError):
            self.store.load()

    def test_counted_close_requires_processed_transaction(self):
        self.forged_state(lambda s: s["counted_closures"].append("99"))
        with self.assertRaises(StateError):
            self.store.load()

    def test_dangling_symlink_does_not_permit_state_reset(self):
        self.store.path.unlink()
        self.store.path.symlink_to(Path(self.temp.name) / "missing")
        with self.assertRaises(StateError):
            self.store.load()
        with self.assertRaises(StateError):
            self.store.initialize("100")

    def test_atomic_replace_error_preserves_previous_state(self):
        original = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("injected crash")):
            with self.assertRaises(StateError):
                self.store.mutate(
                    lambda s: s["cooldowns"].update({
                        "EUR_USD": "2026-11-16T12:00:00Z"
                    }), expected_cursor="100"
                )
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_replaced_writer_lock_must_remain_intact(self):
        with self.assertRaises(StateError):
            with self.store._exclusive():
                self.store.lock_path.write_text("replacement")
        self.assertEqual(self.store.lock_path.read_text(), "replacement")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker = FakeAccount()
        self.store = LifecycleStore("acct-1", directory=self.temp.name)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)
        self.service = AccountReconciler(
            "acct-1", transport=self.broker, store=self.store,
            journal=self.journal,
            lease=ProcessLease("acct-1", directory=self.temp.name)
        )
        with self.service:
            self.assertEqual(self.service.run().cursor, "100")

    def next_transaction(self, number="101"):
        self.broker.cursor = number
        self.broker.transactions = [{
            "id": number, "type": "ORDER_CANCEL", "accountID": "acct-1"
        }]

    def test_pending_order_freezes_cursor_until_snapshot_is_clean(self):
        self.next_transaction()
        self.broker.orders = [{"id": "90", "instrument": "EUR_USD"}]
        with self.service:
            blocked = self.service.run()
        self.assertTrue(blocked.blocked_reasons)
        self.assertEqual(blocked.cursor, "100")
        self.assertEqual(blocked.new_closed_trade_ids, ())
        self.assertEqual(self.store.load()["processed_transactions"], [])
        self.broker.orders = []
        with self.service:
            clear = self.service.run()
        self.assertFalse(clear.blocked_reasons)
        self.assertEqual(clear.cursor, "101")
        self.assertEqual(self.store.load()["processed_transactions"], ["101"])

    def test_unknown_intent_survives_restart_without_advancing_cursor(self):
        self.journal.begin(
            signal_id="unknown-signal", attempt_id="attempt-unknown",
            request=ORDER, risk_pct="1", margin_home="10", grade="A"
        )
        self.journal.transition("attempt-unknown", "IN_FLIGHT")
        self.journal.transition("attempt-unknown", "UNKNOWN")
        self.next_transaction()
        with self.service:
            blocked = self.service.run()
        self.assertFalse(blocked.entry_allowed)
        self.assertEqual(blocked.cursor, "100")
        self.assertTrue(blocked.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")
        restarted = IntentJournal("acct-1", directory=self.temp.name)
        self.assertEqual(restarted.get("attempt-unknown")["status"], "UNKNOWN")

    def test_fsync_replace_crash_replays_batch_exactly_once(self):
        self.next_transaction()
        original = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("crash")):
            with self.service, self.assertRaises(StateError):
                self.service.run()
        self.assertEqual(self.store.path.read_bytes(), original)
        with self.service:
            self.assertEqual(self.service.run().cursor, "101")
            self.assertEqual(self.service.run().new_closed_trade_ids, ())
        self.assertEqual(self.store.load()["processed_transactions"], ["101"])

    def test_operator_backup_restore_is_read_only_and_reconciled(self):
        self.next_transaction()
        with self.service:
            self.service.run()
        backup = Path(self.temp.name) / "backup.json"
        shutil.copy2(self.store.path, backup)
        restored_root = Path(self.temp.name) / "restored"
        restored = LifecycleStore("acct-1", directory=str(restored_root))
        restored.path.parent.mkdir(parents=True)
        shutil.copy2(backup, restored.path)
        self.assertEqual(restored.load()["cursor"], "101")
        self.next_transaction("102")
        service = AccountReconciler(
            "acct-1", transport=self.broker, store=restored,
            journal=self.journal,
            lease=ProcessLease("acct-1", directory=str(restored_root))
        )
        with service:
            report = service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertFalse(report.entry_allowed)
        self.assertEqual(restored.load()["processed_transactions"], ["101", "102"])

    def test_corrupt_backup_fails_integrity_validation(self):
        restored = LifecycleStore(
            "acct-1", directory=str(Path(self.temp.name) / "bad")
        )
        restored.path.parent.mkdir(parents=True)
        restored.path.write_text("{bad-json")
        with self.assertRaises(StateError):
            restored.load()


if __name__ == "__main__":
    unittest.main()
