"""Phase A category 03: OANDA financial evidence, no broker writes."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from broker.financial_ledger import (
    FinancialEvidenceError, initial_ledger, parse_transaction,
    record_transaction, summary, trade_summary, validate_ledger,
)
from broker.account_reconciler import AccountReconciler
from broker.practice_transport import BrokerReply
from storage.lifecycle_state import LifecycleStore, ProcessLease, StateError, _digest
from storage.intent_journal import IntentJournal


def fill(n, *, opened=None, reduced=None, closed=None, pl="0",
         financing="0", commission="0", fee="0", spread="0"):
    tx = {
        "id": str(n), "type": "ORDER_FILL", "accountID": "acct-1",
        "pl": str(pl), "financing": str(financing),
        "commission": str(commission), "guaranteedExecutionFee": str(fee),
        "halfSpreadCost": str(spread),
    }
    if opened is not None:
        tx["tradeOpened"] = {"tradeID": opened, "units": "100"}
    if reduced is not None:
        tx["tradeReduced"] = reduced
    if closed is not None:
        tx["tradesClosed"] = closed
    return tx


def effect(trade, pl, financing="0", fee="0"):
    return {
        "tradeID": trade, "units": "100",
        "realizedPL": str(pl), "financing": str(financing),
        "guaranteedExecutionFee": str(fee),
    }


class FinancialLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = initial_ledger("100")

    def record(self, tx):
        record_transaction(self.ledger, tx)

    def test_single_trade_open_partial_close_net_with_fees_once(self):
        self.record(fill(101, opened="11", commission="2", fee="1", spread="5"))
        self.record(fill(
            102, reduced=effect("11", "7", "-1"),
            pl="7", financing="-1", spread="2",
        ))
        self.record(fill(
            103, closed=[effect("11", "-3")], pl="-3", commission="0.5",
            spread="1",
        ))
        period = summary(self.ledger)
        self.assertEqual(period["net_pl_home_since_cursor"], "-0.5")
        self.assertTrue(period["complete_since_cursor"])
        self.assertFalse(period["historical_account_pl_complete"])
        trade = trade_summary(self.ledger, "11")
        self.assertTrue(trade["complete"])
        self.assertEqual(trade["net_pl_home"], "-0.5")
        self.assertEqual(trade["related_transaction_ids"], ("101", "102", "103"))

    def test_half_spread_diagnostic_is_not_deducted_twice(self):
        self.record(fill(101, opened="11", pl="0", spread="9.9"))
        self.record(fill(
            102, closed=[effect("11", "10")], pl="10", spread="8.1"
        ))
        self.assertEqual(summary(self.ledger)["net_pl_home_since_cursor"], "10")
        self.assertEqual(trade_summary(self.ledger, "11")["net_pl_home"], "10")

    def test_multi_trade_fill_keeps_account_result_but_blocks_trade_allocation(self):
        self.record(fill(101, opened="11"))
        self.record(fill(102, opened="12"))
        self.record(fill(
            103, closed=[effect("11", "3"), effect("12", "7")],
            pl="10", commission="2",
        ))
        self.assertEqual(summary(self.ledger)["net_pl_home_since_cursor"], "8")
        self.assertFalse(trade_summary(self.ledger, "11")["complete"])
        self.assertIsNone(trade_summary(self.ledger, "11")["net_pl_home"])

    def test_daily_financing_and_dividend_keep_trade_provenance(self):
        self.record(fill(101, opened="11"))
        self.record({
            "id": "102", "type": "DAILY_FINANCING", "financing": "-0.3",
            "positionFinancings": [{
                "instrument": "EUR_USD", "financing": "-0.3",
                "openTradeFinancings": [
                    {"tradeID": "11", "financing": "-0.3"},
                ],
            }],
        })
        self.record({
            "id": "103", "type": "DIVIDEND_ADJUSTMENT",
            "dividendAdjustment": "1.2",
            "openTradeDividendAdjustments": [
                {"tradeID": "11", "dividendAdjustment": "1.2"},
            ],
        })
        self.record(fill(104, closed=[effect("11", "8")], pl="8"))
        self.assertEqual(summary(self.ledger)["net_pl_home_since_cursor"], "8.9")
        self.assertEqual(trade_summary(self.ledger, "11")["net_pl_home"], "8.9")

    def test_cash_transfers_are_excluded_from_trading_pl(self):
        self.record({
            "id": "101", "type": "TRANSFER_FUNDS", "amount": "100000"
        })
        self.record(fill(102, opened="11", commission="1"))
        result = summary(self.ledger)
        self.assertEqual(result["net_pl_home_since_cursor"], "-1")
        self.assertEqual(result["excluded_funds_transfer_ids"], ("101",))

    def test_absent_cost_fields_refuse_net_certification(self):
        self.record({
            "id": "101", "type": "ORDER_FILL", "accountID": "acct-1",
            "pl": "10", "tradesClosed": [effect("11", "10")],
        })
        result = summary(self.ledger)
        self.assertFalse(result["complete_since_cursor"])
        self.assertIsNone(result["net_pl_home_since_cursor"])
        self.assertEqual(result["unverified_transaction_ids"], ("101",))

    def test_incomplete_historical_entry_does_not_create_lifetime_trade_net(self):
        self.record(fill(101, closed=[effect("11", "10")], pl="10"))
        self.assertIsNone(trade_summary(self.ledger, "11")["net_pl_home"])
        self.assertFalse(trade_summary(self.ledger, "11")["complete"])

    def test_unknown_transaction_type_blocks_unverified_financial_total(self):
        self.record({"id": "101", "type": "UNRECOGNIZED_FUTURE_TYPE"})
        self.assertIsNone(summary(self.ledger)["net_pl_home_since_cursor"])
        self.assertEqual(summary(self.ledger)["unverified_transaction_ids"], ("101",))

    def test_unknown_future_type_blocks_trade_net_even_after_close(self):
        self.record(fill(101, opened="11"))
        self.record({"id": "102", "type": "NEW_FINANCIAL_ADJUSTMENT"})
        self.record(fill(103, closed=[effect("11", "10")], pl="10"))
        self.assertIsNone(trade_summary(self.ledger, "11")["net_pl_home"])
        self.assertIn(
            "102", trade_summary(self.ledger, "11")["unverified_transaction_ids"]
        )

    def test_missing_financing_allocation_blocks_trade_only(self):
        self.record(fill(101, opened="11"))
        self.record({
            "id": "102", "type": "DAILY_FINANCING", "financing": "-1"
        })
        self.record(fill(103, closed=[effect("11", "10")], pl="10"))
        self.assertEqual(summary(self.ledger)["net_pl_home_since_cursor"], "9")
        self.assertFalse(trade_summary(self.ledger, "11")["complete"])

    def test_aggregate_trade_pl_mismatch_fails_closed_without_new_event(self):
        with self.assertRaises(FinancialEvidenceError):
            self.record(fill(
                101, closed=[effect("11", "10")], pl="11"
            ))
        self.assertEqual(self.ledger["events"], {})

    def test_financing_or_dividend_mismatch_blocks(self):
        bad_financing = {
            "id": "101", "type": "DAILY_FINANCING", "financing": "-2",
            "positionFinancings": [{
                "instrument": "EUR_USD", "financing": "-1",
                "openTradeFinancings": [{"tradeID": "11", "financing": "-1"}],
            }],
        }
        with self.assertRaises(FinancialEvidenceError):
            self.record(bad_financing)
        bad_dividend = {
            "id": "102", "type": "DIVIDEND_ADJUSTMENT",
            "dividendAdjustment": "10",
            "openTradeDividendAdjustments": [
                {"tradeID": "11", "dividendAdjustment": "2"}
            ],
        }
        with self.assertRaises(FinancialEvidenceError):
            self.record(bad_dividend)

    def test_invalid_numeric_costs_fail_closed(self):
        for value in ("NaN", "Infinity", "-1", True):
            with self.subTest(value=value):
                tx = fill(101)
                tx["commission"] = value
                with self.assertRaises(FinancialEvidenceError):
                    self.record(tx)
        self.assertEqual(self.ledger["events"], {})

    def test_duplicate_transaction_or_backdated_baseline_refused(self):
        self.record(fill(101))
        with self.assertRaises(FinancialEvidenceError):
            self.record(fill(101))
        with self.assertRaises(FinancialEvidenceError):
            self.record(fill(100))
        self.assertEqual(len(self.ledger["events"]), 1)

    def test_nonfinancial_broker_transaction_cannot_hide_cash_amount(self):
        with self.assertRaises(FinancialEvidenceError):
            self.record({"id": "101", "type": "ORDER_CANCEL", "commission": "1"})

    def test_validation_detects_tampered_net_and_unprocessed_event(self):
        self.record(fill(101, pl="10", closed=[effect("11", "10")]))
        validate_ledger(self.ledger, cursor="101", processed=["101"])
        event = self.ledger["events"]["101"]
        event["account_net"] = "999"
        with self.assertRaises(FinancialEvidenceError):
            validate_ledger(self.ledger, cursor="101", processed=["101"])
        event["account_net"] = "10"
        with self.assertRaises(FinancialEvidenceError):
            validate_ledger(self.ledger, cursor="100", processed=[])


class FakeAccount:
    def __init__(self):
        self.cursor = "100"
        self.transactions = []
        self.trades = []
        self.orders = []

    def read(self, path, *, params=None):
        if path == "":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "account": {
                    "id": "acct-1", "openTradeCount": len(self.trades),
                    "pendingOrderCount": len(self.orders),
                    "trades": self.trades, "orders": self.orders,
                },
            })
        if path == "/transactions/sinceid":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor, "transactions": self.transactions
            })
        raise AssertionError("Unexpected broker GET: " + path)


class FinancialReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker = FakeAccount()
        self.store = LifecycleStore("acct-1", directory=self.temp.name)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)
        self.reconciler = AccountReconciler(
            "acct-1", transport=self.broker, journal=self.journal,
            store=self.store, lease=ProcessLease("acct-1", directory=self.temp.name)
        )
        with self.reconciler:
            self.reconciler.run()
        self.assertEqual(self.store.load()["financial_ledger"]["baseline_cursor"], "100")

    def tick(self, tx):
        self.broker.cursor = tx["id"]
        self.broker.transactions = [tx]

    def test_full_broker_transaction_persists_with_cursor_exactly_once(self):
        self.tick({
            "id": "101", "accountID": "acct-1", "type": "DAILY_FINANCING",
            "financing": "-0.5",
            # Account amount is verifiable, but trade attribution is unknown.
        })
        with self.reconciler:
            first = self.reconciler.run()
            second = self.reconciler.run()
        self.assertFalse(first.blocked_reasons)
        self.assertEqual(second.new_closed_trade_ids, ())
        saved = self.store.load()
        self.assertEqual(saved["cursor"], "101")
        self.assertEqual(saved["processed_transactions"], ["101"])
        self.assertEqual(set(saved["financial_ledger"]["events"]), {"101"})
        self.assertEqual(summary(saved["financial_ledger"])["net_pl_home_since_cursor"], "-0.5")

    def test_broker_snapshot_blocker_preserves_financial_ledger(self):
        self.tick({"id": "101", "accountID": "acct-1",
                   "type": "DAILY_FINANCING", "financing": "5"})
        self.broker.orders = [{"id": "99"}]
        with self.reconciler:
            report = self.reconciler.run()
        self.assertTrue(report.blocked_reasons)
        self.assertEqual(self.store.load()["cursor"], "100")
        self.assertFalse(self.store.load()["financial_ledger"]["events"])

    def test_atomic_failure_preserves_cursor_and_financial_entry_together(self):
        self.tick({"id": "101", "accountID": "acct-1",
                   "type": "DAILY_FINANCING", "financing": "1"})
        original = self.store.path.read_bytes()
        with patch("storage.lifecycle_state.os.replace",
                   side_effect=OSError("simulated crash")):
            with self.reconciler, self.assertRaises(StateError):
                self.reconciler.run()
        self.assertEqual(self.store.path.read_bytes(), original)
        with self.reconciler:
            self.reconciler.run()
        self.assertEqual(self.store.load()["cursor"], "101")
        self.assertEqual(len(self.store.load()["financial_ledger"]["events"]), 1)

    def test_legacy_state_migrates_without_claiming_old_financial_history(self):
        state = json.loads(self.store.path.read_text())
        state["data"].pop("financial_ledger")
        state["sha256"] = _digest(state["data"])
        self.store.path.write_text(json.dumps(state))
        self.assertNotIn("financial_ledger", self.store.load())
        with self.reconciler:
            self.reconciler.run()
        migrated = self.store.load()["financial_ledger"]
        self.assertEqual(migrated["baseline_cursor"], "100")
        self.assertFalse(summary(migrated)["historical_account_pl_complete"])

    def test_ledger_checksum_mismatch_is_not_silently_recovered(self):
        self.tick({"id": "101", "accountID": "acct-1",
                   "type": "DAILY_FINANCING", "financing": "-1"})
        with self.reconciler:
            self.reconciler.run()
        envelope = json.loads(self.store.path.read_text())
        envelope["data"]["financial_ledger"]["events"]["101"]["account_net"] = "1000"
        envelope["sha256"] = _digest(envelope["data"])
        self.store.path.write_text(json.dumps(envelope))
        with self.assertRaises(StateError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
