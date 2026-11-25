"""Offline safety and compatibility checks for optional live news provider."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import os
import unittest
from unittest.mock import patch

import requests

import config
from data.economic_calendar_showmytrades import (
    SHOWMYTRADES_ENDPOINT, SUPPORTED_CURRENCIES,
    ShowMyTradesCalendarError, fetch_showmytrades_calendar,
)
from data.news_filter import NewsFilter
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
_DEFAULT = object()


def event(*, when=NOW, currency="USD", impact="high", identifier="usd-cpi"):
    return {
        "ts": int(when.timestamp()), "time": iso_utc(when),
        "currency": currency, "impact": impact, "title": "Consumer Price Index",
        "id": identifier, "status": "scheduled", "confidence": "official",
        "moved_from": "",
    }


def envelope(*, events=None):
    items = [event()] if events is None else events
    return {
        "server_time": int(NOW.timestamp()), "server_time_iso": iso_utc(NOW),
        "generated_at": iso_utc(NOW - timedelta(minutes=2)),
        "from": iso_utc(NOW - timedelta(hours=6)),
        "to": iso_utc(NOW + timedelta(days=1)),
        "count": len(items), "events": items,
    }


class Response:
    def __init__(self, body, *, status=200, content=None):
        self.status_code = status
        self.content = (json.dumps(body).encode("utf-8")
                        if content is None else content)

    def json(self):
        return json.loads(self.content)


class ShowMyTradesAdapterTests(unittest.TestCase):
    def fetch(self, payload=_DEFAULT, *, now=NOW, status=200):
        seen = []
        def get(url, **kwargs):
            seen.append((url, kwargs))
            return Response(envelope() if payload is _DEFAULT else payload,
                            status=status)
        return fetch_showmytrades_calendar(now=now, http_get=get), seen

    def test_pinned_uncredentialed_one_get_and_contract(self):
        result, calls = self.fetch()
        self.assertEqual(len(calls), 1)
        url, kwargs = calls[0]
        self.assertEqual(url, SHOWMYTRADES_ENDPOINT)
        self.assertEqual(kwargs["params"], {
            "currency": ",".join(SUPPORTED_CURRENCIES),
            "impact": "high", "days": 1,
        })
        self.assertEqual(kwargs["timeout"], 10)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(set(result), {
            "events", "fetched_at", "coverage_start", "coverage_end",
            "currencies",
        })
        self.assertEqual(result["fetched_at"],
                         iso_utc(NOW - timedelta(minutes=2)))
        self.assertEqual(result["currencies"], list(SUPPORTED_CURRENCIES))
        self.assertEqual(result["events"], [{
            "time": iso_utc(NOW), "currency": "USD",
            "impact": "HIGH", "title": "Consumer Price Index",
        }])

    def test_verified_empty_inventory_can_be_clear_but_not_missing_metadata(self):
        payload = envelope(events=[])
        actual, _ = self.fetch(payload)
        self.assertEqual(actual["events"], [])
        invalid = copy.deepcopy(payload)
        invalid.pop("count")
        with self.assertRaises(ShowMyTradesCalendarError):
            self.fetch(invalid)

    def test_stale_generation_clock_skew_and_insufficient_horizon_rejected(self):
        wrong = []
        p = envelope()
        p["generated_at"] = iso_utc(NOW - timedelta(minutes=31))
        wrong.append(p)
        p = envelope()
        p["server_time"] -= 301
        wrong.append(p)
        p = envelope()
        p["server_time_iso"] = iso_utc(NOW - timedelta(seconds=1))
        wrong.append(p)
        p = envelope()
        p["from"] = iso_utc(NOW - timedelta(minutes=29))
        wrong.append(p)
        p = envelope()
        p["to"] = iso_utc(NOW + timedelta(minutes=29))
        wrong.append(p)
        p = envelope()
        p["from"] = iso_utc(NOW - timedelta(days=4))
        wrong.append(p)
        p = envelope()
        p["generated_at"] = iso_utc(NOW + timedelta(minutes=1))
        wrong.append(p)
        for payload in wrong:
            with self.subTest(change=payload), self.assertRaises(
                ShowMyTradesCalendarError
            ):
                self.fetch(payload)

    def test_tentative_conflicting_unknown_impact_or_time_never_clears(self):
        errors = [
            {"status": "tentative"},
            {"confidence": "sources_disagree"},
            {"status": "moved"},
            {"moved_from": iso_utc(NOW - timedelta(days=1))},
            {"impact": "medium"},
            {"currency": "SGD"},
            {"currency": "usd"},
            {"ts": int(NOW.timestamp()) + 1},
            {"time": "2026-11-25T12:00:00+01:00"},
            {"title": ""},
            {"id": ""},
            {"status": None},
            {"confidence": None},
            {"time": iso_utc(NOW + timedelta(days=2)),
             "ts": int((NOW + timedelta(days=2)).timestamp())},
        ]
        for change in errors:
            with self.subTest(change=change), self.assertRaises(
                ShowMyTradesCalendarError
            ):
                self.fetch(envelope(events=[dict(event(), **change)]))
        with self.assertRaises(ShowMyTradesCalendarError):
            self.fetch(envelope(events=[event(), event()]))

    def test_moved_and_single_source_still_block_high_news(self):
        changed = event()
        changed["status"] = "moved"
        changed["confidence"] = "single_source"
        changed["moved_from"] = iso_utc(NOW - timedelta(hours=1))
        result, _ = self.fetch(envelope(events=[changed]))
        self.assertEqual(result["events"][0]["impact"], "HIGH")

    def test_http_json_invalid_inventory_and_large_body_fail_closed(self):
        for status in (301, 302, 401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaises(
                ShowMyTradesCalendarError
            ):
                self.fetch(status=status)
        for payload in ([], None, {}, {"error": "rate limited"}):
            with self.subTest(payload=payload), self.assertRaises(
                ShowMyTradesCalendarError
            ):
                self.fetch(payload)
        wrong = envelope()
        wrong["count"] = 0
        with self.assertRaises(ShowMyTradesCalendarError):
            self.fetch(wrong)
        def response(content):
            return lambda *a, **kw: Response({}, content=content)
        for body in (b"invalid JSON", b" " * (1024 * 1024 + 1)):
            with self.assertRaises(ShowMyTradesCalendarError):
                fetch_showmytrades_calendar(now=NOW, http_get=response(body))

    def test_network_failure_is_single_attempt_and_no_exception_leak(self):
        calls = []
        def fail(*args, **kwargs):
            calls.append(args)
            raise requests.Timeout("secret transport internals")
        with self.assertRaises(ShowMyTradesCalendarError) as context:
            fetch_showmytrades_calendar(now=NOW, http_get=fail)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret transport", str(context.exception))


class NewsFilterShowMyTradesIntegrationTests(unittest.TestCase):
    def setUp(self):
        patches = (
            patch.object(config, "STATIC_NEWS_LIST", None, create=True),
            patch.object(config, "NEWS_PROVIDER_URL", None, create=True),
            patch.object(config, "NEWS_CALENDAR_PROVIDER", "SHOWMYTRADES"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_usd_and_eur_high_lockout_edges_are_inclusive(self):
        for currency in ("EUR", "USD"):
            for minute in (-30, 0, 30):
                with self.subTest(currency=currency, minute=minute):
                    when = NOW + timedelta(minutes=minute)
                    payload = envelope(events=[event(
                        when=when, currency=currency,
                        identifier=f"{currency}-{minute}",
                    )])
                    with patch("data.economic_calendar_showmytrades.requests.get",
                               return_value=Response(payload)):
                        reason = NewsFilter().block_trading("EUR_USD", NOW)
                    self.assertIn("HIGH-impact lockout", reason)

    def test_outside_lockout_and_covered_empty_inventory_clear(self):
        for payload in (
            envelope(events=[]),
            envelope(events=[event(when=NOW + timedelta(minutes=31))]),
            envelope(events=[event(currency="JPY")]),
        ):
            with patch("data.economic_calendar_showmytrades.requests.get",
                       return_value=Response(payload)):
                self.assertIsNone(NewsFilter().block_trading("EUR_USD", NOW))

    def test_uncertain_data_or_network_failure_is_news_unknown(self):
        for response in (
            Response(envelope(), status=429),
            Response(envelope(events=[dict(event(), status="tentative")])),
            Response(envelope(), content=b"not JSON"),
        ):
            with patch("data.economic_calendar_showmytrades.requests.get",
                       return_value=response):
                self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                    "EUR_USD", NOW))

    def test_conflicting_custom_endpoint_is_unknown_without_network(self):
        with patch.object(config, "NEWS_PROVIDER_URL", "https://provider.test/news"), patch(
            "data.economic_calendar_showmytrades.requests.get",
            side_effect=AssertionError("should not call"),
        ):
            self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                "EUR_USD", NOW))

    def test_source_switch_invalidates_cached_clear_to_fail_closed(self):
        news = NewsFilter()
        with patch("data.economic_calendar_showmytrades.requests.get",
                   return_value=Response(envelope(events=[]))) as get:
            self.assertIsNone(news.block_trading("EUR_USD", NOW))
            self.assertIsNone(news.block_trading(
                "EUR_USD", NOW + timedelta(minutes=1)))
            self.assertEqual(get.call_count, 1)
        with patch.object(config, "NEWS_CALENDAR_PROVIDER", "FMP"), patch.dict(
            os.environ, {"FMP_API_KEY": ""}
        ):
            self.assertIn("NEWS_UNKNOWN", news.block_trading(
                "EUR_USD", NOW + timedelta(minutes=2)))

    def test_default_fmp_remains_unchanged_and_no_broker_mutations(self):
        self.assertFalse(config.ORDER_WRITES_ENABLED)
        with patch.object(config, "NEWS_CALENDAR_PROVIDER", "FMP"), patch.dict(
            os.environ, {"FMP_API_KEY": ""}
        ), patch("data.economic_calendar_showmytrades.requests.get",
                 side_effect=AssertionError("No accidental live fallback")):
            self.assertIn("NEWS_UNKNOWN", NewsFilter().block_trading(
                "EUR_USD", NOW))


if __name__ == "__main__":
    unittest.main()
