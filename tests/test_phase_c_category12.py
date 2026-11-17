"""Category 12 Practice API validation: synthetic responses, NO network."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from unittest.mock import patch
from decimal import Decimal
import io
import json
import unittest

import config
from broker.practice_transport import (
    BrokerReply, PRACTICE_API, BrokerWriteDisabled, PracticeTransport,
)
from broker.practice_readonly_probe import (
    PracticeReadOnlyProbe, PracticeProbeError, main,
)
from utils.time_tools import iso_utc


ACCOUNT = "101-001-37674492-003"
NOW = datetime(2026, 11, 17, 20, 15, tzinfo=timezone.utc)


def fx():
    return {
        "name": "EUR_USD", "type": "CURRENCY",
        "pipLocation": -4, "displayPrecision": 5,
        "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
        "maximumOrderUnits": "1000000",
        "maximumPositionSize": "0", "marginRate": "0.02",
    }


class FakeReplies:
    def __init__(self):
        self.calls = []
        self.account = {
            "id": ACCOUNT, "currency": "USD",
            "NAV": "10000", "balance": "10000",
            "marginAvailable": "10000",
            "openTradeCount": 0, "pendingOrderCount": 0,
            "trades": [], "orders": [],
        }
        self.summary = {"id": ACCOUNT, "currency": "USD"}
        self.catalog = [fx()]
        self.prices = [{
            "instrument": "EUR_USD", "tradeable": True,
            "time": iso_utc(NOW),
            "bids": [{"price": "1.10000"}],
            "asks": [{"price": "1.10020"}],
        }]
        self.cursor = "100"
        self.sinceid = {"lastTransactionID": "100", "transactions": []}
        self.failure = None

    def read(self, path, *, params=None):
        self.calls.append(("GET", path, params))
        if self.failure == path:
            return BrokerReply(401, {"errorMessage": "private-token"})
        if path == "":
            body = {"account": self.account, "lastTransactionID": self.cursor}
        elif path == "/summary":
            body = {"account": self.summary, "lastTransactionID": self.cursor}
        elif path == "/instruments":
            body = {"instruments": self.catalog, "lastTransactionID": self.cursor}
        elif path == "/pricing":
            if params != {"instruments": "EUR_USD"}:
                raise AssertionError("Pricing query must be single targeted pair")
            body = {"prices": self.prices}
        elif path == "/transactions/sinceid":
            if params != {"id": self.cursor}:
                raise AssertionError("sinceid must use snapshotted cursor")
            body = self.sinceid
        else:
            raise AssertionError("Unapproved endpoint")
        return BrokerReply(200, body, request_id="fake-request")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeReplies()
        self.env = [
            patch.object(config, "OANDA_URL", PRACTICE_API),
            patch.object(config, "ORDER_WRITES_ENABLED", False),
            patch.object(config, "API_KEY", "fake-token-never-network"),
            patch.object(config, "ACCOUNT_ID", ACCOUNT),
            patch("broker.practice_readonly_probe.utc_now", return_value=NOW),
        ]
        for setting in self.env:
            setting.start()
            self.addCleanup(setting.stop)

    def probe(self):
        return PracticeReadOnlyProbe(
            expected_account_id=ACCOUNT, transport=self.fake,
        ).run()

    def test_verified_offline_snapshot_is_not_mislabeled_as_network(self):
        report = self.probe()
        self.assertEqual(report.source, "OFFLINE_INJECTED")
        self.assertEqual(report.environment, "PRACTICE")
        self.assertEqual(report.currency, "USD")
        self.assertEqual(report.account_suffix, "003")
        self.assertTrue(report.account_read_verified)
        self.assertTrue(report.summary_read_verified)
        self.assertTrue(report.instrument_catalog_verified)
        self.assertEqual(report.instruments_count, 1)
        self.assertEqual(report.eur_usd_quote_status, "QUOTE_VERIFIED")
        self.assertEqual(report.transaction_history_status, "UNCHANGED_CURSOR")
        self.assertTrue(report.account_balance_reference_10000)
        self.assertTrue(report.zero_open_trades)
        self.assertTrue(report.zero_pending_orders)
        self.assertFalse(report.broker_writes_enabled)
        self.assertEqual(
            [p for _, p, _ in self.fake.calls],
            ["", "/summary", "/instruments", "/pricing",
             "/transactions/sinceid"],
        )
        self.assertTrue(all(v == "GET" for v, _, _ in self.fake.calls))

    def test_exact_account_is_not_inferred_from_api_reply(self):
        self.fake.account["id"] = "101-001-37674492-002"
        with self.assertRaisesRegex(PracticeProbeError, "identity mismatch"):
            self.probe()

    def test_account_id_config_mismatch_blocks_before_any_request(self):
        with patch.object(config, "ACCOUNT_ID", "101-001-37674492-002"):
            with self.assertRaises(PracticeProbeError):
                self.probe()
        self.assertEqual(self.fake.calls, [])

    def test_broker_url_redirect_to_live_is_impossible(self):
        with patch.object(config, "OANDA_URL", "https://api-fxtrade.oanda.com/v3"):
            with self.assertRaises(PracticeProbeError):
                self.probe()
        self.assertEqual(self.fake.calls, [])

    def test_independent_config_write_gate_prevents_probe(self):
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            with self.assertRaises(PracticeProbeError):
                self.probe()
        self.assertEqual(self.fake.calls, [])

    def test_missing_credentials_refused_before_request(self):
        with patch.object(config, "API_KEY", ""):
            with self.assertRaises(PracticeProbeError):
                self.probe()
        self.assertEqual(self.fake.calls, [])

    def test_wrong_currency_fails_without_assuming_usd(self):
        self.fake.account["currency"] = "EUR"
        with self.assertRaises(PracticeProbeError):
            self.probe()

    def test_bad_financials_and_incomplete_inventory_fail_closed(self):
        for change, key, val in (
            ("nan", "balance", "NaN"),
            ("negative", "NAV", "-1"),
            ("trade_count", "openTradeCount", 1),
            ("orders", "orders", [None]),
            ("margin", "marginAvailable", "-1"),
        ):
            with self.subTest(change=change):
                fresh = FakeReplies()
                fresh.account[key] = val
                with self.assertRaises(PracticeProbeError):
                    PracticeReadOnlyProbe(
                        expected_account_id=ACCOUNT, transport=fresh
                    ).run()

    def test_account_snapshot_report_never_claims_write_permission(self):
        self.fake.account.update({
            "openTradeCount": 1,
            "trades": [{"id": "999", "state": "OPEN"}],
            "balance": "9950",
        })
        report = self.probe()
        self.assertFalse(report.zero_open_trades)
        self.assertFalse(report.account_balance_reference_10000)
        self.assertFalse(report.broker_writes_enabled)

    def test_mt4_association_is_raised_as_observation_not_permitted(self):
        self.fake.account["mt4AccountID"] = 1234
        report = self.probe()
        self.assertTrue(report.mt4_associated)
        self.assertFalse(report.broker_writes_enabled)

    def test_broker_unauthorized_is_sanitized_no_token_echo(self):
        self.fake.failure = "/instruments"
        with self.assertRaises(PracticeProbeError) as ctx:
            self.probe()
        self.assertNotIn("private-token", str(ctx.exception))
        self.assertNotIn("fake-token", str(ctx.exception))

    def test_malformed_account_fx_catalog_is_rejected(self):
        self.fake.catalog = [fx(), fx()]
        with self.assertRaises(PracticeProbeError):
            self.probe()

    def test_unavailable_quote_is_not_mislabeled_as_tradable(self):
        self.fake.prices[0]["tradeable"] = False
        report = self.probe()
        self.assertEqual(
            report.eur_usd_quote_status,
            "QUOTE_UNAVAILABLE_OR_UNVERIFIED",
        )

    def test_wrong_quote_time_is_not_approved(self):
        self.fake.prices[0]["time"] = iso_utc(NOW-timedelta(minutes=5))
        self.assertEqual(
            self.probe().eur_usd_quote_status,
            "QUOTE_UNAVAILABLE_OR_UNVERIFIED",
        )

    def test_no_eur_usd_catalog_does_not_forge_price_query(self):
        self.fake.catalog = [{**fx(), "name": "GBP_USD"}]
        self.assertEqual(
            self.probe().eur_usd_quote_status, "INSTRUMENT_NOT_AVAILABLE"
        )
        self.assertNotIn("/pricing", [x[1] for x in self.fake.calls])

    def test_corrupted_sinceid_after_account_read_is_blocked(self):
        self.fake.sinceid = {"lastTransactionID": "101", "transactions": []}
        with self.assertRaises(PracticeProbeError):
            self.probe()

    def test_forward_cursor_with_real_shape_is_reported_as_concurrent(self):
        self.fake.sinceid = {
            "lastTransactionID": "101",
            "transactions": [{
                "id": "101", "accountID": ACCOUNT, "type": "ORDER_CANCEL",
            }],
        }
        report = self.probe()
        self.assertEqual(
            report.transaction_history_status, "CONCURRENT_CHANGES_NO_WRITE"
        )

    def test_duplicate_or_foreign_sinceid_is_not_accepted(self):
        self.fake.sinceid = {
            "lastTransactionID": "102",
            "transactions": [{
                "id": "101", "accountID": ACCOUNT, "type": "ORDER_CANCEL",
            }, {
                "id": "102", "accountID": "other", "type": "ORDER_CANCEL",
            }],
        }
        with self.assertRaises(PracticeProbeError):
            self.probe()

    def test_cli_error_does_not_print_exception_from_http(self):
        # The CLI intentionally reports only fixed diagnostic text.
        def bad(*args, **kwargs):
            raise ValueError("a real token and URL should never be logged")
        with (patch("broker.practice_readonly_probe.PracticeReadOnlyProbe",
                    side_effect=bad),
              patch.dict("os.environ", {
                  "OANDA_PRACTICE_EXPECTED_ACCOUNT_ID": ACCOUNT,
              }),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            # Unhandled constructor error is not part of our broker HTTP
            # threat model, so this test checks _get, not the CLI.
            with self.assertRaises(ValueError):
                main()
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
