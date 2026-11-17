"""Phase B Category 08: deterministic offline operational resilience regressions.

No network, real OANDA orders or credentials. Distinct process/thread
boundaries, checksum failures, randomized adversarial payloads, full-cycle
audit projection, and crash/replay must all fail closed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import copy
import csv
import json
import random
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from broker.oanda_v20_contract import OandaContractError, validate_sinceid_batch
from execution.offline_management_engine import build_command
from risk.trade_lifecycle import TradeRecord
from storage.event_log import AuditError, AuditProjector, EventLog
from storage.heartbeat import Heartbeat, HeartbeatError
from storage.lifecycle_state import LifecycleStore, StateError, _digest
from storage.intent_journal import JournalError, IntentJournal
from test_category6_completion import NOW, audit_fields
from test_phase_b_category07 import OfflineManagementFixture


def rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class AuditWriterResilienceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.log = EventLog(self.folder.name)
        self.lock = self.log.path.with_suffix(".lock")

    def test_two_threads_cannot_append_during_same_exclusive_writer(self):
        inside = threading.Event()
        release = threading.Event()
        original = self.log._existing

        def held():
            inside.set()
            if not release.wait(5):
                raise AssertionError("Concurrent writer test timed out")
            return original()

        with patch.object(self.log, "_existing", side_effect=held):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.log.emit_once, **audit_fields("owner"))
                try:
                    self.assertTrue(inside.wait(5))
                    with self.assertRaises(AuditError):
                        self.log.emit_once(**audit_fields("contender"))
                finally:
                    release.set()
                first.result(timeout=5)
        self.assertEqual(len(rows(self.log.path)), 1)
        self.assertFalse(self.lock.exists())

    def test_replaced_lock_is_not_unlinked_or_allowed_to_write(self):
        old = self.log._existing

        def replaced():
            self.lock.write_text("pid=999 owner=foreign", encoding="ascii")
            return old()

        with patch.object(self.log, "_existing", side_effect=replaced):
            with self.assertRaisesRegex(AuditError, "ownership changed"):
                self.log.emit_once(**audit_fields("collision"))
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.lock.read_text(), "pid=999 owner=foreign")
        self.assertFalse(self.log.path.exists())

    def test_failed_lock_fsync_leaves_orphan_for_manual_recovery(self):
        with patch("storage.event_log.os.fsync",
                   side_effect=OSError("simulated lock fsync loss")):
            with self.assertRaises(AuditError):
                self.log.emit_once(**audit_fields("crash"))
        self.assertTrue(self.lock.exists())
        self.assertFalse(self.log.path.exists())
        with self.assertRaises(AuditError):
            self.log.emit_once(**audit_fields("later"))

    def test_torn_audit_tail_preserved_across_restart(self):
        self.log.emit_once(**audit_fields("first"))
        with self.log.path.open("ab") as handle:
            handle.write(b"torn record")
        old = self.log.path.read_bytes()
        fresh = EventLog(self.folder.name)
        with self.assertRaises(AuditError):
            fresh.emit_once(**audit_fields("second"))
        self.assertEqual(fresh.path.read_bytes(), old)

    def test_deterministic_audit_replay_with_randomized_order(self):
        gen = random.Random(20261117)
        keys = [f"event{i:03d}" for i in range(96)]
        gen.shuffle(keys)
        expected = [audit_fields(key) for key in keys]
        self.log.emit_many_once(expected)
        for _ in range(5):
            gen.shuffle(expected)
            self.log.emit_many_once(expected)
        result = rows(self.log.path)
        self.assertEqual(len(result), 96)
        self.assertEqual({x["event_id"] for x in result}, set(keys))

    def test_fuzz_conflicting_csv_event_identity_never_accepted(self):
        gen = random.Random(17082026)
        self.log.emit_once(**audit_fields("immutable"))
        old = self.log.path.read_bytes()
        for i in range(75):
            random_suffix = str(gen.randint(1, 100000000))
            with self.subTest(i=i), self.assertRaises(AuditError):
                self.log.emit_once(**audit_fields(
                    "immutable", event="CONFLICT" + random_suffix
                ))
        self.assertEqual(self.log.path.read_bytes(), old)


class DurableStateResilienceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = LifecycleStore("acct-1", directory=self.folder.name)
        self.store.initialize("100")

    def test_competing_state_writer_cannot_mutate_under_exclusive_lock(self):
        started = threading.Event()
        released = threading.Event()

        def owner(state):
            started.set()
            if not released.wait(5):
                raise AssertionError("State lock test timed out")
            state["cooldowns"]["EUR_USD"] = "2026-11-17T12:00:00Z"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.store.mutate, owner, expected_cursor="100")
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaises(StateError):
                    self.store.mutate(
                        lambda s: s.update(cursor="999"), expected_cursor="100"
                    )
            finally:
                released.set()
            first.result(timeout=5)
        result = self.store.load()
        self.assertEqual(result["cursor"], "100")
        self.assertEqual(result["cooldowns"], {
            "EUR_USD": "2026-11-17T12:00:00Z"
        })

    def test_atomic_replace_failure_keeps_old_cursor_and_financial_history(self):
        before = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("synthetic disk crash")):
            with self.assertRaises(StateError):
                self.store.mutate(
                    lambda state: state.update(cursor="101"),
                    expected_cursor="100",
                )
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_property_fuzz_cursor_and_close_intent_corruption_fails_closed(self):
        gen = random.Random(20261117)
        baseline = self.store.load()
        bad_states = []
        for _ in range(96):
            proposed = copy.deepcopy(baseline)
            proposed["cursor"] = gen.choice([
                "-1", "x101", "101.1", "1/2", "", "NaN",
            ])
            bad_states.append(proposed)
        for mutated in bad_states:
            envelope = {"data": mutated, "sha256": _digest(mutated)}
            self.store.path.write_text(
                json.dumps(envelope), encoding="utf-8"
            )
            with self.assertRaises(StateError):
                self.store.load()
        self.store.path.write_text(json.dumps({
            "data": baseline, "sha256": _digest(baseline)
        }), encoding="utf-8")
        self.assertEqual(self.store.load(), baseline)

    def test_wrong_checksum_rejected_without_reset(self):
        original = self.store.path.read_bytes()
        snapshot = json.loads(original)
        snapshot["data"]["cursor"] = "101"
        self.store.path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaises(StateError):
            self.store.load()
        self.store.path.write_bytes(original)
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_journal_lock_orphan_does_not_release_on_restart(self):
        journal = IntentJournal("acct-1", directory=self.folder.name)
        journal.lock_path.parent.mkdir(parents=True, exist_ok=True)
        journal.lock_path.write_text("pid=999 owner=orphan", encoding="ascii")
        with self.assertRaises(JournalError):
            journal.snapshot()
        self.assertEqual(
            journal.lock_path.read_text(), "pid=999 owner=orphan"
        )


class BrokerBatchFuzzTests(unittest.TestCase):
    def test_150_deterministic_valid_and_corrupt_transaction_sequences(self):
        gen = random.Random(8708)
        for i in range(150):
            numbers = sorted(gen.sample(range(101, 150), gen.randint(1, 5)))
            numbers.append(150)
            batch = {
                "lastTransactionID": "150",
                "transactions": [
                    {"id": str(x), "accountID": "acct-1", "type": "ORDER_CANCEL"}
                    for x in numbers
                ]
            }
            valid = validate_sinceid_batch(
                batch, account="acct-1",
                previous_cursor="100", target_cursor="150",
            )
            self.assertEqual(len(valid), len(numbers))
            bad = copy.deepcopy(batch)
            idx = gen.randrange(len(bad["transactions"]))
            bad["transactions"][idx]["accountID"] = "other"
            with self.assertRaises(OandaContractError):
                validate_sinceid_batch(
                    bad, account="acct-1", previous_cursor="100",
                    target_cursor="150",
                )


class LifecycleAuditIntegrationTests(OfflineManagementFixture):
    def setUp(self):
        super().setUp()
        self.audit = EventLog(Path(self.folder.name) / "audit")
        self.projector = AuditProjector("acct-1", self.audit)

    def project(self):
        return self.projector.project(
            self.journal.snapshot(), self.store.load()
        )

    def test_full_be_partial_close_is_replayable_without_extra_rows(self):
        self.bootstrap()
        with self.service:
            self.assertEqual(self.be().status, "CONFIRMED")
            self.assertEqual(self.partial().status, "CONFIRMED")
            self.assertEqual(self.full().status, "CONFIRMED")
        self.project()
        first = rows(self.audit.path)
        names = {r["event"] for r in first}
        self.assertTrue({
            "BE_CONFIRMED", "BE_TX_CONFIRMED",
            "PARTIAL_CONFIRMED", "PARTIAL_TX_CONFIRMED",
            "CLOSE_INTENT", "CLOSE_CONFIRMED", "TRADE_CLOSED",
        }.issubset(names))
        txs = {r["event"]: r["transaction_id"] for r in first}
        self.assertEqual(txs["BE_TX_CONFIRMED"], "101")
        self.assertEqual(txs["PARTIAL_TX_CONFIRMED"], "103")
        self.assertEqual(txs["CLOSE_CONFIRMED"], "105")
        self.assertEqual(txs["TRADE_CLOSED"], "105")
        self.projector = AuditProjector(
            "acct-1", EventLog(Path(self.folder.name) / "audit")
        )
        for _ in range(3):
            self.project()
        self.assertEqual(rows(self.audit.path), first)
        self.assertEqual(self.transport.simulated_writes, 3)

    def test_unresolved_full_close_never_audits_success(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            self.manager.mark_unknown("11", operation="CLOSE")
        self.project()
        names = {x["event"] for x in rows(self.audit.path)}
        self.assertIn("CLOSE_INTENT", names)
        self.assertIn("CLOSE_UNKNOWN", names)
        self.assertNotIn("CLOSE_CONFIRMED", names)

    def test_broker_stop_supersedes_close_and_audit_remains_idempotent(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            self.manager.mark_unknown("11", operation="CLOSE")
        self.broker_stop_close()
        with self.service:
            self.service.run()
        self.project()
        before = rows(self.audit.path)
        superseded = [x for x in before if x["event"] == "CLOSE_SUPERSEDED"]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["outcome"], "INFO")
        self.assertEqual(superseded[0]["transaction_id"], "101")
        self.assertNotIn("CLOSE_CONFIRMED", {x["event"] for x in before})
        self.project()
        self.assertEqual(rows(self.audit.path), before)

    def test_sl_superseded_be_never_audits_be_success(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.broker_stop_close()
        with self.service:
            self.service.run()
        self.project()
        data = rows(self.audit.path)
        events = {x["event"]: x["outcome"] for x in data}
        self.assertEqual(events["BE_SUPERSEDED"], "INFO")
        self.assertNotIn("BE_CONFIRMED", events)

    def test_tp_superseded_partial_never_audits_partial_success(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.broker_stop_close(reason="TAKE_PROFIT_ORDER")
        with self.service:
            self.service.run()
        self.project()
        data = rows(self.audit.path)
        events = {x["event"]: x["outcome"] for x in data}
        self.assertEqual(events["PARTIAL_SUPERSEDED"], "INFO")
        self.assertNotIn("PARTIAL_CONFIRMED", events)

    def test_tampered_close_transaction_provenance_cannot_project_success(self):
        self.bootstrap()
        with self.service:
            self.assertEqual(self.full().status, "CONFIRMED")
        state = self.store.load()
        state["close_intents"]["11"]["confirmed_transaction_id"] = "999"
        with self.assertRaises(AuditError):
            self.projector.project(self.journal.snapshot(), state)
        self.assertFalse(self.audit.path.exists())

    def test_no_broker_mutation_from_audit_even_after_restart(self):
        self.bootstrap()
        self.project()
        self.project()
        self.assertEqual(self.transport.simulated_writes, 0)
        self.assertTrue(all(
            method == "GET" for method, _ in self.transport.calls
        ))


if __name__ == "__main__":
    unittest.main()
