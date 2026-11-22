"""Category 24: dependent SL/TP orders must not inflate account risk (offline)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from test_category4 import LifecycleFixture
from execution.trade_management import TradeManagement
from risk.reconciled_account import RiskEvidenceError, build_risk_readiness
from risk.trade_plan import AccountFunds


NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
D = Decimal


class ProtectionRiskContractTests(LifecycleFixture):
    def attach_protections(self):
        trade = self.transport.trades[0]
        for field, summary_id, kind, order_id in (
            ("stopLossOrder", "stopLossOrderID", "STOP_LOSS", "91"),
            ("takeProfitOrder", "takeProfitOrderID", "TAKE_PROFIT", "92"),
        ):
            trade[field].update({
                "id": order_id, "tradeID": trade["id"],
                "type": kind, "state": "PENDING",
            })
            trade[summary_id] = order_id
        self.transport.orders = [
            deepcopy(trade["stopLossOrder"]),
            deepcopy(trade["takeProfitOrder"]),
        ]

    def risk(self, report, *, pending_count=None):
        n = len(self.transport.orders) if pending_count is None else pending_count
        funds = AccountFunds(
            "acct-1", "USD", D("10000"), D("10000"), D("8000"), D("0"),
            len(self.transport.trades), n, report.cursor,
        )
        return build_risk_readiness(
            funds, self.store.load(), self.journal.snapshot(),
            broker_trades=tuple(self.transport.trades),
            broker_orders=tuple(self.transport.orders), report=report, now=NOW,
        )

    def verified_report(self, direction="LONG"):
        self.bootstrap(direction)
        self.attach_protections()
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(report.verified_protective_order_ids, ("91", "92"))
        return report

    def test_long_sl_tp_preserve_one_original_risk_reservation(self):
        report = self.verified_report()
        ready = self.risk(report)
        self.assertTrue(ready.exposure.verified)
        self.assertEqual(len(ready.exposure.reservations), 1)
        self.assertEqual(ready.exposure.reservations[0].risk_pct, D("1"))
        self.assertNotIn("UNRECONCILED_PENDING_BROKER_ORDER",
                         ready.blocked_reasons)
        self.assertFalse(ready.entry_allowed)

    def test_short_sl_tp_do_not_become_entry_reservations(self):
        report = self.verified_report("SHORT")
        ready = self.risk(report)
        self.assertTrue(ready.exposure.verified)
        self.assertEqual(len(ready.exposure.reservations), 1)
        self.assertEqual(ready.exposure.reservations[0].direction, "SHORT")

    def test_replaced_stop_after_verified_break_even_remains_one_reservation(self):
        self.verified_report()
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
        self.transport.orders[0] = deepcopy(trade["stopLossOrder"])
        with self.service:
            report = self.service.run()
        self.assertFalse(report.blocked_reasons)
        self.assertEqual(report.verified_protective_order_ids, ("92", "101"))
        ready = self.risk(report)
        self.assertTrue(ready.exposure.verified)
        self.assertEqual(len(ready.exposure.reservations), 1)
        self.assertEqual(ready.exposure.reservations[0].risk_pct, D("1"))

    def test_unrelated_pending_entry_remains_blocking(self):
        report = self.verified_report()
        self.transport.orders.append({
            "id": "93", "instrument": "EUR_USD",
            "type": "MARKET_IF_TOUCHED", "state": "PENDING",
            "price": "1.0800",
        })
        with self.service:
            blocked = self.service.run()
        self.assertTrue(blocked.blocked_reasons)
        readiness = self.risk(report)
        self.assertFalse(readiness.exposure.verified)
        self.assertIn("UNRECONCILED_PENDING_BROKER_ORDER", readiness.blocked_reasons)

    def test_foreign_tp_same_order_id_never_laundered_by_old_report(self):
        report = self.verified_report()
        self.transport.orders[1]["tradeID"] = "999"
        readiness = self.risk(report)
        self.assertFalse(readiness.exposure.verified)
        self.assertIn("UNRECONCILED_PENDING_BROKER_ORDER", readiness.blocked_reasons)

    def test_same_ids_with_changed_price_or_trade_protection_are_rejected(self):
        report = self.verified_report()
        untouched = deepcopy(self.transport.orders)
        for field, change in (("price", "1.0500"), ("state", "CANCELLED"),
                              ("type", "LIMIT")):
            with self.subTest(field=field):
                self.transport.orders = deepcopy(untouched)
                self.transport.orders[0][field] = change
                readiness = self.risk(report)
                self.assertFalse(readiness.exposure.verified)
                self.assertIn("UNRECONCILED_PENDING_BROKER_ORDER",
                              readiness.blocked_reasons)

    def test_missing_nested_or_pending_order_identity_fails_closed(self):
        report = self.verified_report()
        self.transport.trades[0]["stopLossOrder"].pop("id")
        self.transport.trades[0].pop("stopLossOrderID")
        self.assertFalse(self.risk(report).exposure.verified)
        self.transport.trades[0]["stopLossOrder"]["id"] = "91"
        self.transport.trades[0]["stopLossOrderID"] = "91"
        self.transport.orders[0].pop("id")
        self.assertFalse(self.risk(report).exposure.verified)

    def test_forged_report_or_stale_protective_id_cannot_authorize_risk(self):
        report = self.verified_report()
        bogus = replace(report, verified_protective_order_ids=("91", "999"))
        self.assertFalse(self.risk(bogus).exposure.verified)
        self.transport.orders = []
        self.assertFalse(self.risk(report).exposure.verified)

    def test_pending_count_mismatch_is_an_error(self):
        report = self.verified_report()
        with self.assertRaises(RiskEvidenceError):
            self.risk(report, pending_count=1)

    def test_pending_order_duplicates_cannot_be_counted_as_protection(self):
        report = self.verified_report()
        self.transport.orders[1]["id"] = "91"
        self.assertFalse(self.risk(report).exposure.verified)

    def test_legacy_zero_pending_report_still_has_verified_exposure(self):
        report = self.bootstrap()
        self.assertEqual(report.verified_protective_order_ids, ())
        ready = self.risk(report)
        self.assertTrue(ready.exposure.verified)
        self.assertEqual(len(ready.exposure.reservations), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
