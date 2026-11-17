"""Category 4: account recovery, lifetime risk, BE/partial and close accounting.

Only synthetic broker snapshots and transactions; no real network mutations.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import config
import main
from broker.account_reconciler import AccountReconciler, ReconcileError
from broker.practice_transport import BrokerReply, BrokerWriteDisabled, PracticeTransport
from execution.partial_close_engine import PartialCloseEngine
from execution.trade_management import ManagementLoop, TradeManagement
from risk.trade_lifecycle import LifecycleError, TradeRecord
from risk.trade_plan import InstrumentRules
from storage.intent_journal import IntentJournal, JournalError
from storage.lifecycle_state import LifecycleStore, ProcessLease, StateError


def D(x):
    return Decimal(str(x))


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


def make_trade(direction="LONG", *, current=None):
    signed = "100" if direction == "LONG" else "-100"
    return {
        "id": "11", "instrument": "EUR_USD", "state": "OPEN",
        "price": "1.1000", "openTime": "2026-11-15T10:00:00Z",
        "currentUnits": current if current is not None else signed,
        "initialUnits": signed,
        "clientExtensions": {"id": "algin-11", "tag": "algin"},
        "stopLossOrder": {"price": "1.0980" if direction == "LONG" else "1.1020"},
        "takeProfitOrder": {"price": "1.1060" if direction == "LONG" else "1.0940"},
    }


class RecoveryTransport:
    def __init__(self):
        self.cursor = "100"
        self.trades = []
        self.orders = []
        self.transactions = []
        self.calls = []
        self.fail_read = False

    def read(self, path, *, params=None):
        self.calls.append((path, params))
        if self.fail_read:
            raise TimeoutError("Broker timeout")
        if path == "":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "account": {
                    "id": "acct-1", "openTradeCount": len(self.trades),
                    "pendingOrderCount": len(self.orders),
                    "trades": self.trades, "orders": self.orders
                }
            })
        if path == "/transactions/sinceid":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "transactions": self.transactions,
            })
        raise AssertionError("Unexpected request " + path)


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.transport = RecoveryTransport()
        self.journal = IntentJournal("acct-1", directory=self.folder.name)
        self.store = LifecycleStore("acct-1", directory=self.folder.name)
        self.lease = ProcessLease("acct-1", directory=self.folder.name)
        self.service = AccountReconciler(
            "acct-1", transport=self.transport, journal=self.journal,
            store=self.store, lease=self.lease
        )
        self.rules = InstrumentRules(
            "EUR_USD", "CURRENCY", -4, 5, 2,
            D("0.01"), D("1000000"), D("0"), D("0.02")
        )

    def seed(self, direction="LONG", *, grade="A", with_grade=True):
        trade = make_trade(direction)
        self.transport.trades = [trade]
        signed = trade["initialUnits"]
        stop = trade["stopLossOrder"]["price"]
        tp = trade["takeProfitOrder"]["price"]
        request = {"order": {
            "instrument": "EUR_USD", "units": signed,
            "stopLossOnFill": {"price": stop},
            "takeProfitOnFill": {"price": tp},
        }}
        self.journal.begin(
            signal_id="signal-11", attempt_id="algin-11",
            request=request, risk_pct="1", margin_home="100",
            grade=grade if with_grade else None
        )
        self.journal.transition("algin-11", "IN_FLIGHT")
        self.journal.transition(
            "algin-11", "FILLED",
            trade_id="11", order_id="10",
            fill_price="1.1000", filled_units=signed,
            protection_verified=True,
        )
        return trade

    def bootstrap(self, direction="LONG"):
        self.seed(direction)
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        return report

    def update_broker(self, transaction, *, current=None, sl=None, close=False):
        self.transport.cursor = transaction["id"]
        self.transport.transactions = [transaction]
        if close:
            self.transport.trades = []
        else:
            if current is not None:
                self.transport.trades[0]["currentUnits"] = str(current)
            if sl is not None:
                self.transport.trades[0]["stopLossOrder"]["price"] = str(sl)


class StateTests(LifecycleFixture):
    def test_P31_initial_full_snapshot_clean_bootstrap(self):
        with self.service:
            report = self.service.run()
        self.assertEqual(report.cursor, "100")
        self.assertEqual(report.owned_open_trades, ())
        self.assertFalse(report.entry_allowed)
        self.assertEqual(self.store.load()["account_id"], "acct-1")

    def test_P31_known_trade_adopted_from_verified_fill_and_original_risk(self):
        self.bootstrap()
        state = self.store.load()
        trade = TradeRecord.from_dict(state["trades"]["11"])
        self.assertEqual(trade.grade, "A")
        self.assertEqual(trade.initial_risk_distance, D("0.002"))
        self.assertEqual(trade.initial_stop, D("1.0980"))
        self.assertEqual(trade.risk_pct, D("1"))
        self.assertEqual(trade.initial_units, D("100"))

    def test_P31_missing_journal_grade_never_guessed(self):
        self.seed(with_grade=False)
        with self.service:
            report = self.service.run()
        self.assertIn("Unattributed broker trade 11", report.blocked_reasons)
        self.assertNotIn("11", self.store.load()["trades"])

    def test_P31_manual_trade_never_adopted(self):
        foreign = make_trade()
        foreign["clientExtensions"] = {"id": "manual", "tag": "other"}
        self.transport.trades = [foreign]
        with self.service:
            report = self.service.run()
        self.assertTrue(report.blocked_reasons)
        self.assertEqual(self.store.load()["trades"], {})

    def test_P31_pending_orders_block_entry_even_empty_account(self):
        self.transport.orders = [{"id": "22", "instrument": "EUR_USD"}]
        with self.service:
            report = self.service.run()
        self.assertIn("Unreconciled pending broker orders", report.blocked_reasons)

    def test_P31_broker_GET_failure_does_not_migrate_or_mutate(self):
        self.transport.fail_read = True
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertIsNone(self.store.load())

    def test_P31_mismatched_account_and_duplicate_trade_ids_fail(self):
        trade = make_trade()
        self.transport.trades = [trade, trade]
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.transport.trades = []
        self.transport.cursor = "invalid"
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()

    def test_P32_atomic_state_roundtrip_and_checksum(self):
        self.bootstrap()
        state = self.store.load()
        self.assertEqual(state["cursor"], "100")
        self.store.mutate(lambda s: s["cooldowns"].update({
            "EUR_USD": "2026-11-15T10:30:00Z"
        }), expected_cursor="100")
        self.assertIn("EUR_USD", self.store.load()["cooldowns"])
        contents = json.loads(self.store.path.read_text())
        contents["data"]["cursor"] = "999"
        self.store.path.write_text(json.dumps(contents))
        with self.assertRaises(StateError):
            self.store.load()

    def test_P32_atomic_replace_error_preserves_previous_file(self):
        self.bootstrap()
        before = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("disk full")):
            with self.assertRaises(StateError):
                self.store.mutate(lambda s: s["cursor"] == "200")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_P34_wrong_account_environment_and_corruption(self):
        self.bootstrap()
        other = LifecycleStore("acct-1", environment="live", directory=self.folder.name)
        self.assertIsNone(other.load())
        raw = json.loads(self.store.path.read_text())
        raw["data"]["environment"] = "live"
        from storage.lifecycle_state import _digest
        raw["sha256"] = _digest(raw["data"])
        self.store.path.write_text(json.dumps(raw))
        with self.assertRaises(StateError):
            self.store.load()

    def test_P34_symlink_state_is_blocked(self):
        other = Path(self.folder.name) / "outside"
        other.write_text("{}")
        self.store.path.parent.mkdir(parents=True)
        self.store.path.symlink_to(other)
        with self.assertRaises(StateError):
            self.store.load()

    def test_P35_one_process_lease_at_a_time_and_safe_release(self):
        competing = ProcessLease("acct-1", directory=self.folder.name)
        with self.lease:
            with self.assertRaises(StateError):
                competing.acquire()
        with competing:
            self.assertTrue(competing.path.exists())
        self.assertFalse(competing.path.exists())

    def test_P35_stale_lock_not_automatically_removed(self):
        self.lease.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease.path.write_text("orphaned")
        with self.assertRaises(StateError):
            self.lease.acquire()
        self.assertTrue(self.lease.path.exists())

    def test_P35_reconcile_requires_process_lease(self):
        with self.assertRaises(ReconcileError):
            self.service.run()


class TradeLifecycleTests(LifecycleFixture):
    def test_P37_long_R_negative_and_positive_from_original_distance(self):
        self.bootstrap()
        trade = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(trade.favorable_r(bid="1.0980", ask="1.0982"), D("-1"))
        self.assertEqual(trade.favorable_r(bid="1.1020", ask="1.1022"), D("1"))
        self.assertEqual(trade.initial_risk_distance, D("0.0020"))

    def test_P37_short_R_uses_ask_not_bid(self):
        self.bootstrap("SHORT")
        trade = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(trade.favorable_r(bid="1.0978", ask="1.0980"), D("1"))
        self.assertEqual(trade.favorable_r(bid="1.1020", ask="1.1022"), D("-1.1"))
        self.assertEqual(trade.initial_risk_distance, D("0.0020"))

    def test_P38_BE_requires_favorable_move_and_does_not_worsen_SL(self):
        self.bootstrap()
        trade = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertIsNone(trade.break_even_target(bid="1.1010", ask="1.1012"))
        self.assertEqual(
            trade.break_even_target(bid="1.1030", ask="1.1032"),
            trade.entry_price
        )
        moved = replace(trade, current_stop=D("1.1010"))
        self.assertIsNone(moved.break_even_target(
            bid="1.1030", ask="1.1032"
        ))

    def test_P38_stage_BE_is_durable_no_broker_write_and_no_duplicate(self):
        self.bootstrap()
        management = TradeManagement(self.service)
        with self.service:
            action = management.stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.assertFalse(action["broker_write_attempted"])
        self.assertEqual(action["stopLoss"]["price"], "1.1000")
        self.assertEqual(self.store.load()["trades"]["11"]["be_state"], "INTENT")
        with self.service:
            with self.assertRaises(ReconcileError):
                management.stage_break_even(
                    "11", self.rules, bid="1.1030", ask="1.1032"
                )

    def test_P38_BE_confirms_only_after_broker_transaction_and_snapshot(self):
        self.bootstrap()
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_break_even("11", self.rules, bid="1.1030", ask="1.1032")
        self.assertEqual(self.store.load()["trades"]["11"]["be_state"], "INTENT")
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "STOP_LOSS_ORDER",
            "tradeID": "11", "price": "1.1000"
        }, sl="1.1000")
        with self.service:
            result = self.service.run()
        self.assertFalse(result.blocked_reasons)
        saved = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(saved.be_state, "CONFIRMED")
        self.assertEqual(saved.current_stop, saved.entry_price)
        self.assertEqual(saved.initial_risk_distance, D("0.0020"))
        with self.service:
            action = manager.preview("11", self.rules, bid="1.1030", ask="1.1032")
        self.assertIsNone(action.be_target)

    def test_P38_unattributed_stop_change_blocks_state_and_cursor(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1",
            "type": "STOP_LOSS_ORDER", "tradeID": "11", "price": "1.1000"
        }, sl="1.1000")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_P39_fractional_short_close_units_always_positive(self):
        self.bootstrap("SHORT")
        manager = TradeManagement(self.service)
        with self.service:
            advice = manager.preview(
                "11", self.rules, bid="1.0975", ask="1.0977"
            )
            action = manager.stage_partial(
                "11", self.rules, bid="1.0975", ask="1.0977"
            )
        self.assertGreater(advice.current_r, 1)
        self.assertEqual(action["units"], "50.00")
        self.assertFalse(action["broker_write_attempted"])
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "INTENT"
        )

    def test_P39_partial_broker_confirmation_no_duplicate_phase(self):
        self.bootstrap("SHORT")
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_partial(
                "11", self.rules, bid="1.0975", ask="1.0977"
            )
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "tradeReduced": {
                "tradeID": "11", "units": "50", "realizedPL": "10"
            }
        }, current="-50")
        with self.service:
            result = self.service.run()
        self.assertFalse(result.blocked_reasons)
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(record.current_units, D("-50"))
        self.assertEqual(record.net_pl_home, D("10"))
        self.assertEqual(record.partial_phases["phase1"]["state"], "CONFIRMED")
        with self.service:
            with self.assertRaises(LifecycleError):
                manager.stage_partial(
                    "11", self.rules, bid="1.0975", ask="1.0977"
                )

    def test_P39_long_partial_wrong_effect_sign_preserves_intent_and_cursor(self):
        self.bootstrap("LONG")
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "reason": "MARKET_ORDER_TRADE_CLOSE",
            "tradeReduced": {
                "tradeID": "11", "units": "50", "realizedPL": "10"
            }
        }, current="50")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        state = self.store.load()
        self.assertEqual(state["cursor"], "100")
        self.assertEqual(state["trades"]["11"]["partial_phases"]["phase1"]["state"],
                         "INTENT")
        self.transport.transactions[0]["tradeReduced"]["units"] = "-50"
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "101")
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "CONFIRMED"
        )

    def test_P39_short_partial_wrong_effect_sign_preserves_intent_and_cursor(self):
        self.bootstrap("SHORT")
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_partial(
                "11", self.rules, bid="1.0975", ask="1.0977"
            )
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "reason": "MARKET_ORDER_TRADE_CLOSE",
            "tradeReduced": {
                "tradeID": "11", "units": "-50", "realizedPL": "10"
            }
        }, current="-50")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")
        self.transport.transactions[0]["tradeReduced"]["units"] = "50"
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(self.store.load()["trades"]["11"]["current_units"], "-50")

    def test_P39_missing_partial_intent_does_not_adopt_reduction(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "tradeReduced": {"tradeID": "11", "units": "-30", "realizedPL": "2"}
        }, current="70")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_P39_partial_amount_larger_than_remaining_is_rejected(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "tradeReduced": {"tradeID": "11", "units": "-200", "realizedPL": "2"}
        }, current="100")
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()

    def test_P40_close_updates_PNL_cooldown_once_only(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "-50"}
            ]
        }, close=True)
        with self.service:
            first = self.service.run()
            second = self.service.run()
        self.assertEqual(first.new_closed_trade_ids, ("11",))
        self.assertEqual(second.new_closed_trade_ids, ())
        state = self.store.load()
        self.assertEqual(state["cursor"], "101")
        self.assertEqual(len(state["counted_closures"]), 1)
        self.assertEqual(state["closed_results"]["11"]["pl"], "-50")
        self.assertEqual(state["cooldowns"]["EUR_USD"], "2026-11-15T10:50:00Z")
        trade = TradeRecord.from_dict(state["trades"]["11"])
        self.assertTrue(trade.closed and trade.result_counted)
        self.assertEqual(trade.current_units, 0)

    def test_P40_long_full_close_rejects_same_direction_effect(self):
        self.bootstrap("LONG")
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "100", "realizedPL": "5"}
            ]
        }, close=True)
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertEqual(self.store.load()["counted_closures"], [])
        self.transport.transactions[0]["tradesClosed"][0]["units"] = "-100"
        with self.service:
            report = self.service.run()
        self.assertEqual(report.new_closed_trade_ids, ("11",))
        self.assertEqual(self.store.load()["counted_closures"], ["101"])

    def test_P40_short_full_close_rejects_same_direction_effect(self):
        self.bootstrap("SHORT")
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "5"}
            ]
        }, close=True)
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertEqual(self.store.load()["closed_results"], {})
        self.transport.transactions[0]["tradesClosed"][0]["units"] = "100"
        with self.service:
            report = self.service.run()
        self.assertEqual(report.new_closed_trade_ids, ("11",))
        self.assertEqual(self.store.load()["closed_results"]["11"]["pl"], "5")

    def test_P40_missing_close_tx_does_not_mark_closed(self):
        self.bootstrap()
        self.transport.trades = []
        with self.service:
            report = self.service.run()
        self.assertIn("Open trade missing without confirmed close 11",
                      report.blocked_reasons)
        self.assertFalse(self.store.load()["trades"]["11"]["closed"])

    def test_P40_duplicate_tx_replay_fails_closed(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "10"}
            ]
        }, close=True)
        with self.service:
            self.service.run()
        self.transport.cursor = "102"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "10"}
            ]
        }]
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(len(self.store.load()["counted_closures"]), 1)

    def test_P40_missing_realizedPL_blocks_close_counting(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [{"tradeID": "11", "units": "-100"}]
        }, close=True)
        with self.service:
            with self.assertRaises((ReconcileError, ValueError, KeyError)):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_P31_transaction_cursor_and_account_mismatch_block(self):
        self.bootstrap()
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "other", "type": "FINANCING"
        }]
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.transport.cursor = "99"
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()


class OperationalIntegrationTests(LifecycleFixture):
    def test_P36_management_loop_does_not_consult_entry_sessions(self):
        with patch("main.MarketSessions.can_trade",
                   side_effect=AssertionError("session must not gate management")):
            report = ManagementLoop(self.service).run_once()
        self.assertEqual(report.cursor, "100")

    def test_P36_manage_once_CLI_is_read_only_and_session_independent(self):
        with (
            patch.object(config, "API_KEY", "test-token"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
            patch("main.AccountReconciler", return_value=self.service),
            patch("main.MarketSessions.can_trade",
                  side_effect=AssertionError("sessions do not gate recovery"))
        ):
            self.assertEqual(main.main(["--manage-once"]), 0)
        self.assertEqual(self.transport.calls[0][0], "")

    def test_P36_broker_uncertainty_makes_manage_command_fail_closed(self):
        self.transport.fail_read = True
        with (
            patch.object(config, "API_KEY", "test-token"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
            patch("main.AccountReconciler", return_value=self.service)
        ):
            self.assertEqual(main.main(["--manage-once"]), 3)

    def test_P38_P39_partial_close_compatibility_never_writes(self):
        self.bootstrap()
        rec = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        engine = PartialCloseEngine(executor=object())
        self.assertEqual(
            engine.preview(rec, self.rules, bid="1.1030", ask="1.1032"),
            D("50.00")
        )
        with self.assertRaises(RuntimeError):
            engine.check_and_execute()

    def test_P19_P31_actual_transport_remains_hard_disabled(self):
        from unittest.mock import Mock
        fake_session = Mock()
        transport = PracticeTransport(session=fake_session)
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            with self.assertRaises(BrokerWriteDisabled):
                transport.submit_market_order({"order": {"type": "MARKET"}})
            with self.assertRaises(BrokerWriteDisabled):
                transport._call("PUT", "/trades/11/close", payload={"units": "10"})
        fake_session.request.assert_not_called()


    def test_P31_journal_unknown_freezes_entry_after_restart(self):
        self.journal.begin(
            signal_id="s-new", attempt_id="algin-unknown",
            request={"order": {"instrument": "EUR_USD", "units": "10"}},
            risk_pct="0.5", margin_home="10", grade="B"
        )
        self.journal.transition("algin-unknown", "IN_FLIGHT")
        self.journal.transition("algin-unknown", "UNKNOWN")
        with self.service:
            report = self.service.run()
        self.assertTrue(any("Ambiguous broker submission" in x
                            for x in report.blocked_reasons))
        self.assertFalse(report.entry_allowed)

    def test_P31_invalid_pending_broker_order_id_blocks_state(self):
        self.transport.orders = [{"instrument": "EUR_USD"}]
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertIsNone(self.store.load())

    def test_P40_ONE_broker_transaction_can_close_two_known_trades(self):
        self.bootstrap()
        original = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        other = replace(
            original, trade_id="12", attempt_id="algin-12",
            instrument="GBP_USD"
        )
        self.store.mutate(
            lambda state: state["trades"].update({"12": other.to_dict()})
        )
        self.transport.cursor = "101"
        self.transport.trades = []
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "-15"},
                {"tradeID": "12", "units": "-100", "realizedPL": "20"}
            ]
        }]
        with self.service:
            report = self.service.run()
        self.assertEqual(set(report.new_closed_trade_ids), {"11", "12"})
        state = self.store.load()
        self.assertEqual(state["counted_closures"], ["101"])
        self.assertEqual(set(state["closed_results"]), {"11", "12"})
        self.assertEqual(state["cooldowns"], {
            "EUR_USD": "2026-11-15T10:50:00Z"
        })
        with self.service:
            self.assertEqual(self.service.run().new_closed_trade_ids, ())

    def test_P38_BE_unknown_remains_reserved_and_is_not_retried(self):
        self.bootstrap()
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_break_even("11", self.rules,
                                     bid="1.1030", ask="1.1032")
            manager.mark_unknown("11", operation="BE")
            self.assertEqual(
                self.store.load()["trades"]["11"]["be_state"], "UNKNOWN"
            )
            with self.assertRaises(ReconcileError):
                manager.stage_break_even("11", self.rules,
                                         bid="1.1030", ask="1.1032")

    def test_P39_partial_unknown_reservation_survives_restart(self):
        self.bootstrap("SHORT")
        manager = TradeManagement(self.service)
        with self.service:
            manager.stage_partial("11", self.rules,
                                  bid="1.0975", ask="1.0977")
            manager.mark_unknown("11", operation="PARTIAL", phase="phase1")
        restarted = AccountReconciler(
            "acct-1", transport=self.transport,
            journal=IntentJournal("acct-1", directory=self.folder.name),
            store=LifecycleStore("acct-1", directory=self.folder.name),
            lease=ProcessLease("acct-1", directory=self.folder.name)
        )
        with restarted:
            report = restarted.run()
        self.assertTrue(report.blocked_reasons)
        self.assertEqual(
            restarted.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "UNKNOWN"
        )

    def test_P35_perpetual_main_loop_holds_lease_through_scan(self):
        self.bootstrap()
        checks = []
        def scan_under_lease():
            checks.append(self.lease.path.exists())
            return 7
        with (
            patch.object(config, "API_KEY", "dummy"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
            patch("main.AccountReconciler", return_value=self.service),
            patch("main.scan_once", side_effect=scan_under_lease),
        ):
            result = main.main([])
        self.assertEqual(result, 7)
        self.assertEqual(checks, [True])
        self.assertFalse(self.lease.path.exists())



    def test_P40_malformed_close_raises_reconcile_error_and_preserves_cursor(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [{"tradeID": "11", "units": "-100"}]
        }, close=True)
        with self.service:
            with self.assertRaises(ReconcileError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_P37_locked_bid_ask_not_valid_for_R(self):
        self.bootstrap()
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        with self.assertRaises(LifecycleError):
            record.favorable_r(bid="1.1000", ask="1.1000")



if __name__ == "__main__":
    unittest.main()
