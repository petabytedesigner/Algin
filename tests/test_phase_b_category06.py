"""Phase B Category 06: OANDA REST v20 contract/offline safety tests.

All HTTP is stubbed. No user credentials, no live account, no real mutation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import unittest

import requests

import config
from broker.account_reconciler import AccountReconciler, ReconcileError
from broker.execution_result import Outcome, classify_response
from broker.oanda_v20_contract import (
    OandaContractError, account_id, price_tradeable, transaction_id,
    validate_sinceid_batch,
)
from broker.order_engine import OrderEngine
from broker.practice_transport import (
    BrokerCommunicationError, BrokerReply, BrokerWriteDisabled, PracticeTransport,
)
from broker.risk_data import get_quote_conversions
from data.data_handler import DataHandler
from execution.trade_coordinator import SimulatedOrderTransport
from risk.trade_plan import PlanRejected
from storage.lifecycle_state import LifecycleStore, ProcessLease
from storage.intent_journal import IntentJournal
from utils.common import get_pricing, safe_request

from test_category3 import FakeTransport, inputs, NOW, D
from test_phase_a_category03 import FakeAccount


def tx(number, *, account="acct-1", kind="ORDER_CANCEL"):
    return {"id": str(number), "accountID": account, "type": kind}


class V20PriceTests(unittest.TestCase):
    def test_current_tradeable_boolean_outweighs_deprecated_status(self):
        self.assertTrue(price_tradeable({
            "tradeable": True, "status": "non-tradeable"
        }))
        self.assertFalse(price_tradeable({
            "tradeable": False, "status": "tradeable"
        }))
        self.assertFalse(price_tradeable({
            "tradeable": "true", "status": "tradeable"
        }))
        self.assertTrue(price_tradeable({"status": "tradeable"}))
        self.assertFalse(price_tradeable({"status": "invalid"}))
        self.assertFalse(price_tradeable({}))

    def test_order_preflight_quote_uses_v20_boolean_authority(self):
        from broker.execution_validation import parse_market_quote
        rules = inputs()[1]
        for flag, status, acceptable in (
            (True, "non-tradeable", True),
            (False, "tradeable", False),
            ("true", "tradeable", False)
        ):
            with self.subTest(flag=flag):
                data = {"prices": [{
                    "instrument": "EUR_USD", "tradeable": flag,
                    "status": status, "time": "2026-11-15T10:00:00Z",
                    "bids": [{"price": "1.1000"}],
                    "asks": [{"price": "1.1002"}],
                }]}
                if acceptable:
                    self.assertEqual(parse_market_quote(
                        data, "EUR_USD", rules, now=NOW,
                        max_age_seconds=20, max_spread_pips=D("2.5")
                    ).instrument, "EUR_USD")
                else:
                    from broker.execution_validation import PreflightRejected
                    with self.assertRaises(PreflightRejected):
                        parse_market_quote(
                            data, "EUR_USD", rules, now=NOW,
                            max_age_seconds=20, max_spread_pips=D("2.5")
                        )

    def test_risk_conversions_prefer_boolean_but_preserve_home_factors(self):
        from test_category2 import Response
        payload = {"prices": [{
            "instrument": "EUR_USD",
            "tradeable": True, "status": "invalid",
            "time": "2026-11-15T10:00:00Z",
            "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1002"}],
        }], "homeConversions": [
            {"currency": "EUR", "accountLoss": "1.1", "positionValue": "1.1"}
        ]}
        with (patch.object(config, "API_KEY", "synthetic-only"),
              patch.object(config, "ACCOUNT_ID", "acct-1"),
              patch("broker.risk_data.safe_request", return_value=Response(payload))):
            quote = get_quote_conversions("EUR_USD", "USD")
        self.assertEqual(quote.bid, D("1.1000"))
        self.assertEqual(quote.base_value_to_home, D("1.1"))
        payload["prices"][0]["tradeable"] = False
        with (patch.object(config, "API_KEY", "synthetic-only"),
              patch.object(config, "ACCOUNT_ID", "acct-1"),
              patch("broker.risk_data.safe_request", return_value=Response(payload))):
            with self.assertRaises(PlanRejected):
                get_quote_conversions("EUR_USD", "USD")

    def test_data_handler_and_compat_reader_agree_on_status_priority(self):
        import data.data_handler as dh
        import utils.common as common
        from test_category5 import Response
        payload = {"prices": [{
            "instrument": "EUR_USD", "tradeable": True,
            "status": "invalid", "time": "2026-11-15T10:00:00Z",
            "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1002"}],
        }]}
        with (patch.object(config, "API_KEY", "offline-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct-1"),
              patch.object(dh, "utc_now", return_value=NOW),
              patch.object(dh, "safe_request", return_value=Response(payload)),
              patch.object(common, "safe_request", return_value=Response(payload))):
            self.assertIsNotNone(DataHandler().get_price("EUR_USD"))
            self.assertIsNotNone(get_pricing("EUR_USD"))
            payload["prices"][0]["tradeable"] = False
            self.assertIsNone(DataHandler().get_price("EUR_USD"))
            self.assertIsNone(get_pricing("EUR_USD"))


class V20FillTests(unittest.TestCase):
    def modern(self, *, legacy=False, trade_price=True):
        fill = {
            "id": "2201", "type": "ORDER_FILL",
            "orderID": "2200", "units": "100", "fullVWAP": "1.1999",
            "tradeOpened": {"tradeID": "2201", "units": "100"},
        }
        if legacy:
            fill["price"] = "1.1998"
        if trade_price:
            fill["tradeOpened"]["price"] = "1.1002"
        return BrokerReply(201, {
            "orderCreateTransaction": {"id": "2200"},
            "orderFillTransaction": fill
        })

    def test_deprecated_transaction_price_is_not_required(self):
        result = classify_response(self.modern(), "algin-test")
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertEqual(result.fill_price, D("1.1002"))
        self.assertEqual(result.trade_id, "2201")

    def test_new_trade_price_takes_precedence_over_legacy_and_vwap(self):
        result = classify_response(
            self.modern(legacy=True), "algin-test"
        )
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertEqual(result.fill_price, D("1.1002"))

    def test_legacy_price_still_works_when_no_trade_price(self):
        result = classify_response(
            self.modern(legacy=True, trade_price=False), "algin-test"
        )
        self.assertEqual(result.outcome, Outcome.FILLED)
        self.assertEqual(result.fill_price, D("1.1998"))

    def test_fullvwap_alone_never_forges_exact_trade_execution_price(self):
        result = classify_response(
            self.modern(trade_price=False), "algin-test"
        )
        self.assertEqual(result.outcome, Outcome.UNKNOWN)

    def test_modern_fill_goes_through_existing_order_engine_protection(self):
        class Modern(FakeTransport):
            def fill(self, *args, **kwargs):
                reply = super().fill(*args, **kwargs)
                trade = reply.body["orderFillTransaction"]
                trade["tradeOpened"]["price"] = trade.pop("price")
                trade["fullVWAP"] = "1.2000"
                return reply

        fake = Modern()
        plan, rules, conv = inputs()
        with TemporaryDirectory() as root:
            journal = IntentJournal("acct-1", directory=root)
            engine = OrderEngine("acct-1", transport=fake, journal=journal)
            result = engine.submit(
                plan, rules, conv, signal_id="one-modern-fill", now=NOW
            )
            self.assertEqual(result.outcome, Outcome.FILLED)
            self.assertTrue(result.protection_verified)
            self.assertEqual(result.fill_price, D("1.1002"))
            self.assertEqual(fake.posts, 1)
            self.assertEqual(journal.get(result.attempt_id)["status"], "FILLED")

    def test_unknown_modern_fill_is_never_rerouted_to_second_post(self):
        class NoExactPrice(FakeTransport):
            def fill(self, *args, **kwargs):
                reply = super().fill(*args, **kwargs)
                tx = reply.body["orderFillTransaction"]
                tx.pop("price")
                tx["fullVWAP"] = "1.1002"
                return reply

        fake = NoExactPrice()
        plan, rules, conv = inputs()
        with TemporaryDirectory() as root:
            journal = IntentJournal("acct-1", directory=root)
            engine = OrderEngine("acct-1", transport=fake, journal=journal)
            result = engine.submit(
                plan, rules, conv, signal_id="missing-exact-price", now=NOW
            )
            self.assertEqual(result.outcome, Outcome.UNKNOWN)
            self.assertEqual(fake.posts, 1)
            self.assertEqual(journal.snapshot()[0]["status"], "UNKNOWN")


class V20TransactionTests(unittest.TestCase):
    def batch(self, *transactions, last="150"):
        return {"transactions": list(transactions), "lastTransactionID": last}

    def validate(self, payload):
        return validate_sinceid_batch(
            payload, account="acct-1",
            previous_cursor="100", target_cursor="150",
        )

    def test_numeric_transaction_ids_and_account_identity(self):
        self.assertTrue(transaction_id("10000"))
        self.assertFalse(transaction_id("t100"))
        self.assertFalse(transaction_id("1/2"))
        self.assertFalse(transaction_id(200))
        self.assertTrue(account_id("101-001-12345-001"))
        self.assertFalse(account_id("acct/../other"))

    def test_sinceid_accepts_gaps_but_requires_last_snapshot_cursor(self):
        result = self.validate(self.batch(tx(103), tx(121), tx(150)))
        self.assertEqual(tuple(x["id"] for x in result), ("103", "121", "150"))

    def test_sinceid_rejects_foreign_missing_out_of_order_and_advanced_head(self):
        invalid = [
            self.batch(tx(101, account="other"), tx(150)),
            self.batch(tx(150), tx(101)),
            self.batch(tx(101), tx(101), tx(150)),
            self.batch(tx(101), tx(149)),
            self.batch(tx(101), tx(150), last="151"),
            self.batch(),
            self.batch(None),
            self.batch(tx(99), tx(150)),
            self.batch({"id": "150", "accountID": "acct-1"}),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(OandaContractError):
                    self.validate(payload)

    def test_reconciler_malformed_sinceid_cannot_move_durable_cursor(self):
        with TemporaryDirectory() as root:
            fake = FakeAccount()
            store = LifecycleStore("acct-1", directory=root)
            journal = IntentJournal("acct-1", directory=root)
            service = AccountReconciler(
                "acct-1", transport=fake, store=store, journal=journal,
                lease=ProcessLease("acct-1", directory=root),
            )
            with service:
                self.assertEqual(service.run().cursor, "100")
            fake.cursor = "150"
            fake.transactions = [tx(105), tx(149)]
            with service, self.assertRaises(ReconcileError):
                service.run()
            self.assertEqual(store.load()["cursor"], "100")
            self.assertEqual(store.load()["processed_transactions"], [])
            fake.transactions = [tx(105), tx(150)]
            with service:
                report = service.run()
            self.assertEqual(report.cursor, "150")
            self.assertEqual(store.load()["processed_transactions"], ["105", "150"])


class TransportBoundaryTests(unittest.TestCase):
    class Session:
        def __init__(self, reply=None, error=None):
            self.calls = []
            self.error = error
            self.reply = reply
        def mount(self, prefix, adapter):
            self.mounted = (prefix, adapter)
        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if self.error:
                raise self.error
            return self.reply

    class Response:
        status_code = 200
        headers = {"RequestID": "request-123"}
        def json(self):
            return {"account": {"id": "acct-1"}, "lastTransactionID": "100"}

    def test_get_never_follows_redirect_and_preserves_request_id(self):
        session = self.Session(self.Response())
        with (patch.object(config, "API_KEY", "fake-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            transport = PracticeTransport(session=session)
            reply = transport.read("")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.request_id, "request-123")
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.calls[0][0][0], "GET")

    def test_invalid_paths_and_account_ids_are_rejected_before_http(self):
        session = self.Session(self.Response())
        with (patch.object(config, "API_KEY", "fake-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            transport = PracticeTransport(session=session)
            for path in ("/../orders", "//evil", "/pricing?evil=1",
                         "/pricing#fragment", "/pricing%2forders", "/pricing\\x"):
                with self.subTest(path=path), self.assertRaises(BrokerCommunicationError):
                    transport.read(path)
        self.assertEqual(len(session.calls), 0)
        with (patch.object(config, "API_KEY", "fake-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct/../other")):
            with self.assertRaises(BrokerCommunicationError):
                transport.read("")
        self.assertEqual(len(session.calls), 0)

    def test_all_mutations_stay_blocked_before_network(self):
        session = self.Session(self.Response())
        with (patch.object(config, "API_KEY", "fake-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            transport = PracticeTransport(session=session)
            with self.assertRaises(BrokerWriteDisabled):
                transport.submit_market_order({"order": {}})
            with self.assertRaises(BrokerWriteDisabled):
                transport._call("PUT", "/orders", payload={})
        self.assertEqual(session.calls, [])

    def test_legacy_read_helper_disables_redirects_and_rejects_http_302(self):
        response = Mock()
        response.status_code = 302
        with patch("utils.common.requests.get", return_value=response) as get:
            got = safe_request(
                "GET", "https://api-fxpractice.oanda.com/v3/accounts/acct-1"
            )
        self.assertIs(got, response)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_timeout_is_sanitized_and_does_not_retry(self):
        session = self.Session(error=requests.Timeout("private-url-secret"))
        with (patch.object(config, "API_KEY", "fake-not-real"),
              patch.object(config, "ACCOUNT_ID", "acct-1")):
            transport = PracticeTransport(session=session)
            with self.assertRaises(BrokerCommunicationError) as ctx:
                transport.read("/pricing", params={"instruments": "EUR_USD"})
        self.assertNotIn("private-url-secret", str(ctx.exception))
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
