"""Category 3: deterministic OANDA fixture regressions; NO real network writes."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import unittest

import config
from broker.execution_result import Outcome, classify_response
from broker.execution_validation import (
    PreflightRejected, build_market_order, parse_market_quote,
)
from broker.order_engine import OrderEngine
from broker.practice_transport import (
    BrokerCommunicationError, BrokerReply, BrokerWriteDisabled,
    PracticeTransport,
)
from execution.safety_check import SafetyCheck
from risk.trade_plan import (
    AccountFunds, ExposureBook, InstrumentRules, LossState,
    QuoteConversions, TradePlanner,
)
from storage.intent_journal import IntentJournal, JournalError


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


def D(s):
    return Decimal(str(s))


def inputs(*, direction="LONG"):
    rules = InstrumentRules(
        "EUR_USD", "CURRENCY", -4, 5, 0, D("1"),
        D("1000000"), D("0"), D("0.02")
    )
    conv = QuoteConversions(
        "EUR_USD", D("1.1000"), D("1.1002"), NOW,
        D("1"), D("1.1")
    )
    acct = AccountFunds(
        "acct-1", "USD", D("10000"), D("10000"), D("8000"),
        D("0"), 0, 0, "1000"
    )
    book = ExposureBook("acct-1", (), verified=True,
                        uncertain_outcomes_accounted=True)
    losses = LossState(
        D("10000"), 0, True, day_start_nav=D("10000"),
        daily_realized_loss_home=D("0")
    )
    sig = SimpleNamespace(
        instrument="EUR_USD", direction=direction, grade="A",
        score=85, timestamp=NOW
    )
    plan = TradePlanner().plan(
        sig, D("0.0010"), acct, rules, conv, book, losses, now=NOW
    )
    return plan, rules, conv


def price_fixture(*, status="tradeable", bid="1.1000", ask="1.1002", time=None):
    return {"prices": [{
        "instrument": "EUR_USD",
        "status": status, "bids": [{"price": bid}],
        "asks": [{"price": ask}],
        "time": time or "2026-11-15T10:00:00Z",
    }]}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.posts = 0
        self.payload = None
        self.submit_reply = None
        self.submit_failure = None
        self.broker_trade = None
        self.order_for_lookup = None
        self.transaction_for_lookup = None
        self.account = {
            "lastTransactionID": "1000",
            "account": {
                "id": "acct-1", "openTradeCount": 0,
                "pendingOrderCount": 0, "trades": [], "orders": []
            },
        }
        self.quote = price_fixture()

    def read(self, path, *, params=None):
        self.calls.append(("GET", path))
        if path == "":
            return BrokerReply(200, self.account, "read-account")
        if path == "/pricing":
            return BrokerReply(200, self.quote, "read-price")
        if path.startswith("/orders/@"):
            return self.order_for_lookup or BrokerReply(404, {"errorMessage": "missing"})
        if path.startswith("/transactions/"):
            return self.transaction_for_lookup or BrokerReply(404, None)
        if path.startswith("/trades/"):
            if self.broker_trade is not None:
                return BrokerReply(200, {"trade": self.broker_trade}, "read-trade")
            if self.payload is None:
                return BrokerReply(404, None)
            order = self.payload["order"]
            return BrokerReply(200, {"trade": {
                "id": "2201", "instrument": order["instrument"],
                "state": "OPEN", "currentUnits": order["units"],
                "clientExtensions": order["tradeClientExtensions"],
                "stopLossOrder": order["stopLossOnFill"],
                "takeProfitOrder": order["takeProfitOnFill"],
            }}, "read-trade")
        raise AssertionError("Unexpected broker GET " + path)

    def submit_market_order(self, payload):
        self.posts += 1
        self.calls.append(("POST", "/orders"))
        self.payload = payload
        if self.submit_failure:
            raise self.submit_failure
        if self.submit_reply is not None:
            return self.submit_reply
        return self.fill()

    def fill(self, *, reductions=None, closed=None, trade_opened=True):
        order = self.payload["order"]
        tx = {
            "id": "2201", "type": "ORDER_FILL",
            "orderID": "2200", "price": "1.1002",
            "units": order["units"],
        }
        if trade_opened:
            tx["tradeOpened"] = {"tradeID": "2201", "units": order["units"]}
        if reductions is not None:
            tx["tradeReduced"] = reductions
        if closed is not None:
            tx["tradesClosed"] = closed
        return BrokerReply(
            201, {
                "orderCreateTransaction": {"id": "2200"},
                "orderFillTransaction": tx
            }, "request-200"
        )

    def provision_reconciliation(self, state="FILLED", *, bad_tx=False):
        self.order_for_lookup = BrokerReply(
            200, {"order": {
                "id": "2200", "instrument": "EUR_USD", "state": state,
                "clientExtensions": self.payload["order"]["clientExtensions"],
                "fillingTransactionID": "2201",
                "cancellingTransactionID": "2201",
            }}
        )
        if state == "FILLED":
            transaction = self.fill().body["orderFillTransaction"]
        else:
            transaction = {
                "id": "2201", "type": "ORDER_CANCEL", "orderID": "2200"
            }
        if bad_tx:
            transaction = dict(transaction, orderID="wrong")
        self.transaction_for_lookup = BrokerReply(
            200, {"transaction": transaction}, "request-get"
        )


class QuoteAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.rules, self.conversion = inputs()

    def market(self, **kw):
        return parse_market_quote(
            price_fixture(**kw), "EUR_USD", self.rules,
            now=NOW, max_age_seconds=20, max_spread_pips=D("2.5")
        )

    def prepared(self, *, plan=None, market=None, rules=None, conversion=None):
        return build_market_order(
            plan or self.plan, rules or self.rules, conversion or self.conversion,
            market or self.market(), attempt_id="algin-abc", signal_id="signal-1",
            slippage_pips=D("2"), minimum_stop_pips=D("3")
        )

    def test_P24_documented_quote_status_and_spread(self):
        quote = self.market()
        self.assertEqual(quote.spread_pips, D("2"))
        self.assertEqual(quote.ask, D("1.1002"))
        self.assertEqual(quote.bid, D("1.1000"))

    def test_P24_logTX_spread_guard_regression(self):
        # Historical LogTX verified 2.9/3.9 pips blocked against a 2.5 cap.
        for ask in ("1.10029", "1.10039"):
            with self.subTest(ask=ask):
                with self.assertRaises(PreflightRejected):
                    self.market(ask=ask)

    def test_P24_stale_future_and_untradeable_quote(self):
        cases = (
            {"status": "non-tradeable"},
            {"time": "2026-11-15T09:59:20Z"},
            {"time": "2026-11-15T10:00:05Z"},
            {"bid": "1.1002", "ask": "1.1001"},
            {"bid": "NaN"},
            {"ask": "Infinity"},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                with self.assertRaises(PreflightRejected):
                    self.market(**entry)

    def test_P24_missing_or_duplicate_quote(self):
        with self.assertRaises(PreflightRejected):
            parse_market_quote(
                {"prices": []}, "EUR_USD", self.rules, now=NOW,
                max_age_seconds=20, max_spread_pips=D("2.5")
            )
        with self.assertRaises(PreflightRejected):
            parse_market_quote(
                {"prices": price_fixture()["prices"] * 2},
                "EUR_USD", self.rules, now=NOW,
                max_age_seconds=20, max_spread_pips=D("2.5")
            )

    def test_P26_buy_priceBound_is_worst_ask_plus_tolerance(self):
        result = self.prepared()
        self.assertEqual(result.bound, D("1.10040"))
        self.assertEqual(result.payload["order"]["priceBound"], "1.10040")
        self.assertEqual(result.payload["order"]["positionFill"], "OPEN_ONLY")
        self.assertEqual(result.payload["order"]["timeInForce"], "FOK")
        self.assertEqual(result.payload["order"]["tradeClientExtensions"]["tag"], "algin")

    def test_P26_short_priceBound_uses_bid_minus_tolerance(self):
        plan, rules, conv = inputs(direction="SHORT")
        result = self.prepared(plan=plan, rules=rules, conversion=conv)
        self.assertEqual(result.bound, D("1.09980"))
        self.assertTrue(result.units < 0)
        self.assertEqual(result.payload["order"]["units"], format(result.units, "f"))

    def test_P27_slippage_aware_units_never_exceed_risk(self):
        result = self.prepared()
        self.assertLess(abs(result.units), abs(self.plan.units))
        self.assertLessEqual(result.worst_loss_home, self.plan.risk_budget_home)
        self.assertEqual(result.units, result.units.quantize(self.rules.unit_step))

    def test_P27_invalid_direction_precision_distance_and_instrument(self):
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, units=-abs(self.plan.units)))
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, stop_loss=D("1.098701")))
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, stop_loss=D("1.09995")))
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, instrument="USD_CAD"))
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, quote_timestamp=NOW-timedelta(seconds=1)))

    def test_P27_broker_minimum_and_fractional_precision(self):
        with self.assertRaises(PreflightRejected):
            self.prepared(rules=replace(self.rules, minimum_trade_size=D("1000000")))
        # Reject an unquantized original request, never flip/round it to trade.
        with self.assertRaises(PreflightRejected):
            self.prepared(plan=replace(self.plan, units=D("100.5")))


class ClassifierTests(unittest.TestCase):
    def test_P20_fill_uses_trade_opened_id_not_order_id(self):
        fake = FakeTransport()
        fake.payload = {"order": {"units": "100"}}
        result = classify_response(fake.fill(), "attempt")
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertEqual(result.trade_id, "2201")
        self.assertEqual(result.order_id, "2200")
        self.assertEqual(result.fill_price, D("1.1002"))
        self.assertEqual(result.filled_units, D("100"))

    def test_P20_order_create_id_is_never_a_trade_id(self):
        raw = {"orderCreateTransaction": {"id": "99"}}
        result = classify_response(BrokerReply(201, raw), "attempt")
        self.assertEqual(result.outcome, Outcome.UNKNOWN)
        self.assertIsNone(result.trade_id)

    def test_P20_cancel_and_reject_transaction_only(self):
        cancelled = classify_response(
            BrokerReply(201, {"orderCancelTransaction": {
                "id": "21", "orderID": "20", "type": "ORDER_CANCEL"
            }}), "attempt"
        )
        rejected = classify_response(
            BrokerReply(400, {"orderRejectTransaction": {
                "id": "42", "type": "MARKET_ORDER_REJECT"
            }}), "attempt"
        )
        self.assertEqual(cancelled.outcome, Outcome.CANCELLED)
        self.assertEqual(rejected.outcome, Outcome.REJECTED)

    def test_P23_4xx_5xx_429_json_missing_fields_are_unknown(self):
        for status, body in (
            (201, None), (500, {}), (503, {"error": "gateway"}),
            (429, {"errorMessage": "rate limit"}),
            (401, {"errorMessage": "not authorized"}),
            (400, {"errorMessage": "unknown error"}),
            (201, {"orderFillTransaction": {
                "id": "2201", "type": "ORDER_FILL", "units": "100"}}),
            (201, {"orderFillTransaction": {
                "id": "2201", "type": "ORDER_FILL", "price": "1.1",
                "units": "100", "tradeOpened": {"tradeID": None}}}),
            (201, {"orderFillTransaction": {"id": "2201", "type": "ORDER_FILL"},
                   "orderCancelTransaction": {"id": "2202", "type": "ORDER_CANCEL"}})
        ):
            with self.subTest(status=status, body=body):
                outcome = classify_response(BrokerReply(status, body), "attempt")
                self.assertEqual(outcome.outcome, Outcome.UNKNOWN)

    def test_P28_real_fill_records_reductions_and_closures(self):
        fake = FakeTransport()
        fake.payload = {"order": {"units": "-100"}}
        reduction = {"tradeID": "10", "units": "-30", "realizedPL": "-1.1"}
        closed = [{"tradeID": "11", "units": "-70", "realizedPL": "2.1"}]
        result = classify_response(
            fake.fill(reductions=reduction, closed=closed, trade_opened=False),
            "attempt"
        )
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertIsNone(result.trade_id)
        self.assertEqual(result.trade_reduced, reduction)
        self.assertEqual(result.trades_closed, tuple(closed))


class IntentJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)
        self.order = {"order": {"instrument": "EUR_USD", "units": "100"}}

    def begin(self, signal="s1", attempt="algin-1"):
        return self.journal.begin(
            signal_id=signal, attempt_id=attempt, request=self.order,
            risk_pct="0.5", margin_home="50"
        )

    def test_P21_durable_before_transmission_and_duplicate_signal(self):
        attempt = self.begin()
        self.assertEqual(attempt["status"], "PREPARED")
        reopened = IntentJournal("acct-1", directory=self.temp.name)
        self.assertEqual(reopened.get("algin-1")["request_hash"], attempt["request_hash"])
        self.assertTrue(Path(reopened.path).exists())
        with self.assertRaises(JournalError):
            reopened.begin(
                signal_id="s1", attempt_id="algin-2", request=self.order,
                risk_pct="0.5", margin_home="50"
            )
        with self.assertRaises(JournalError):
            reopened.begin(
                signal_id="s2", attempt_id="algin-2", request=self.order,
                risk_pct="0.5", margin_home="50"
            )

    def test_P21_durable_unknown_blocks_new_attempt(self):
        self.begin()
        self.journal.transition("algin-1", "IN_FLIGHT")
        self.journal.transition("algin-1", "UNKNOWN")
        with self.assertRaises(JournalError):
            self.begin(signal="s2", attempt="algin-2")
        self.assertEqual(self.journal.get("algin-1")["status"], "UNKNOWN")

    def test_P21_stale_lock_fail_closed(self):
        Path(self.journal.lock_path).write_text("stale")
        with self.assertRaises(JournalError):
            self.begin()
        self.assertTrue(Path(self.journal.lock_path).exists())

    def test_P21_corrupt_or_wrong_account_state_fails(self):
        self.journal.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(JournalError):
            self.begin()
        self.journal.path.write_text(
            json.dumps({"version": 1, "account_id": "other", "attempts": {}}),
            encoding="utf-8"
        )
        with self.assertRaises(JournalError):
            self.begin()

    def test_P21_illegal_transition_is_not_permitted(self):
        self.begin()
        with self.assertRaises(JournalError):
            self.journal.transition("algin-1", "FILLED")
        self.journal.transition("algin-1", "IN_FLIGHT")
        self.journal.transition("algin-1", "REJECTED")
        with self.assertRaises(JournalError):
            self.journal.transition("algin-1", "IN_FLIGHT")
        with self.assertRaises(JournalError):
            self.journal.transition("algin-1", "REJECTED", token="secret")


class OrderEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)
        self.transport = FakeTransport()
        self.plan, self.rules, self.conv = inputs()
        self.engine = OrderEngine(
            "acct-1", transport=self.transport, journal=self.journal
        )

    def submit(self, *, signal="s1"):
        return self.engine.submit(
            self.plan, self.rules, self.conv, signal_id=signal, now=NOW
        )

    def test_P19_one_POST_and_confirmed_protection(self):
        result = self.submit()
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertTrue(result.protection_verified)
        self.assertEqual(self.transport.posts, 1)
        self.assertEqual(self.journal.get(result.attempt_id)["status"], "FILLED")
        self.assertTrue(self.journal.get(result.attempt_id)["protection_verified"])
        self.assertEqual(self.journal.get(result.attempt_id)["fill_price"], "1.1002")
        self.assertIn(("GET", "/trades/2201"), self.transport.calls)
        with self.assertRaises(PreflightRejected):
            self.submit(signal="s2")
        self.assertEqual(self.transport.posts, 1)

    def test_P21_duplicate_signal_never_posts_twice(self):
        self.transport.submit_reply = BrokerReply(
            400, {"orderRejectTransaction": {
                "id": "102", "type": "MARKET_ORDER_REJECT"
            }}
        )
        result = self.submit()
        self.assertEqual(result.outcome, Outcome.REJECTED)
        with self.assertRaises(JournalError):
            self.submit(signal="s1")
        self.assertEqual(self.transport.posts, 1)

    def test_P19_timeout_unknown_no_second_post(self):
        self.transport.submit_failure = BrokerCommunicationError("Timeout")
        result = self.submit()
        self.assertEqual(result.outcome, Outcome.UNKNOWN)
        self.assertEqual(self.transport.posts, 1)
        saved = self.journal.get(result.attempt_id)
        self.assertEqual(saved["status"], "UNKNOWN")
        with self.assertRaises(PreflightRejected):
            self.submit(signal="s2")
        self.assertEqual(self.transport.posts, 1)
        self.assertIn(("GET", "/orders/@" + result.attempt_id), self.transport.calls)

    def test_P21_restart_then_read_only_reconcile_to_fill(self):
        self.transport.submit_failure = BrokerCommunicationError("lost response")
        first = self.submit()
        self.assertEqual(first.outcome, Outcome.UNKNOWN)
        self.transport.provision_reconciliation("FILLED")
        restarted = OrderEngine(
            "acct-1", transport=self.transport,
            journal=IntentJournal("acct-1", directory=self.temp.name)
        )
        resolved = restarted.reconcile(first.attempt_id)
        self.assertEqual(resolved.outcome, Outcome.FILLED)
        self.assertTrue(resolved.protection_verified)
        self.assertEqual(self.transport.posts, 1)
        self.assertEqual(self.journal.get(first.attempt_id)["status"], "FILLED")

    def test_P21_invalid_reconciliation_does_not_authorize_retry(self):
        self.transport.submit_failure = BrokerCommunicationError("timeout")
        first = self.submit()
        self.transport.provision_reconciliation("FILLED", bad_tx=True)
        result = self.engine.reconcile(first.attempt_id)
        self.assertEqual(result.outcome, Outcome.UNKNOWN)
        self.assertEqual(self.transport.posts, 1)

    def test_P20_reconcile_cancelled_without_resend(self):
        self.transport.submit_failure = BrokerCommunicationError("timeout")
        first = self.submit()
        self.transport.provision_reconciliation("CANCELLED")
        result = self.engine.reconcile(first.attempt_id)
        self.assertEqual(result.outcome, Outcome.CANCELLED)
        self.assertEqual(self.transport.posts, 1)

    def test_P29_mismatched_stop_loss_blocks_future_entry(self):
        self.transport.broker_trade = {
            "id": "2201", "instrument": "EUR_USD", "state": "OPEN",
            "currentUnits": "500",
            "clientExtensions": {"id": "bad", "tag": "other"},
            "stopLossOrder": {"price": "1.0000"},
            "takeProfitOrder": {"price": "1.2000"},
        }
        out = self.submit()
        self.assertEqual(out.outcome, Outcome.FILLED)
        self.assertFalse(out.protection_verified)
        self.assertEqual(self.transport.posts, 1)
        with self.assertRaises(PreflightRejected):
            self.submit(signal="s2")

    def test_P28_unexpected_reduce_close_remains_unprotected(self):
        self.transport.submit_reply = BrokerReply(
            201, {"orderCreateTransaction": {"id": "300"},
                  "orderFillTransaction": {
                      "id": "301", "type": "ORDER_FILL",
                      "orderID": "300", "units": "1",
                      "price": "1.1002",
                      "tradeReduced": {"tradeID": "old", "units": "1"}
                  }}
        )
        out = self.submit()
        self.assertEqual(out.outcome, Outcome.FILLED)
        self.assertFalse(out.protection_verified)
        self.assertEqual(self.journal.get(out.attempt_id)["trade_reduced"]["tradeID"],
                         "old")
        self.assertEqual(self.transport.posts, 1)

    def test_P30_manual_trade_preflight_block(self):
        self.transport.account["account"]["openTradeCount"] = 1
        self.transport.account["account"]["trades"] = [{
            "id": "manual-1", "instrument": "EUR_USD", "currentUnits": "100"
        }]
        with self.assertRaises(PreflightRejected):
            self.submit()
        self.assertEqual(self.transport.posts, 0)

    def test_P29_pending_broker_order_preflight_block(self):
        self.transport.account["account"]["pendingOrderCount"] = 1
        self.transport.account["account"]["orders"] = [{
            "id": "pend-1", "instrument": "EUR_USD"
        }]
        with self.assertRaises(PreflightRejected):
            self.submit()
        self.assertEqual(self.transport.posts, 0)

    def test_P21_journal_write_failure_prevents_mutation(self):
        with patch.object(self.journal, "begin", side_effect=JournalError("disk full")):
            with self.assertRaises(JournalError):
                self.submit()
        self.assertEqual(self.transport.posts, 0)

    def test_P23_malformed_response_remains_unknown(self):
        self.transport.submit_reply = BrokerReply(201, None, "req-1")
        result = self.submit()
        self.assertEqual(result.outcome, Outcome.UNKNOWN)
        self.assertEqual(self.transport.posts, 1)


class OwnershipTests(unittest.TestCase):
    def test_P30_manual_and_spoofed_algin_trade_not_owned(self):
        entries = [{
            "attempt_id": "algin-aa", "trade_id": "100",
            "status": "FILLED", "protection_verified": True
        }]
        raw = [
            {"id": "100", "instrument": "EUR_USD",
             "clientExtensions": {"id": "algin-aa", "tag": "algin"},
             "stopLossOrder": {"price": "1.0"},
             "takeProfitOrder": {"price": "1.2"}},
            {"id": "101", "instrument": "GBP_USD",
             "clientExtensions": {"id": "algin-aa", "tag": "algin"},
             "stopLossOrder": {"price": "1.0"},
             "takeProfitOrder": {"price": "1.2"}},
            {"id": "102", "instrument": "USD_CAD",
             "stopLossOrder": {"price": "1.0"}}
        ]
        result = SafetyCheck.audit_raw_trades(raw, entries)
        self.assertNotIn("100", result)
        self.assertIn("101", result)
        self.assertFalse(result["101"]["owned"])
        self.assertIn("102", result)
        self.assertIn("Manual/foreign/unattributed trade", result["102"]["issues"])


class PracticeSafetyTests(unittest.TestCase):
    def test_P19_default_transport_never_posts(self):
        with patch.object(config, "ORDER_WRITES_ENABLED", False):
            from unittest.mock import Mock
            requests_session = Mock()
            transport = PracticeTransport(session=requests_session)
            with self.assertRaises(BrokerWriteDisabled):
                transport.submit_market_order({"order": {"units": "10"}})
            requests_session.request.assert_not_called()

    def test_P19_order_engine_default_transport_blocked_before_journal(self):
        with TemporaryDirectory() as temp:
            ledger = IntentJournal("acct-1", directory=temp)
            with patch.object(config, "ORDER_WRITES_ENABLED", False):
                service = OrderEngine("acct-1", journal=ledger)
                plan, rules, conv = inputs()
                with self.assertRaises(BrokerWriteDisabled):
                    service.submit(plan, rules, conv, signal_id="sig", now=NOW)
                self.assertFalse(ledger.path.exists())


if __name__ == "__main__":
    unittest.main()
