"""Phase B category 07: GET-reconciled synthetic BE/partial/full-close.

No real credentials, HTTP, or broker writes. Fixtures exercise the existing
LifecycleStore, IntentJournal, AccountReconciler and Management contract.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import config
from broker.account_reconciler import AccountReconciler, ReconcileError
from broker.practice_transport import PracticeTransport, BrokerWriteDisabled
from execution.offline_management_engine import (
    ManagementContractError, ManagementCommand,
    OfflineManagementEngine, SyntheticLifecycleBroker, build_command,
)
from execution.trade_management import TradeManagement
from risk.trade_lifecycle import TradeRecord, LifecycleError
from storage.lifecycle_state import LifecycleStore, ProcessLease, StateError, _digest
from storage.intent_journal import IntentJournal, JournalError
from test_category4 import LifecycleFixture, D


class OfflineManagementFixture(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.transport = SyntheticLifecycleBroker()
        self.service.transport = self.transport
        self.engine = OfflineManagementEngine(self.service)
        self.manager = TradeManagement(self.service)

    def be(self):
        return self.engine.stage_and_simulate_be(
            "11", self.rules, bid="1.1030", ask="1.1032",
            transport=self.transport,
        )

    def partial(self):
        return self.engine.stage_and_simulate_partial(
            "11", self.rules, bid="1.1030", ask="1.1032",
            transport=self.transport,
        )

    def full(self, *, source="SYSTEM"):
        return self.engine.stage_and_simulate_close(
            "11", transport=self.transport, source=source,
        )

    def broker_stop_close(self, *, reason="STOP_LOSS_ORDER"):
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1",
            "type": "ORDER_FILL", "reason": reason,
            "time": "2026-11-16T12:00:00Z",
            "pl": "-10", "financing": "0",
            "commission": "0", "guaranteedExecutionFee": "0",
            "halfSpreadCost": "0",
            "tradesClosed": [{
                "tradeID": "11", "units": "100",
                "realizedPL": "-10", "financing": "0",
                "guaranteedExecutionFee": "0", "halfSpreadCost": "0",
            }],
        }]
        self.transport.trades.clear()


class ManagementActionTests(OfflineManagementFixture):
    def test_full_lifecycle_be_partial_close_and_retirement(self):
        self.bootstrap()
        with self.service:
            be = self.be()
            part = self.partial()
            close = self.full()
        self.assertEqual((be.status, part.status, close.status),
                         ("CONFIRMED", "CONFIRMED", "CONFIRMED"))
        self.assertEqual(
            (be.confirmed_transaction_id,
             part.confirmed_transaction_id,
             close.confirmed_transaction_id),
            ("101", "103", "105"),
        )
        self.assertEqual(self.transport.simulated_writes, 3)
        saved = self.store.load()
        record = TradeRecord.from_dict(saved["trades"]["11"])
        self.assertTrue(record.closed and record.result_counted)
        self.assertEqual(record.be_transaction_id, "101")
        self.assertEqual(record.partial_phases["phase1"]["transaction_id"], "103")
        self.assertEqual(record.close_transaction_ids, ("103", "105"))
        self.assertEqual(saved["close_intents"]["11"]["status"], "CONFIRMED")
        self.assertEqual(saved["processed_transactions"],
                         ["101", "102", "103", "104", "105"])
        self.assertEqual(saved["counted_closures"], ["105"])
        self.assertEqual(len(saved["financial_ledger"]["events"]), 5)
        self.assertTrue(self.journal.get("algin-11")["retired"])
        self.assertEqual(self.journal.get("algin-11")["closure_transaction_id"], "105")
        self.assertFalse(any(
            name == "PUT" or name == "POST" for name, _ in self.transport.calls
        ))
        with self.service:
            replay = self.service.run()
        self.assertFalse(replay.blocked_reasons)
        self.assertEqual(replay.new_closed_trade_ids, ())
        self.assertEqual(self.transport.simulated_writes, 3)

    def test_short_partial_close_always_uses_positive_units(self):
        self.bootstrap("SHORT")
        with self.service:
            response = self.engine.stage_and_simulate_partial(
                "11", self.rules, bid="1.0975", ask="1.0977",
                transport=self.transport,
            )
        self.assertEqual(response.status, "CONFIRMED")
        self.assertEqual(self.transport.trades[0]["currentUnits"], "-50.00")
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(record.current_units, D("-50"))
        self.assertEqual(record.partial_phases["phase1"]["units"], "50.00")

    def test_be_command_uses_documented_dependent_order_route(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        command = build_command(record, "BE")
        self.assertEqual(command.method, "PUT")
        self.assertEqual(command.path, "/trades/11/orders")
        self.assertEqual(command.body, {"stopLoss": {"price": "1.1000"}})
        self.assertEqual(self.transport.simulated_writes, 0)

    def test_partial_and_full_command_paths_and_units(self):
        self.bootstrap()
        with self.service:
            action = self.manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        command = build_command(record, "PARTIAL", phase="phase1")
        self.assertEqual(command.path, "/trades/11/close")
        self.assertEqual(command.body, {"units": "50.00"})
        self.assertEqual(action["units"], "50.00")
        self.assertEqual(self.transport.simulated_writes, 0)

    def test_full_close_command_all_only_and_durable_source(self):
        self.bootstrap()
        with self.service:
            staged = self.manager.stage_close("11", source="MANUAL")
        state = self.store.load()
        cmd = build_command(
            TradeRecord.from_dict(state["trades"]["11"]), "CLOSE",
            close_intent=state["close_intents"]["11"]
        )
        self.assertEqual(cmd.path, "/trades/11/close")
        self.assertEqual(cmd.body, {"units": "ALL"})
        self.assertEqual(staged["status"], "INTENT")
        self.assertEqual(state["close_intents"]["11"]["source"], "MANUAL")
        self.assertEqual(self.transport.simulated_writes, 0)

    def test_second_be_never_reissues_even_at_valid_price(self):
        self.bootstrap()
        with self.service:
            first = self.be()
            self.assertEqual(first.status, "CONFIRMED")
            with self.assertRaises(LifecycleError):
                self.be()
        self.assertEqual(self.transport.simulated_writes, 1)

    def test_duplicate_explicit_close_intent_blocks_durable_resend(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            with self.assertRaises((ReconcileError, LifecycleError)):
                self.manager.stage_close("11")
        self.assertEqual(self.store.load()["close_intents"]["11"]["status"], "INTENT")
        self.assertEqual(self.transport.simulated_writes, 0)

    def test_lost_ack_be_is_confirmed_by_get_replay_without_resend(self):
        self.bootstrap()
        self.transport.effect_mode = "LOST_ACK"
        with self.service:
            result = self.be()
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.confirmed_transaction_id, "101")
        self.assertEqual(self.transport.simulated_writes, 1)
        self.assertEqual(
            self.store.load()["trades"]["11"]["be_state"], "CONFIRMED"
        )

    def test_lost_ack_partial_is_confirmed_by_get_replay_once(self):
        self.bootstrap()
        self.transport.effect_mode = "LOST_ACK"
        with self.service:
            result = self.partial()
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.confirmed_transaction_id, "102")
        self.assertEqual(self.transport.simulated_writes, 1)
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "CONFIRMED"
        )

    def test_lost_ack_full_close_is_confirmed_by_get_replay(self):
        self.bootstrap()
        self.transport.effect_mode = "LOST_ACK"
        with self.service:
            result = self.full()
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.confirmed_transaction_id, "102")
        self.assertEqual(self.transport.simulated_writes, 1)
        self.assertTrue(self.journal.get("algin-11")["retired"])

    def test_timeout_before_effect_stays_unknown_without_resubmission(self):
        self.bootstrap()
        self.transport.effect_mode = "TIMEOUT_BEFORE_EFFECT"
        with self.service:
            result = self.be()
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(self.transport.simulated_writes, 1)
        self.assertEqual(self.store.load()["trades"]["11"]["be_state"], "UNKNOWN")
        restarted = AccountReconciler(
            "acct-1", transport=self.transport,
            journal=IntentJournal("acct-1", directory=self.folder.name),
            store=LifecycleStore("acct-1", directory=self.folder.name),
            lease=ProcessLease("acct-1", directory=self.folder.name),
        )
        with restarted:
            report = restarted.run()
        self.assertTrue(report.blocked_reasons)
        self.assertEqual(self.transport.simulated_writes, 1)

    def test_absent_broker_close_transaction_cannot_prove_missing_trade(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            self.manager.mark_unknown("11", operation="CLOSE")
        self.transport.trades = []
        with self.service:
            report = self.service.run()
        self.assertTrue(report.blocked_reasons)
        self.assertFalse(self.store.load()["trades"]["11"]["closed"])
        self.assertEqual(
            self.store.load()["close_intents"]["11"]["status"], "UNKNOWN"
        )

    def test_broker_stop_supersedes_explicit_close_without_forging_origin(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            self.manager.mark_unknown("11", operation="CLOSE")
        self.broker_stop_close()
        with self.service:
            result = self.service.run()
        self.assertFalse(result.blocked_reasons)
        close = self.store.load()["close_intents"]["11"]
        self.assertEqual(close["status"], "SUPERSEDED")
        self.assertEqual(close["confirmed_transaction_id"], "101")
        self.assertTrue(self.journal.get("algin-11")["retired"])

    def test_broker_stop_supersedes_pending_be(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.broker_stop_close()
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertTrue(record.closed)
        self.assertEqual(record.be_state, "SUPERSEDED")
        self.assertIsNone(record.be_transaction_id)

    def test_broker_take_profit_supersedes_pending_partial(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.broker_stop_close(reason="TAKE_PROFIT_ORDER")
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "SUPERSEDED"
        )

    def test_wrong_reason_for_explicit_close_keeps_cursor_and_reservation(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
        self.broker_stop_close(reason="UNRECOGNIZED_EVENT")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertEqual(self.store.load()["close_intents"]["11"]["status"], "INTENT")

    def test_stop_loss_on_fill_cannot_confirm_staged_break_even(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "STOP_LOSS_ORDER",
            "tradeID": "11", "price": "1.1000", "reason": "ON_FILL",
        }]
        self.transport.trades[0]["stopLossOrder"]["price"] = "1.1000"
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_stop_loss_fill_cannot_be_misattributed_as_staged_partial(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1",
            "type": "ORDER_FILL", "reason": "STOP_LOSS_ORDER",
            "tradeReduced": {
                "tradeID": "11", "units": "50", "realizedPL": "0",
            }
        }]
        self.transport.trades[0]["currentUnits"] = "50"
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_corrupt_close_intent_identity_fails_state_validation(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
        envelope = json.loads(self.store.path.read_text())
        envelope["data"]["close_intents"]["11"]["cursor_before"] = "999"
        envelope["sha256"] = _digest(envelope["data"])
        self.store.path.write_text(json.dumps(envelope))
        with self.assertRaises(StateError):
            self.store.load()

    def test_close_rejects_unknown_source_without_persisting(self):
        self.bootstrap()
        with self.service:
            with self.assertRaises(LifecycleError):
                self.manager.stage_close("11", source="UNVERIFIED")
        self.assertEqual(self.store.load()["close_intents"], {})

    def test_untrusted_transport_is_blocked_before_any_durable_stage(self):
        self.bootstrap()
        class TransportSubclass(SyntheticLifecycleBroker):
            def submit_synthetic(self, command):
                raise AssertionError("Must not reach an unsafe override")
        untrusted = TransportSubclass()
        with self.service:
            blocked = self.engine.stage_and_simulate_close(
                "11", transport=untrusted
            )
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(self.store.load()["close_intents"], {})
        self.assertEqual(self.transport.simulated_writes, 0)

    def test_real_practice_transport_cannot_stage_a_mutation(self):
        self.bootstrap()
        original = self.service.transport
        self.service.transport = PracticeTransport()
        with self.service:
            blocked = self.engine.stage_and_simulate_close(
                "11", transport=self.service.transport
            )
        self.service.transport = original
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(self.store.load()["close_intents"], {})

    def test_config_write_release_blocks_even_synthetic_stage(self):
        self.bootstrap()
        with self.service, patch.object(config, "ORDER_WRITES_ENABLED", True):
            blocked = self.engine.stage_and_simulate_close(
                "11", transport=self.transport
            )
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(self.store.load()["close_intents"], {})

    def test_foreign_trade_appearing_after_stage_blocks_synthetic_send(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            state = self.store.load()
            command = build_command(
                TradeRecord.from_dict(state["trades"]["11"]), "CLOSE",
                close_intent=state["close_intents"]["11"],
            )
            foreign = dict(self.transport.trades[0])
            foreign["id"] = "12"
            foreign["clientExtensions"] = {
                "id": "other-operator", "tag": "foreign"
            }
            self.transport.trades.append(foreign)
            result = self.engine._operate(command, self.transport)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(self.transport.simulated_writes, 0)
        self.assertEqual(self.store.load()["close_intents"]["11"]["status"],
                         "UNKNOWN")
        self.assertFalse(self.store.load()["trades"]["11"]["closed"])

    def test_changed_protection_after_stage_blocks_synthetic_send(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_close("11")
            state = self.store.load()
            command = build_command(
                TradeRecord.from_dict(state["trades"]["11"]), "CLOSE",
                close_intent=state["close_intents"]["11"],
            )
            self.transport.trades[0]["takeProfitOrder"]["price"] = "1.1050"
            result = self.engine._operate(command, self.transport)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(self.transport.simulated_writes, 0)
        self.assertEqual(self.store.load()["close_intents"]["11"]["status"],
                         "UNKNOWN")

    def test_crash_before_reconcile_replays_confirmed_partial_after_restart(self):
        self.bootstrap()
        with self.service:
            self.manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
            record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
            command = build_command(record, "PARTIAL", phase="phase1")
            self.transport.submit_synthetic(command)
            # Simulate process death after synthetic effect, before GET replay.
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "INTENT"
        )
        service = AccountReconciler(
            "acct-1", transport=self.transport,
            journal=IntentJournal("acct-1", directory=self.folder.name),
            store=LifecycleStore("acct-1", directory=self.folder.name),
            lease=ProcessLease("acct-1", directory=self.folder.name),
        )
        with service:
            report = service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(
            service.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "CONFIRMED"
        )
        self.assertEqual(self.transport.simulated_writes, 1)
        self.assertEqual(self.transport.cursor, "102")


if __name__ == "__main__":
    unittest.main()
