"""Category 23: post-bootstrap trade admission and crash-safe replay (offline)."""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from test_category4 import LifecycleFixture, make_trade
from broker.order_engine import OrderEngine
from broker.practice_transport import BrokerReply
from risk.trade_lifecycle import TradeRecord
from storage.lifecycle_state import StateError


class PostBootstrapAdmissionTests(LifecycleFixture):
    def start_empty(self):
        with self.service:
            result = self.service.run()
        self.assertFalse(result.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")

    def seed_filled_order(self, *, protection_verified=True):
        trade = make_trade()
        self.transport.trades = [trade]
        identity = {
            "id": "algin-11", "tag": "algin", "comment": "signal-11",
        }
        request = {"order": {
            "type": "MARKET", "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY", "instrument": "EUR_USD",
            "units": "100", "stopLossOnFill": {"price": "1.0980"},
            "takeProfitOnFill": {"price": "1.1060"},
            "clientExtensions": identity, "tradeClientExtensions": identity,
        }}
        self.journal.begin(
            signal_id="signal-11", attempt_id="algin-11",
            request=request, risk_pct="1", margin_home="100", grade="A",
        )
        self.journal.transition("algin-11", "IN_FLIGHT")
        self.journal.transition(
            "algin-11", "FILLED", trade_id="11", order_id="10",
            filled_units="100", fill_price="1.1000",
            protection_verified=protection_verified,
        )
        self.transport.cursor = "102"
        self.transport.transactions = [
            {
                "id": "101", "accountID": "acct-1", "type": "MARKET_ORDER",
            },
            {
                "id": "102", "accountID": "acct-1", "type": "ORDER_FILL",
                "orderID": "10", "clientOrderID": "algin-11",
                "instrument": "EUR_USD", "units": "100",
                "reason": "MARKET_ORDER", "time": "2026-11-15T10:00:00Z",
                "tradeOpened": {
                    "tradeID": "11", "units": "100", "price": "1.1000",
                    "guaranteedExecutionFee": "0",
                },
                "pl": "0", "financing": "0", "commission": "0",
                "guaranteedExecutionFee": "0",
            },
        ]

    def test_confirmed_fill_after_bootstrap_is_committed_once(self):
        self.start_empty()
        self.seed_filled_order()
        with self.service:
            first = self.service.run()
            second = self.service.run()
        self.assertEqual(first.cursor, "102")
        self.assertFalse(first.blocked_reasons)
        self.assertEqual(first.owned_open_trades, ("11",))
        self.assertFalse(second.blocked_reasons)
        self.assertEqual(second.owned_open_trades, ("11",))
        state = self.store.load()
        self.assertEqual(list(state["trades"]), ["11"])
        self.assertEqual(state["processed_transactions"], ["101", "102"])
        self.assertEqual(state["cursor"], "102")
        record = TradeRecord.from_dict(state["trades"]["11"])
        self.assertEqual(record.attempt_id, "algin-11")
        self.assertEqual(record.risk_pct, 1)
        self.assertFalse(first.entry_allowed)

    def test_crash_before_state_commit_replays_only_once(self):
        self.start_empty()
        self.seed_filled_order()
        original = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("synthetic disk failure")):
            with self.service, self.assertRaises(StateError):
                self.service.run()
        self.assertEqual(self.store.path.read_bytes(), original)
        with self.service:
            recovered = self.service.run()
            replay = self.service.run()
        self.assertEqual(recovered.owned_open_trades, ("11",))
        self.assertEqual(replay.new_closed_trade_ids, ())
        self.assertEqual(self.store.load()["processed_transactions"], ["101", "102"])
        self.assertEqual(len(self.store.load()["trades"]), 1)

    def test_manual_or_spoofed_trade_is_never_adopted(self):
        self.start_empty()
        self.seed_filled_order()
        self.transport.trades[0]["clientExtensions"] = {
            "id": "algin-11", "tag": "manual",
        }
        with self.service:
            report = self.service.run()
        self.assertTrue(report.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertEqual(self.store.load()["trades"], {})

    def test_missing_or_modified_protection_blocks_all_cursor_changes(self):
        self.start_empty()
        self.seed_filled_order()
        pristine = deepcopy(self.transport.trades[0])
        for changed in (None, "1.0900"):
            with self.subTest(stop_loss=changed):
                self.transport.trades[0] = deepcopy(pristine)
                if changed is None:
                    del self.transport.trades[0]["stopLossOrder"]
                else:
                    self.transport.trades[0]["stopLossOrder"]["price"] = changed
                with self.service:
                    result = self.service.run()
                self.assertTrue(result.blocked_reasons)
                self.assertEqual(self.store.load()["cursor"], "100")
                self.assertEqual(self.store.load()["trades"], {})

    def test_wrong_fill_order_units_or_price_blocks_admission(self):
        self.start_empty()
        self.seed_filled_order()
        baseline = deepcopy(self.transport.transactions[1])
        for field, changed in (
            ("orderID", "999"), ("units", "90"),
            ("clientOrderID", "not-ours"), ("instrument", "GBP_USD"),
        ):
            with self.subTest(field=field):
                self.transport.transactions[1] = deepcopy(baseline)
                self.transport.transactions[1][field] = changed
                with self.service:
                    result = self.service.run()
                self.assertTrue(result.blocked_reasons)
                self.assertEqual(self.store.load()["cursor"], "100")
        self.transport.transactions[1] = deepcopy(baseline)
        self.transport.transactions[1]["tradeOpened"]["price"] = "1.1001"
        with self.service:
            result = self.service.run()
        self.assertTrue(result.blocked_reasons)
        self.assertEqual(self.store.load()["trades"], {})

    def test_no_fresh_fill_does_not_authorize_adoption(self):
        self.start_empty()
        self.seed_filled_order()
        del self.transport.transactions[1]["tradeOpened"]
        with self.service:
            result = self.service.run()
        self.assertTrue(result.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_duplicate_opening_evidence_cannot_create_a_trade_twice(self):
        self.start_empty()
        self.seed_filled_order()
        duplicate = deepcopy(self.transport.transactions[1])
        duplicate["id"] = "103"
        self.transport.transactions.append(duplicate)
        self.transport.cursor = "103"
        with self.service:
            result = self.service.run()
        self.assertTrue(result.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertEqual(self.store.load()["trades"], {})

    def test_open_and_closed_before_lifecycle_commit_stays_blocked(self):
        self.start_empty()
        self.seed_filled_order()
        self.transport.trades = []
        with self.service:
            result = self.service.run()
        self.assertTrue(result.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_unverified_fill_can_recheck_protection_with_get_only(self):
        self.start_empty()
        self.seed_filled_order(protection_verified=False)
        with self.service:
            first = self.service.run()
        self.assertTrue(first.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")

        class TradeReader:
            calls = []

            def read(inner, path, *, params=None):
                inner.calls.append(path)
                if path == "/trades/11":
                    return BrokerReply(200, {"trade": self.transport.trades[0]})
                raise AssertionError("Recovery must not mutate broker")

        reader = TradeReader()
        engine = OrderEngine("acct-1", transport=reader, journal=self.journal)
        recovered = engine.reconcile("algin-11")
        self.assertTrue(recovered.protection_verified)
        self.assertEqual(reader.calls, ["/trades/11"])
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(report.owned_open_trades, ("11",))

    def test_recovery_does_not_upgrade_fill_that_reduced_other_positions(self):
        self.start_empty()
        self.seed_filled_order(protection_verified=False)
        self.journal.transition(
            "algin-11", "FILLED",
            trade_reduced={"tradeID": "99", "units": "-10"},
        )

        class ReadMustNotRun:
            def read(self, *args, **kwargs):
                raise AssertionError("Contradictory fill must not reach broker GET")

        engine = OrderEngine("acct-1", transport=ReadMustNotRun(), journal=self.journal)
        result = engine.reconcile("algin-11")
        self.assertFalse(result.protection_verified)
        self.assertFalse(self.journal.get("algin-11")["protection_verified"])

    def test_recovery_leaves_changed_protection_unverified(self):
        self.start_empty()
        self.seed_filled_order(protection_verified=False)
        bad_trade = deepcopy(self.transport.trades[0])
        bad_trade["takeProfitOrder"]["price"] = "1.1040"

        class ChangedProtection:
            def read(self, path, *, params=None):
                return BrokerReply(200, {"trade": bad_trade})

        engine = OrderEngine("acct-1", transport=ChangedProtection(), journal=self.journal)
        result = engine.reconcile("algin-11")
        self.assertFalse(result.protection_verified)
        self.assertFalse(self.journal.get("algin-11")["protection_verified"])


if __name__ == "__main__":
    import unittest
    unittest.main()
