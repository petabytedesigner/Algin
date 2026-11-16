"""Phase A / Category 04: broker-reconciled exposure and net loss evidence.

Offline fake broker only.  No risk policy settings or OANDA writes.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import unittest

from test_category4 import LifecycleFixture
from broker.account_reconciler import ReconcileReport
from broker.financial_ledger import record_transaction, parse_transaction
from risk.reconciled_account import (
    RiskEvidenceError, RiskHistoryAnchor, build_risk_readiness,
    _transaction_downside,
)
from risk.trade_lifecycle import TradeRecord
from risk.trade_plan import AccountFunds
from storage.intent_journal import JournalError
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
D = Decimal


def account(*, cursor="100", opened=0, pending=0, nav="10000"):
    return AccountFunds(
        "acct-1", "USD", D(nav), D(nav), D("8000"), D("0"),
        opened, pending, cursor,
    )


def evidence(*, cursor="102", day="2026-11-16",
             day_cursor="100", peak="10000", complete=True, streak=0):
    return RiskHistoryAnchor(
        account_id="acct-1", cursor=cursor, day=day,
        day_start_cursor=day_cursor,
        day_start_nav=D("10000"), peak_nav=D(peak),
        starting_loss_streak=streak, history_complete=complete,
    )


class ExposureAndRetirementTests(LifecycleFixture):
    def read_risk(self, report, *, nav="10000", anchor=None):
        return build_risk_readiness(
            account(cursor=report.cursor, opened=len(self.transport.trades),
                    pending=len(self.transport.orders), nav=nav),
            self.store.load(), self.journal.snapshot(),
            broker_trades=tuple(self.transport.trades),
            broker_orders=tuple(self.transport.orders),
            report=report, now=NOW, anchor=anchor
        )

    def test_verified_open_fill_is_reserved_at_original_risk(self):
        report = self.bootstrap()
        snapshot = self.read_risk(report)
        self.assertTrue(snapshot.exposure.verified)
        self.assertEqual(len(snapshot.exposure.reservations), 1)
        reservation = snapshot.exposure.reservations[0]
        self.assertEqual(reservation.status, "OPEN")
        self.assertEqual(reservation.risk_pct, D("1"))
        self.assertEqual(reservation.grade, "A")
        self.assertFalse(snapshot.losses.verified)
        self.assertFalse(snapshot.entry_allowed)

    def test_unknown_order_reserves_risk_but_never_enables_new_entry(self):
        with self.service:
            self.service.run()
        self.journal.begin(
            signal_id="s-unknown", attempt_id="algin-unknown",
            request={"order": {"instrument": "EUR_USD", "units": "10"}},
            risk_pct="0.5", margin_home="15", grade="B"
        )
        self.journal.transition("algin-unknown", "IN_FLIGHT")
        self.journal.transition("algin-unknown", "UNKNOWN")
        with self.service:
            report = self.service.run()
        snapshot = self.read_risk(report)
        self.assertFalse(snapshot.exposure.verified)
        self.assertEqual(len(snapshot.exposure.reservations), 1)
        self.assertEqual(snapshot.exposure.reservations[0].status,
                         "OUTCOME_UNKNOWN")
        self.assertIn("UNRESOLVED_ORDER_INTENT", snapshot.blocked_reasons)
        self.assertFalse(snapshot.entry_allowed)

    def test_closed_trade_release_is_transaction_backed_and_idempotent(self):
        self.bootstrap()
        with self.assertRaises(JournalError):
            self.journal.begin(
                signal_id="premature", attempt_id="premature",
                request={"order": {"instrument": "GBP_USD", "units": "10"}},
                risk_pct="0.5", margin_home="10", grade="B"
            )
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-16T10:00:00Z",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "-20"}
            ],
        }, close=True)
        with self.service:
            first = self.service.run()
            second = self.service.run()
        self.assertEqual(first.new_closed_trade_ids, ("11",))
        self.assertEqual(second.new_closed_trade_ids, ())
        saved = self.journal.get("algin-11")
        self.assertTrue(saved["retired"])
        self.assertEqual(saved["closure_transaction_id"], "101")
        snapshot = self.read_risk(second)
        self.assertTrue(snapshot.exposure.verified)
        self.assertEqual(snapshot.exposure.reservations, ())
        self.assertFalse(snapshot.losses.verified)
        self.assertFalse(snapshot.entry_allowed)
        created = self.journal.begin(
            signal_id="later", attempt_id="later",
            request={"order": {"instrument": "GBP_USD", "units": "10"}},
            risk_pct="0.5", margin_home="10", grade="B"
        )
        self.assertEqual(created["status"], "PREPARED")
        with self.assertRaises(JournalError):
            self.journal.begin(
                signal_id="signal-11", attempt_id="reuse",
                request={"order": {"instrument": "GBP_USD", "units": "10"}},
                risk_pct="0.5", margin_home="10", grade="B"
            )

    def test_failed_retirement_keeps_filled_intent_until_safe_replay(self):
        self.bootstrap()
        self.update_broker({
            "id": "101", "accountID": "acct-1", "type": "ORDER_FILL",
            "time": "2026-11-16T10:00:00Z",
            "tradesClosed": [{"tradeID": "11", "units": "-100", "realizedPL": "5"}],
        }, close=True)
        # Patch only the journal's durable persistence boundary AFTER
        # lifecycle commit (patching os.replace affects both modules).
        with patch.object(self.journal, "_store",
                          side_effect=JournalError("journal disk interrupted")):
            with self.service, self.assertRaises(JournalError):
                self.service.run()
        self.assertEqual(self.store.load()["cursor"], "101")
        self.assertFalse(self.journal.get("algin-11").get("retired", False))
        with self.service:
            report = self.service.run()
        self.assertEqual(report.new_closed_trade_ids, ())
        self.assertTrue(self.journal.get("algin-11")["retired"])
        self.assertEqual(len(self.store.load()["counted_closures"]), 1)

    def test_wrong_account_or_unclosed_trade_cannot_retire(self):
        self.bootstrap()
        with self.assertRaises(JournalError):
            self.journal.retire_confirmed("algin-11", self.store)
        from storage.lifecycle_state import LifecycleStore
        foreign = LifecycleStore("other", directory=self.folder.name)
        with self.assertRaises(JournalError):
            self.journal.retire_confirmed("algin-11", foreign)
        self.assertFalse(self.journal.get("algin-11").get("retired", False))

    def test_ambiguous_pending_order_never_becomes_verified(self):
        with self.service:
            self.service.run()
        self.transport.orders = [{"id": "90", "instrument": "EUR_USD"}]
        with self.service:
            report = self.service.run()
        snapshot = self.read_risk(report)
        self.assertIn("UNRECONCILED_PENDING_BROKER_ORDER",
                      snapshot.blocked_reasons)
        self.assertFalse(snapshot.exposure.verified)

    def test_readiness_refuses_wrong_cursor_or_foreign_trade(self):
        report = self.bootstrap()
        with self.assertRaises(RiskEvidenceError):
            build_risk_readiness(
                account(cursor="999", opened=1), self.store.load(),
                self.journal.snapshot(), broker_trades=self.transport.trades,
                broker_orders=(), report=report, now=NOW
            )
        foreign = [dict(self.transport.trades[0])]
        foreign[0]["clientExtensions"] = {"id": "foreign", "tag": "other"}
        with self.assertRaises(RiskEvidenceError):
            build_risk_readiness(
                account(opened=1), self.store.load(), self.journal.snapshot(),
                broker_trades=foreign, broker_orders=(), report=report, now=NOW
            )


class MultiTradeRiskAccountingTests(unittest.TestCase):
    def test_profitable_aggregate_does_not_hide_losing_trade_leg(self):
        event = parse_transaction({
            "id": "102", "type": "ORDER_FILL", "pl": "5",
            "financing": "0", "commission": "0",
            "guaranteedExecutionFee": "0",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "-10",
                 "financing": "0", "guaranteedExecutionFee": "0"},
                {"tradeID": "12", "units": "-100", "realizedPL": "15",
                 "financing": "0", "guaranteedExecutionFee": "0"},
            ],
        })
        self.assertEqual(event["account_net"], "5")
        self.assertTrue(event["allocation_complete"])
        self.assertEqual(_transaction_downside(event), D("10"))

    def test_unallocated_order_fill_cannot_certify_daily_loss(self):
        event = parse_transaction({
            "id": "102", "type": "ORDER_FILL", "pl": "5",
            "financing": "0", "commission": "2",
            "guaranteedExecutionFee": "0",
            "tradesClosed": [
                {"tradeID": "11", "units": "-100", "realizedPL": "-10",
                 "financing": "0", "guaranteedExecutionFee": "0"},
                {"tradeID": "12", "units": "-100", "realizedPL": "15",
                 "financing": "0", "guaranteedExecutionFee": "0"},
            ],
        })
        self.assertFalse(event["allocation_complete"])
        with self.assertRaises(RiskEvidenceError):
            _transaction_downside(event)


class VerifiedLossFixture(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.store.initialize("100")
        record = TradeRecord(
            trade_id="11", attempt_id="algin-11",
            instrument="EUR_USD", direction="LONG", grade="A",
            entry_price=D("1.1000"), initial_stop=D("1.0980"),
            take_profit=D("1.1060"), initial_units=D("100"),
            current_units=D("0"), current_stop=D("1.0980"),
            risk_pct=D("1"), opened_at=datetime(
                2026, 11, 16, 9, tzinfo=timezone.utc
            ),
            closed=True, result_counted=True, close_transaction_ids=("102",),
            net_pl_home=D("-10"), closed_at=datetime(
                2026, 11, 16, 10, tzinfo=timezone.utc
            ),
        )
        self.journal.begin(
            signal_id="s-11", attempt_id="algin-11",
            request={"order": {
                "instrument": "EUR_USD", "units": "100"
            }}, risk_pct="1", margin_home="50", grade="A"
        )
        self.journal.transition("algin-11", "IN_FLIGHT")
        self.journal.transition(
            "algin-11", "FILLED", trade_id="11", order_id="100",
            filled_units="100", fill_price="1.1000",
            protection_verified=True
        )

        def update(state):
            state["cursor"] = "102"
            state["processed_transactions"] = ["101", "102"]
            state["counted_closures"] = ["102"]
            state["trades"]["11"] = record.to_dict()
            state["closed_results"]["11"] = {
                "transaction_id": "102", "pl": "-10",
                "closed_at": iso_utc(record.closed_at),
                "instrument": "EUR_USD",
            }
            record_transaction(state["financial_ledger"], {
                "id": "101", "type": "ORDER_FILL", "pl": "0",
                "financing": "0", "commission": "2",
                "guaranteedExecutionFee": "0",
                "tradeOpened": {"tradeID": "11", "units": "100",
                                "guaranteedExecutionFee": "0"}
            })
            record_transaction(state["financial_ledger"], {
                "id": "102", "type": "ORDER_FILL", "pl": "-10",
                "financing": "-1", "commission": "0",
                "guaranteedExecutionFee": "0",
                "tradesClosed": [{
                    "tradeID": "11", "units": "-100", "realizedPL": "-10",
                    "financing": "-1", "guaranteedExecutionFee": "0"
                }]
            })
        self.store.mutate(update, expected_cursor="100")
        self.journal.retire_confirmed("algin-11", self.store)

    def view(self, *, anchor=None, nav="9950"):
        report = ReconcileReport(
            cursor="102", owned_open_trades=(),
            blocked_reasons=(), new_closed_trade_ids=()
        )
        return build_risk_readiness(
            account(cursor="102", nav=nav),
            self.store.load(), self.journal.snapshot(),
            broker_trades=(), broker_orders=(), report=report, now=NOW,
            anchor=anchor
        )

    def test_complete_net_evidence_yields_verified_losses_but_not_entry_permission(self):
        result = self.view(anchor=evidence())
        self.assertTrue(result.exposure.verified)
        self.assertTrue(result.losses.verified)
        self.assertTrue(result.inputs_verified)
        self.assertFalse(result.entry_allowed)
        self.assertEqual(result.losses.consecutive_losses, 1)
        self.assertEqual(result.losses.daily_realized_loss_home, D("13"))
        self.assertEqual(result.losses.peak_nav, D("10000"))
        self.assertIn("EUR_USD", result.losses.cooldown_until_by_instrument)

    def test_no_anchor_does_not_guess_day_nav_or_peak(self):
        result = self.view()
        self.assertFalse(result.losses.verified)
        self.assertFalse(result.inputs_verified)
        self.assertIn("RISK_HISTORY_ANCHOR_MISSING", result.blocked_reasons)

    def test_wrong_day_cursor_or_drawdown_anchor_rejects_verified_loss(self):
        for anchor in (
            evidence(day="2026-11-15"),
            evidence(day_cursor="99"),
            evidence(peak="9000"),
            evidence(complete=False),
        ):
            with self.subTest(anchor=anchor):
                result = self.view(anchor=anchor)
                self.assertFalse(result.losses.verified)
                self.assertFalse(result.inputs_verified)

    def test_missing_financial_fee_refuses_verified_losses(self):
        def corrupt(state):
            event = state["financial_ledger"]["events"]["101"]
            event["components"]["commission"] = None
            event["account_net"] = None
        self.store.mutate(corrupt, expected_cursor="102")
        result = self.view(anchor=evidence())
        self.assertFalse(result.losses.verified)
        self.assertIn("FINANCIAL_HISTORY_INCOMPLETE", result.blocked_reasons)

    def test_net_loss_and_legacy_pl_are_not_confused(self):
        state = self.store.load()
        self.assertEqual(state["closed_results"]["11"]["pl"], "-10")
        result = self.view(anchor=evidence())
        # -10 close realized, -1 financing, -2 open commission.
        self.assertEqual(result.losses.daily_realized_loss_home, D("13"))
        self.assertTrue(result.losses.verified)


if __name__ == "__main__":
    unittest.main()
