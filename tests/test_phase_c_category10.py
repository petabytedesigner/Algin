"""Phase C / Category 10: account instruments, quote and news evidence.

All data is synthetic and read-only. These tests deliberately verify the
unavailable/unknown states; no actual OANDA or economic-calendar call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from tempfile import TemporaryDirectory
import copy
import unittest

import config
import main
from broker.account_instruments import (
    InstrumentCatalogError, parse_account_instruments,
)
from data.data_handler import DataHandler
from data.news_filter import NewsFilter, NewsUnavailable
from risk.trade_plan import InstrumentRules
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
D = Decimal


class Reply:
    status_code = 200

    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


def fx_spec(name="EUR_USD", pip=-4, precision=5):
    return {
        "name": name, "type": "CURRENCY", "pipLocation": pip,
        "displayPrecision": precision, "tradeUnitsPrecision": 0,
        "minimumTradeSize": "1", "maximumOrderUnits": "1000000",
        "maximumPositionSize": "0", "marginRate": "0.02",
    }


def catalog(*items, tx="100"):
    return {"lastTransactionID": tx, "instruments": list(items)}


def price(*, bid="1.10000", ask="1.10020", now=NOW, tradeable=True,
          instrument="EUR_USD"):
    return {"prices": [{
        "instrument": instrument, "tradeable": tradeable,
        "time": iso_utc(now),
        "bids": [{"price": bid}], "asks": [{"price": ask}],
    }]}


def rules():
    return parse_account_instruments(catalog(fx_spec()))["EUR_USD"]


def market_data(reply):
    return (
        patch.object(config, "API_KEY", "synthetic-token"),
        patch.object(config, "ACCOUNT_ID", "acct-1"),
        patch("data.data_handler.safe_request", return_value=Reply(reply)),
        patch("data.data_handler.utc_now", return_value=NOW),
    )


class InstrumentEvidenceTests(unittest.TestCase):
    def test_account_catalog_provides_verified_fx_rules_not_inferred_symbols(self):
        data = parse_account_instruments(catalog(
            fx_spec(),
            {"name": "XAU_USD", "type": "METAL"},
        ))
        self.assertEqual(tuple(data), ("EUR_USD",))
        self.assertEqual(data["EUR_USD"].pip_location, -4)
        self.assertEqual(data["EUR_USD"].price_step, D("0.00001"))
        self.assertIsNone(data.get("GBP_USD"))

    def test_account_catalog_rejects_duplicate_instrument_names(self):
        with self.assertRaises(InstrumentCatalogError):
            parse_account_instruments(catalog(fx_spec(), fx_spec()))

    def test_account_catalog_requires_numeric_cursor_and_nonempty_list(self):
        for payload in (
            catalog(fx_spec(), tx="unknown"),
            catalog(),
            {"instruments": [fx_spec()]},
            {"lastTransactionID": "101", "instruments": "EUR_USD"},
            {"lastTransactionID": "101", "instruments": [None]},
        ):
            with self.subTest(payload=payload), self.assertRaises(InstrumentCatalogError):
                parse_account_instruments(payload)

    def test_account_catalog_rejects_missing_or_invalid_fx_metadata(self):
        for change in (
            {"pipLocation": True},
            {"displayPrecision": "5"},
            {"maximumOrderUnits": "NaN"},
            {"marginRate": "2"},
            {"tradeUnitsPrecision": -1},
            {"name": "EUR/../../USD"},
        ):
            sample = {**fx_spec(), **change}
            with self.subTest(change=change), self.assertRaises(InstrumentCatalogError):
                parse_account_instruments(catalog(sample))

    def test_explicit_empty_fx_set_is_not_a_permission_to_guess(self):
        result = parse_account_instruments(catalog(
            {"name": "XAU_USD", "type": "METAL"}
        ))
        self.assertEqual(result, {})

    def test_handler_get_account_instruments_is_account_scoped_get_only(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for cm in market_data(catalog(fx_spec())):
                stack.enter_context(cm)
            handler = DataHandler()
            result = handler.get_account_instruments()
        self.assertEqual(tuple(result), ("EUR_USD",))
        self.assertIsInstance(result["EUR_USD"], InstrumentRules)

    def test_handler_does_not_accept_partial_catalog_or_failed_http(self):
        from contextlib import ExitStack
        for payload, code in (
            (catalog(fx_spec(), tx="??"), 200),
            (catalog(fx_spec()), 401),
            ({}, 200),
        ):
            with self.subTest(payload=payload, status=code), ExitStack() as stack:
                for cm in market_data(payload):
                    stack.enter_context(cm)
                with patch("data.data_handler.safe_request",
                           return_value=Reply(payload, code)):
                    self.assertIsNone(DataHandler().get_account_instruments())


class QuoteEvidenceTests(unittest.TestCase):
    def request(self, payload, *, expected_rules=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for cm in market_data(payload):
                stack.enter_context(cm)
            return DataHandler().get_price(
                "EUR_USD", rules=expected_rules or rules()
            )

    def test_exact_fresh_quote_accepts_canonical_broker_precision(self):
        q = self.request(price())
        self.assertIsNotNone(q)
        self.assertEqual(q["bid"], 1.1)
        self.assertAlmostEqual(q["ask"] - q["bid"], .0002)

    def test_above_policy_spread_is_not_tradeable_evidence(self):
        self.assertIsNone(self.request(price(ask="1.10030")))
        self.assertIsNotNone(self.request(price(ask="1.10025")))

    def test_invalid_quote_precision_is_not_rounded_into_validity(self):
        self.assertIsNone(self.request(price(ask="1.100205")))

    def test_stale_or_future_quote_blocks_even_if_tradeable(self):
        self.assertIsNone(self.request(price(
            now=NOW - timedelta(seconds=21)
        )))
        self.assertIsNone(self.request(price(
            now=NOW + timedelta(seconds=3)
        )))

    def test_crossed_locked_or_unavailable_bid_ask_blocks(self):
        for payload in (
            price(bid="1.10100", ask="1.10000"),
            price(bid="1.10020", ask="1.10020"),
            price(bid="-1", ask="1.10020"),
            price(bid="NaN"),
            {"prices": [{"instrument": "EUR_USD", "tradeable": True,
                         "time": iso_utc(NOW), "bids": [], "asks": []}]},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(self.request(payload))

    def test_broker_false_tradeability_overrides_legacy_status(self):
        payload = price(tradeable=False)
        payload["prices"][0]["status"] = "tradeable"
        self.assertIsNone(self.request(payload))

    def test_duplicate_or_wrong_instrument_quote_is_not_evidence(self):
        p = price()
        self.assertIsNone(self.request({"prices": p["prices"] * 2}))
        self.assertIsNone(self.request(price(instrument="GBP_USD")))

    def test_quote_requires_exact_account_instrument_rules(self):
        wrong = fx_spec("GBP_USD")
        spec = parse_account_instruments(catalog(wrong))["GBP_USD"]
        self.assertIsNone(self.request(price(), expected_rules=spec))


class NewsCoverageTests(unittest.TestCase):
    def payload(self, *, start=None, end=None, currencies=None, events=None):
        return {
            "fetched_at": iso_utc(NOW),
            "coverage_start": iso_utc(start or NOW - timedelta(hours=2)),
            "coverage_end": iso_utc(end or NOW + timedelta(hours=2)),
            "currencies": currencies if currencies is not None
                else ["EUR", "USD", "GBP", "JPY"],
            "events": [] if events is None else events,
        }

    def static(self, payload):
        return patch.multiple(
            config, create=True,
            STATIC_NEWS_LIST=payload.get("events"),
            STATIC_NEWS_FETCHED_AT=payload.get("fetched_at"),
            STATIC_NEWS_COVERAGE_START=payload.get("coverage_start"),
            STATIC_NEWS_COVERAGE_END=payload.get("coverage_end"),
            STATIC_NEWS_CURRENCIES=payload.get("currencies"),
        )

    def test_empty_events_without_coverage_is_unknown(self):
        data = {"events": [], "fetched_at": iso_utc(NOW)}
        with self.static(data):
            self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                "EUR_USD", NOW
            ))

    def test_fully_covered_empty_event_window_is_synthetic_clear(self):
        with self.static(self.payload()):
            self.assertIsNone(NewsFilter().block_trading("EUR_USD", NOW))

    def test_uncovered_currency_must_never_be_declared_clear(self):
        with self.static(self.payload(currencies=["EUR", "GBP"])):
            self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                "EUR_USD", NOW
            ))

    def test_partial_time_horizon_must_never_be_declared_clear(self):
        for p in (
            self.payload(start=NOW - timedelta(minutes=29)),
            self.payload(end=NOW + timedelta(minutes=29)),
        ):
            with self.subTest(p=p), self.static(p):
                self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                    "EUR_USD", NOW
                ))

    def test_invalid_coverage_and_duplicate_currencies_block(self):
        for payload in (
            self.payload(currencies=["EUR", "EUR"]),
            self.payload(currencies=[]),
            self.payload(currencies=["EUR", "usd"]),
            self.payload(start=NOW + timedelta(hours=1),
                         end=NOW - timedelta(hours=1)),
        ):
            with self.subTest(payload=payload), self.static(payload):
                self.assertIn("NEWS_UNKNOWN",
                              NewsFilter().block_trading("EUR_USD", NOW))

    def test_verified_high_impact_blocks_only_covered_pair(self):
        event = {
            "time": iso_utc(NOW + timedelta(minutes=15)),
            "currency": "USD", "impact": "HIGH", "title": "Synthetic CPI",
        }
        with self.static(self.payload(events=[event])):
            source = NewsFilter()
            self.assertIn("HIGH-impact", source.block_trading("EUR_USD", NOW))
            self.assertIsNone(source.block_trading("GBP_JPY", NOW))

    def test_provider_events_outside_claimed_coverage_are_unknown(self):
        p = self.payload(
            events=[{
                "time": iso_utc(NOW + timedelta(days=3)),
                "currency": "USD", "impact": "HIGH", "title": "Late CPI",
            }]
        )
        with self.static(p):
            self.assertIn("NEWS_UNKNOWN",
                          NewsFilter().block_trading("EUR_USD", NOW))

    def test_in_place_static_event_update_invalidates_cache(self):
        data = self.payload()
        with self.static(data):
            source = NewsFilter()
            self.assertIsNone(source.block_trading("EUR_USD", NOW))
            config.STATIC_NEWS_LIST.append({
                "time": iso_utc(NOW + timedelta(minutes=10)),
                "currency": "USD", "impact": "HIGH",
                "title": "Inserted after cache",
            })
            self.assertIn("HIGH-impact",
                          source.block_trading("EUR_USD", NOW))

    def test_unapproved_https_host_cannot_trigger_external_fetch(self):
        with (
            patch.object(config, "STATIC_NEWS_LIST", None, create=True),
            patch.object(config, "NEWS_PROVIDER_URL",
                         "https://not-approved.example/news", create=True),
            patch.object(config, "NEWS_PROVIDER_HOST",
                         "calendar.approved.example", create=True),
            patch("requests.get") as http,
        ):
            result = NewsFilter().block_trading("EUR_USD", NOW)
        self.assertIn("NEWS_UNKNOWN", result)
        http.assert_not_called()

    def test_unconfigured_provider_is_always_unknown(self):
        with (
            patch.object(config, "STATIC_NEWS_LIST", None, create=True),
            patch.object(config, "NEWS_PROVIDER_URL", None, create=True),
        ):
            self.assertIn("NEWS_UNKNOWN",
                          NewsFilter().block_trading("EUR_USD", NOW))

    def test_malformed_instrument_does_not_get_news_clear(self):
        with self.static(self.payload()):
            self.assertIn("NEWS_UNKNOWN",
                          NewsFilter().block_trading("../../USD", NOW))


class ReadOnlyScanTests(unittest.TestCase):
    def test_missing_account_catalog_blocks_before_candle_or_order(self):
        with (
            patch.object(config, "API_KEY", "synthetic"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
            patch("main.TradeExecutor") as broker,
            patch("main.DataHandler") as market,
            patch("main.Heartbeat"),
            patch("main.EventLog"),
        ):
            broker.return_value.get_open_trades.return_value = []
            market.return_value.get_account_instruments.return_value = None
            self.assertEqual(main.scan_once(), 3)
            market.return_value.get_candles.assert_not_called()
            market.return_value.get_price.assert_not_called()

    def test_account_catalog_limits_which_pairs_can_be_scanned(self):
        with (
            patch.object(config, "API_KEY", "synthetic"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
            patch("main.TradeExecutor") as broker,
            patch("main.DataHandler") as market,
            patch("main.Heartbeat"),
            patch("main.EventLog"),
            patch("main.build_news_filter"),
            patch("main.CandleGate"),
        ):
            broker.return_value.get_open_trades.return_value = []
            market.return_value.get_account_instruments.return_value = {
                "EUR_USD": rules()
            }
            market.return_value.get_candles.return_value = None
            self.assertEqual(main.scan_once(), 0)
            market.return_value.get_candles.assert_called_once_with(
                "EUR_USD", "M15", 200
            )
            market.return_value.get_price.assert_not_called()


if __name__ == "__main__":
    unittest.main()
