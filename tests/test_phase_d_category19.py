"""Phase D Category 19: FMP economic-calendar provider contract and news fail-close.

All HTTP is patched; no provider credentials, OANDA I/O or broker writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import os
import unittest
from unittest.mock import patch

import requests

import config
from data.economic_calendar_fmp import (
    FMP_ENDPOINT, FmpCalendarError, fetch_fmp_calendar
)
from data.news_filter import NewsFilter, NewsUnavailable

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
KEY = "synthetic-fmp-only-test-key-do-not-use"


def event(*, currency="USD", when=NOW, impact="High", title="CPI inflation"):
    return {
        "date": when.strftime("%Y-%m-%d %H:%M:%S"),
        "currency": currency, "impact": impact, "event": title,
        "country": "US", "actual": None, "estimate": None,
    }


class Response:
    def __init__(self, body, *, status_code=200, content=None):
        self.status_code = status_code
        self.content = (json.dumps(body).encode("utf-8")
                        if content is None else content)

    def json(self):
        return json.loads(self.content)


class FmpAdapterTests(unittest.TestCase):
    def call(self, body, *, now=NOW, status=200):
        calls = []
        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response(body, status_code=status)
        result = fetch_fmp_calendar(
            api_key=KEY, now=now, http_get=fake_get
        )
        return result, calls

    def test_pinned_get_range_and_full_currency_envelope(self):
        payload, calls = self.call([event()])
        self.assertEqual(len(calls), 1)
        url, kwargs = calls[0]
        self.assertEqual(url, FMP_ENDPOINT)
        self.assertEqual(kwargs["params"], {
            "from": "2026-11-16", "to": "2026-11-18", "apikey": KEY
        })
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(payload["fetched_at"], "2026-11-17T12:00:00Z")
        self.assertIn("EUR", payload["currencies"])
        self.assertIn("USD", payload["currencies"])
        self.assertEqual(payload["events"], [{
            "time": "2026-11-17T12:00:00Z",
            "currency": "USD", "impact": "HIGH",
            "title": "CPI inflation",
        }])
        self.assertNotIn(KEY, str(payload))

    def test_both_lockout_edges_use_utc_not_local_timezone(self):
        for gap in (-30, 30):
            stamp = NOW + timedelta(minutes=gap)
            payload, _ = self.call([event(when=stamp)])
            self.assertEqual(payload["events"][0]["time"],
                             stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
        with self.assertRaises(FmpCalendarError):
            self.call([dict(event(), date="2026-11-17T12:00:00+01:00")])

    def test_major_currency_events_kept_other_valid_currencies_ignored(self):
        payload, _ = self.call([
            event(currency="SGD", impact="High", title="Foreign news"),
            event(currency="EUR", impact="low", title="Euro final"),
            event(currency="USD", impact="MEDIUM", title="US final"),
        ])
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["events"][0]["impact"], "LOW")
        self.assertEqual(payload["events"][1]["impact"], "MEDIUM")
        with self.assertRaises(FmpCalendarError):
            self.call([event(currency="SGD")])

    def test_empty_unauthorized_malformed_and_excessive_inventory_fail_closed(self):
        for value in ([], None, {}, {"Error Message": "Subscription needed"},
                      [None], [42]):
            with self.subTest(body=repr(value)[:40]):
                with self.assertRaises(FmpCalendarError):
                    self.call(value)
        for code in (301, 302, 401, 403, 404, 429, 500):
            with self.subTest(status=code):
                with self.assertRaises(FmpCalendarError):
                    self.call([event()], status=code)

    def test_bad_record_fields_and_duplicate_events_fail_closed(self):
        wrong_rows = [
            dict(event(), impact="TBD"),
            dict(event(), impact="Holiday"),
            dict(event(), impact=None),
            dict(event(), currency=""),
            dict(event(), currency="us"),
            dict(event(), date="unknown"),
            dict(event(), date="2026-11-18"),
            dict(event(), date="2026-11-19 10:00:00"),
            dict(event(), event=""),
            dict(event(), event="x" * 241),
            [1, 2],
        ]
        for row in wrong_rows:
            with self.subTest(case=str(row)[:40]):
                with self.assertRaises(FmpCalendarError):
                    self.call([row])
        with self.assertRaises(FmpCalendarError):
            self.call([event(), event()])

    def test_network_error_is_one_get_and_key_never_in_exception(self):
        calls = []
        def timeout(url, **kwargs):
            calls.append(url)
            raise requests.Timeout("sensitive fake request")
        with self.assertRaises(FmpCalendarError) as cm:
            fetch_fmp_calendar(api_key=KEY, now=NOW, http_get=timeout)
        self.assertEqual(calls, [FMP_ENDPOINT])
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn("sensitive fake", str(cm.exception))

    def test_large_http_payload_is_rejected_before_parse(self):
        def too_large(*args, **kwargs):
            return Response([], content=b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(FmpCalendarError):
            fetch_fmp_calendar(api_key=KEY, now=NOW, http_get=too_large)

    def test_key_required_without_logging_or_fallback(self):
        for bad_key in ("", None, "secret\nvalue", " "):
            with self.subTest(key=repr(bad_key)):
                with self.assertRaises(FmpCalendarError):
                    fetch_fmp_calendar(api_key=bad_key, now=NOW,
                                       http_get=lambda *a, **k: None)


class FmpNewsFilterIntegrationTests(unittest.TestCase):
    def setUp(self):
        # The FMP adapter is selected only when there is no legacy explicit
        # pinned provider URL or static test fixture.
        self.static = patch.object(config, "STATIC_NEWS_LIST", None, create=True)
        self.legacy = patch.object(config, "NEWS_PROVIDER_URL", None, create=True)
        self.static.start()
        self.legacy.start()
        self.addCleanup(self.static.stop)
        self.addCleanup(self.legacy.stop)
        self.current_clock = NOW
        self.wall_clock = patch(
            "data.news_filter.utc_now", side_effect=lambda: self.current_clock
        )
        self.wall_clock.start()
        self.addCleanup(self.wall_clock.stop)

    def news(self, raw_rows, *, now=NOW):
        self.current_clock = now
        def fake_get(*args, **kwargs):
            return Response(raw_rows)
        with patch.dict(os.environ, {"FMP_API_KEY": KEY}), patch(
            "data.economic_calendar_fmp.requests.get", side_effect=fake_get
        ) as get:
            block = NewsFilter().block_trading("EUR_USD", now)
        self.assertEqual(get.call_count, 1)
        return block

    def test_high_usd_is_locked_and_unrelated_pair_is_clear(self):
        rows = [event(when=NOW + timedelta(minutes=15))]
        self.assertIn("HIGH-impact lockout", self.news(rows))
        with patch.dict(os.environ, {"FMP_API_KEY": KEY}), patch(
            "data.economic_calendar_fmp.requests.get",
            return_value=Response(rows)
        ):
            self.assertIsNone(NewsFilter().block_trading("GBP_JPY", NOW))

    def test_low_or_medium_never_triggers_high_impact_lock(self):
        for level in ("Low", "Medium"):
            with self.subTest(level=level):
                self.assertIsNone(self.news([event(impact=level)]))

    def test_outside_30_minute_window_remains_clear(self):
        self.assertIsNone(self.news([event(when=NOW + timedelta(minutes=31))]))
        self.assertIsNone(self.news([event(when=NOW - timedelta(minutes=31))]))

    def test_utc_midnight_boundary_is_covered(self):
        now = datetime(2026, 11, 18, 0, 10, tzinfo=timezone.utc)
        self.assertIn("HIGH-impact lockout", self.news([
            event(when=datetime(2026, 11, 17, 23, 50,
                                tzinfo=timezone.utc))
        ], now=now))

    def test_missing_fmp_key_is_news_unknown_not_clear(self):
        with patch.dict(os.environ, {"FMP_API_KEY": ""}), patch(
            "data.economic_calendar_fmp.requests.get",
            side_effect=AssertionError("No request permitted")
        ):
            result = NewsFilter().block_trading("EUR_USD", NOW)
        self.assertIn("NEWS_UNKNOWN", result)

    def test_http_error_and_bad_json_cannot_be_treated_as_no_news(self):
        for response in (Response([], status_code=429),
                         Response({"Error Message": "not subscribed"}),
                         Response([], content=b"<html>blocked</html>")):
            with self.subTest(status=response.status_code), patch.dict(
                os.environ, {"FMP_API_KEY": KEY}
            ), patch("data.economic_calendar_fmp.requests.get",
                     return_value=response):
                result = NewsFilter().block_trading("EUR_USD", NOW)
                self.assertIn("NEWS_UNKNOWN", result)
                self.assertNotIn(KEY, result)

    def test_cache_ttl_expiry_with_network_failure_is_unknown_not_old_clear(self):
        news = NewsFilter(ttl_minutes=2, max_age_minutes=30)
        with patch.dict(os.environ, {"FMP_API_KEY": KEY}), patch(
            "data.economic_calendar_fmp.requests.get",
            side_effect=[
                Response([event(impact="Low")]),
                requests.Timeout("provider now unavailable"),
            ]
        ) as get:
            self.assertIsNone(news.block_trading("EUR_USD", NOW))
            self.assertIsNone(news.block_trading(
                "EUR_USD", NOW + timedelta(minutes=1)))
            self.assertEqual(get.call_count, 1)
            result = news.block_trading(
                "EUR_USD", NOW + timedelta(minutes=3))
            self.assertIn("NEWS_UNKNOWN", result)
            self.assertEqual(get.call_count, 2)

    def test_api_key_switch_invalidates_cache_without_exposing_secrets(self):
        news = NewsFilter()
        get_reply = Response([event(impact="Low")])
        with patch("data.economic_calendar_fmp.requests.get",
                   return_value=get_reply) as get:
            with patch.dict(os.environ, {"FMP_API_KEY": KEY}):
                self.assertIsNone(news.block_trading("EUR_USD", NOW))
                self.assertNotIn(KEY, repr(news._source_identity))
            with patch.dict(os.environ, {"FMP_API_KEY": KEY + "-rotated"}):
                self.assertIsNone(news.block_trading("EUR_USD", NOW))
                self.assertNotIn(KEY, repr(news._source_identity))
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
