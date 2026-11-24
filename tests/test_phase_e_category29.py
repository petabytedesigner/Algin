"""Offline Category 29 tests; no actual broker network or mutations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

import config
from data.historical_oanda import (
    HistoricalDataError, _candle, fetch_range, load_dataset, save_dataset,
)
from broker.practice_transport import PRACTICE_API


UTC = timezone.utc
START = datetime(2026, 11, 23, 12, tzinfo=UTC)


def make_bar(timestamp):
    return {
        "time": timestamp.isoformat().replace("+00:00", "Z"),
        "complete": True, "volume": 20,
        "mid": {"o": "1.1001", "h": "1.1003", "l": "1.0999", "c": "1.1001"},
        "bid": {"o": "1.1000", "h": "1.1002", "l": "1.0998", "c": "1.1000"},
        "ask": {"o": "1.1002", "h": "1.1004", "l": "1.1000", "c": "1.1002"},
    }


class FakeResponse:
    status_code = 200

    def __init__(self, candles):
        self.candles = candles

    def json(self):
        return {"instrument": "EUR_USD", "granularity": "M15",
                "candles": self.candles}


class Category29HistoricalTests(unittest.TestCase):
    def settings(self):
        return (
            patch.object(config, "OANDA_URL", PRACTICE_API),
            patch.object(config, "API_KEY", "synthetic-token"),
            patch.object(config, "ACCOUNT_ID", "test-001"),
        )

    def test_bounded_get_pagination_MBA_without_count_or_broker_write(self):
        requests = []
        start, end = START, START + timedelta(hours=15)
        candles = [make_bar(start + i * timedelta(minutes=15)) for i in range(60)]

        def get(method, url, *, headers, params):
            self.assertEqual(method, "GET")
            self.assertEqual(params["price"], "MBA")
            self.assertEqual(params["granularity"], "M15")
            self.assertEqual(params["smooth"], "false")
            self.assertEqual(params["includeFirst"], "true")
            self.assertNotIn("count", params)
            self.assertIn("/instruments/EUR_USD/candles", url)
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            left = datetime.fromisoformat(params["from"].replace("Z", "+00:00"))
            right = datetime.fromisoformat(params["to"].replace("Z", "+00:00"))
            self.assertLessEqual(right - left, timedelta(minutes=15 * 20))
            requests.append((left, right))
            return FakeResponse([b for b in candles if left <=
                                 datetime.fromisoformat(b["time"].replace("Z", "+00:00")) < right])

        with self.settings()[0], self.settings()[1], self.settings()[2]:
            result = fetch_range("EUR_USD", start, end, requester=get,
                                 max_bars_per_page=20, now=end)
        self.assertEqual(result["request_pages"], 3)
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(result["candles"]), 60)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["candles"][0]["bid"]["o"], "1.1000")
        self.assertEqual(result["candles"][-1]["ask"]["c"], "1.1002")

    def test_inweek_gap_is_never_silently_filled(self):
        start, end = START, START + timedelta(hours=1)
        bars = [make_bar(start + i * timedelta(minutes=15)) for i in (0, 1, 3)]
        with self.settings()[0], self.settings()[1], self.settings()[2]:
            with self.assertRaisesRegex(HistoricalDataError, "market-open gaps"):
                fetch_range("EUR_USD", start, end, requester=lambda *a, **k:
                            FakeResponse(bars), now=end)

    def test_weekend_gap_is_classified_and_retained(self):
        first = datetime(2026, 11, 20, 21, 45, tzinfo=UTC)
        second = datetime(2026, 11, 22, 22, 0, tzinfo=UTC)
        end = second + timedelta(minutes=15)
        bars = [make_bar(first), make_bar(second)]
        with self.settings()[0], self.settings()[1], self.settings()[2]:
            result = fetch_range("EUR_USD", first, end,
                                 requester=lambda *a, **k: FakeResponse(bars),
                                 now=end)
        self.assertEqual(len(result["gaps"]), 1)
        self.assertEqual(result["gaps"][0]["classification"], "WEEKEND_CLOSED")
        self.assertGreater(result["gaps"][0]["missing_bars"], 100)

    def test_duplicate_and_out_of_range_response_block(self):
        start, end = START, START + timedelta(minutes=45)
        with self.settings()[0], self.settings()[1], self.settings()[2]:
            with self.assertRaises(HistoricalDataError):
                fetch_range("EUR_USD", start, end,
                            requester=lambda *a, **k: FakeResponse(
                                [make_bar(start), make_bar(start)]), now=end)
            with self.assertRaises(HistoricalDataError):
                fetch_range("EUR_USD", start, end,
                            requester=lambda *a, **k: FakeResponse(
                                [make_bar(end)]), now=end)

    def test_incomplete_crossed_nonfinite_or_malformed_price_block(self):
        variants = []
        incomplete = make_bar(START)
        incomplete["complete"] = False
        variants.append(incomplete)
        crossed = make_bar(START)
        crossed["ask"]["o"] = crossed["bid"]["o"]
        variants.append(crossed)
        nonfinite = make_bar(START)
        nonfinite["mid"]["h"] = "NaN"
        variants.append(nonfinite)
        malformed = make_bar(START)
        malformed["bid"].pop("c")
        variants.append(malformed)
        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaises(HistoricalDataError):
                _candle(candidate)

    def test_save_load_checksum_and_tamper_rejection(self):
        end = START + timedelta(minutes=15)
        with self.settings()[0], self.settings()[1], self.settings()[2]:
            data = fetch_range("EUR_USD", START, end,
                               requester=lambda *a, **k: FakeResponse([make_bar(START)]),
                               now=end)
        with TemporaryDirectory() as root:
            path = save_dataset(data, root)
            self.assertEqual(path, save_dataset(data, root))
            self.assertEqual(load_dataset(path), data)
            self.assertNotIn("synthetic-token", path.read_text())
            self.assertNotIn("test-001", path.read_text())
            before = path.read_bytes()
            path.write_bytes(before.replace(b"1.1000", b"1.9000"))
            with self.assertRaises(HistoricalDataError):
                load_dataset(path)

    def test_bad_endpoint_or_credentials_never_call_transport(self):
        called = []
        def get(*args, **kwargs):
            called.append(True)
            return FakeResponse([])
        with patch.object(config, "API_KEY", ""), patch.object(config, "ACCOUNT_ID", "test-001"):
            with self.assertRaises(HistoricalDataError):
                fetch_range("EUR_USD", START, START + timedelta(minutes=15),
                            requester=get, now=START + timedelta(minutes=15))
        self.assertEqual(called, [])

    def test_range_alignment_and_excess_request_guard(self):
        with self.assertRaises(HistoricalDataError):
            fetch_range("EUR_USD", START + timedelta(minutes=1),
                        START + timedelta(minutes=30), requester=lambda *a, **k: None)
        with self.assertRaises(HistoricalDataError):
            fetch_range("EUR_USD", START, START + timedelta(days=2000),
                        requester=lambda *a, **k: None)
        with self.assertRaises(HistoricalDataError):
            fetch_range("EUR_USD", START, START + timedelta(minutes=15),
                        max_bars_per_page=5000, requester=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
