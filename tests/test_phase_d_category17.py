"""Phase D Category 17: strict OANDA pending SL/TP reconciliation (offline).

Exercises broker full-account inventories; never calls OANDA or modifies orders.
"""
from __future__ import annotations

import copy
import unittest

from broker.account_reconciler import ReconcileError
from execution.trade_management import TradeManagement
from risk.trade_lifecycle import TradeRecord
from test_category4 import LifecycleFixture


class DependentOrderInventoryTests(LifecycleFixture):
    def attach_protections(self):
        """OANDA-shaped full trade plus two linked pending dependent orders."""
        trade = self.transport.trades[0]
        for field, summary_id, kind, oid in (
            ("stopLossOrder", "stopLossOrderID", "STOP_LOSS", "91"),
            ("takeProfitOrder", "takeProfitOrderID", "TAKE_PROFIT", "92"),
        ):
            trade[field].update({
                "id": oid, "tradeID": trade["id"],
                "type": kind, "state": "PENDING",
            })
            trade[summary_id] = oid
        self.transport.orders = [
            copy.deepcopy(trade["stopLossOrder"]),
            copy.deepcopy(trade["takeProfitOrder"]),
        ]

    def assert_untrusted_inventory(self):
        with self.service:
            report = self.service.run()
        self.assertIn("Unreconciled pending broker orders", report.blocked_reasons)
        self.assertFalse(report.entry_allowed)

    def test_owned_long_sl_tp_pending_are_not_foreign(self):
        self.bootstrap()
        self.attach_protections()
        with self.service:
            report = self.service.run()
        self.assertEqual(report.blocked_reasons, ())
        self.assertEqual(report.owned_open_trades, ("11",))
        self.assertEqual(report.cursor, "100")
        self.assertFalse(report.entry_allowed)

    def test_owned_short_sl_tp_pending_are_not_foreign(self):
        self.bootstrap("SHORT")
        self.attach_protections()
        with self.service:
            report = self.service.run()
        self.assertEqual(report.blocked_reasons, ())
        self.assertEqual(report.owned_open_trades, ("11",))
        self.assertEqual(self.store.load()["trades"]["11"]["current_units"], "-100")

    def test_verified_first_bootstrap_with_live_pending_protections(self):
        self.seed("LONG")
        self.attach_protections()
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(report.owned_open_trades, ("11",))
        self.assertEqual(self.store.load()["cursor"], "100")

    def test_pending_order_array_order_does_not_affect_match(self):
        self.bootstrap()
        self.attach_protections()
        self.transport.orders.reverse()
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)

    def test_legacy_zero_pending_snapshot_still_uses_trade_price_contract(self):
        self.bootstrap()
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(self.transport.orders, [])

    def test_foreign_or_changed_pending_order_never_whitelisted(self):
        self.bootstrap()
        self.attach_protections()
        originals = copy.deepcopy(self.transport.orders)
        cases = (
            ("wrong_trade_id", lambda o: o[0].update(tradeID="777")),
            ("unsupported_order_type", lambda o: o[0].update(type="LIMIT")),
            ("wrong_price", lambda o: o[0].update(price="1.0000")),
            ("cancelled_order", lambda o: o[0].update(state="CANCELLED")),
            ("missing_order_type", lambda o: o[0].pop("type")),
            ("wrong_order_id", lambda o: o[0].update(id="991")),
            ("missing_order_price", lambda o: o[0].pop("price")),
            ("invalid_order_price", lambda o: o[0].update(price="NaN")),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                self.transport.orders = copy.deepcopy(originals)
                mutate(self.transport.orders)
                self.assert_untrusted_inventory()
                self.assertEqual(self.store.load()["cursor"], "100")
        self.transport.orders = copy.deepcopy(originals)
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)

    def test_missing_protection_or_extra_order_always_blocks(self):
        self.bootstrap()
        self.attach_protections()
        valid = copy.deepcopy(self.transport.orders)
        self.transport.orders = valid[:1]
        self.assert_untrusted_inventory()
        self.transport.orders = valid + [{
            "id": "93", "tradeID": "11", "type": "LIMIT",
            "state": "PENDING", "price": "1.0800",
        }]
        self.assert_untrusted_inventory()
        self.transport.orders = valid + [{
            "id": "93", "tradeID": "77", "type": "STOP_LOSS",
            "state": "PENDING", "price": "1.0800",
        }]
        self.assert_untrusted_inventory()

    def test_identity_mismatch_on_trade_nested_order_is_not_trusted(self):
        self.bootstrap()
        self.attach_protections()
        trade = self.transport.trades[0]
        trade["stopLossOrder"]["id"] = "888"
        self.assert_untrusted_inventory()
        trade["stopLossOrder"]["id"] = "91"
        trade["stopLossOrderID"] = "777"
        self.assert_untrusted_inventory()

    def test_missing_linkage_in_trade_details_is_not_guessed(self):
        self.bootstrap()
        self.attach_protections()
        trade = self.transport.trades[0]
        del trade["stopLossOrder"]["tradeID"]
        self.assert_untrusted_inventory()
        trade["stopLossOrder"]["tradeID"] = "11"
        del trade["takeProfitOrder"]["state"]
        self.assert_untrusted_inventory()

    def test_pending_orders_from_unverified_trade_never_grant_ownership(self):
        self.bootstrap()
        self.attach_protections()
        self.transport.trades[0]["clientExtensions"]["tag"] = "foreign"
        with self.service:
            report = self.service.run()
        self.assertTrue(report.blocked_reasons)
        self.assertIn("Trade ownership mismatch 11", report.blocked_reasons)
        self.assertIn("Unreconciled pending broker orders", report.blocked_reasons)

    def test_untrusted_order_blocks_cursor_commit_and_replay_recovers(self):
        self.bootstrap()
        self.attach_protections()
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "ORDER_CANCEL",
        }]
        self.transport.orders[0]["price"] = "1.0000"
        self.assert_untrusted_inventory()
        state = self.store.load()
        self.assertEqual(state["cursor"], "100")
        self.assertEqual(state["processed_transactions"], [])
        self.transport.orders[0]["price"] = "1.0980"
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(report.cursor, "101")
        self.assertEqual(self.store.load()["processed_transactions"], ["101"])

    def test_be_replaced_stop_id_is_reconciled_using_new_order_identity(self):
        self.bootstrap()
        self.attach_protections()
        with self.service:
            TradeManagement(self.service).stage_break_even(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "STOP_LOSS_ORDER",
            "tradeID": "11", "reason": "REPLACEMENT", "price": "1.1000",
        }]
        trade = self.transport.trades[0]
        trade["stopLossOrder"]["price"] = "1.1000"
        trade["stopLossOrder"]["id"] = "101"
        trade["stopLossOrderID"] = "101"
        self.transport.orders[0] = copy.deepcopy(trade["stopLossOrder"])
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        record = TradeRecord.from_dict(self.store.load()["trades"]["11"])
        self.assertEqual(record.be_state, "CONFIRMED")
        self.assertEqual(record.current_stop, record.entry_price)
        self.assertEqual(record.be_transaction_id, "101")

    def test_pending_protection_survives_partial_and_disappears_on_close(self):
        self.bootstrap()
        self.attach_protections()
        with self.service:
            TradeManagement(self.service).stage_partial(
                "11", self.rules, bid="1.1030", ask="1.1032"
            )
        self.transport.cursor = "101"
        self.transport.transactions = [{
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "reason": "MARKET_ORDER_TRADE_CLOSE",
            "tradeReduced": {
                "tradeID": "11", "units": "-50", "realizedPL": "3",
            },
        }]
        self.transport.trades[0]["currentUnits"] = "50"
        with self.service:
            partial = self.service.run()
        self.assertFalse(partial.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "101")
        self.assertEqual(
            self.store.load()["trades"]["11"]["partial_phases"]["phase1"]["state"],
            "CONFIRMED",
        )
        self.transport.cursor = "102"
        self.transport.transactions = [{
            "id": "102", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-15T10:20:00Z",
            "tradesClosed": [{
                "tradeID": "11", "units": "-50", "realizedPL": "2",
            }],
        }]
        self.transport.trades = []
        self.transport.orders = []
        with self.service:
            terminal = self.service.run()
        self.assertFalse(terminal.blocked_reasons)
        self.assertEqual(terminal.new_closed_trade_ids, ("11",))
        self.assertEqual(self.store.load()["closed_results"]["11"]["pl"], "5")
        self.assertTrue(self.journal.get("algin-11")["retired"])

    def test_invalid_order_cannot_be_fixed_by_matching_price_alone(self):
        self.bootstrap()
        self.attach_protections()
        self.transport.orders[0]["tradeID"] = "999"
        self.transport.orders[0]["price"] = "1.0980"
        self.assert_untrusted_inventory()


if __name__ == "__main__":
    unittest.main()
