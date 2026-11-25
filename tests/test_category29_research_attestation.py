"""Small, offline Category 29 provenance tests; no broker or large replay."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from data.historical_oanda import _gaps
from data.historical_research_attestation import (
    ResearchAttestationError, _canonical, attest_research_export,
)


UTC = timezone.utc
START = datetime(2026, 11, 18, tzinfo=UTC)
END = START + timedelta(hours=1, minutes=45)
CUTOFF = "2026-11-18T00:15:00Z"
GAP = (
    "2026-11-18T00:45:00Z", "2026-11-18T01:15:00Z",
    "https://www.oanda.com/us-en/trading/holiday-trading-hours/",
)


def bar(minute):
    stamp = START + timedelta(minutes=minute)
    return {
        "time": stamp.isoformat().replace("+00:00", "Z"),
        "complete": True, "volume": 15,
        "mid": {"o": "1.1001", "h": "1.1003", "l": "1.0999", "c": "1.1001"},
        "bid": {"o": "1.1000", "h": "1.1002", "l": "1.0998", "c": "1.1000"},
        "ask": {"o": "1.1002", "h": "1.1004", "l": "1.1000", "c": "1.1002"},
    }


def envelope():
    candles = [bar(m) for m in (0, 15, 30, 75, 90)]
    return {
        "candles": candles,
        "from": "2026-11-18T00:00:00Z",
        "to": "2026-11-18T01:45:00Z",
        "gaps": _gaps(candles, START, END),
        "granularity": "M15", "instrument": "EUR_USD", "price": "MBA",
        "source": "OANDA_PRACTICE_V20_GET",
        "validation_status": "UNVERIFIED_NOT_BACKTEST_APPROVED",
    }


class ResearchAttestationTests(unittest.TestCase):
    def attest(self, dataset, evidence=(GAP,)):
        return attest_research_export(
            dataset, source_sha256="unit-test-digest", research_start=CUTOFF,
            expected_holidays=evidence,
        )

    def test_exact_evidenced_gap_attested_without_inventing_prices(self):
        source = envelope()
        original = deepcopy(source)
        payload, manifest = self.attest(source)
        self.assertEqual(source, original)
        self.assertEqual(payload["candles"], source["candles"][1:])
        self.assertEqual(len(payload["candles"]), 4)
        self.assertEqual(manifest["excluded_early_candles"], 1)
        self.assertTrue(manifest["no_prices_synthesized"])
        self.assertTrue(manifest["research_only"])
        self.assertTrue(manifest["not_broker_fill_verified"])
        self.assertEqual(manifest["remaining_unexplained_market_open_gaps"], 0)
        self.assertEqual(manifest["holiday_evidence_count"], 1)
        self.assertEqual(manifest["payload_sha256"], hashlib.sha256(_canonical(payload)).hexdigest())
        self.assertEqual(manifest["gaps"][0]["reference"], GAP[2])

    def test_missing_or_mismatched_holiday_evidence_fails_closed(self):
        for evidence in ((), ((GAP[0], "2026-11-18T01:00:00Z", GAP[2]),)):
            with self.subTest(evidence=evidence), self.assertRaises(ResearchAttestationError):
                self.attest(envelope(), evidence=evidence)

    def test_extra_gap_fails_even_if_original_gap_ledger_is_updated(self):
        source = envelope()
        source["candles"].pop(2)
        source["gaps"] = _gaps(source["candles"], START, END)
        with self.assertRaises(ResearchAttestationError):
            self.attest(source)

    def test_changed_gap_ledger_fails(self):
        source = envelope()
        source["gaps"] = []
        with self.assertRaisesRegex(ResearchAttestationError, "ledger"):
            self.attest(source)

    def test_duplicate_source_bar_fails(self):
        source = envelope()
        source["candles"].insert(1, deepcopy(source["candles"][0]))
        with self.assertRaises(ResearchAttestationError):
            self.attest(source)

    def test_unverified_source_shape_fails(self):
        source = envelope()
        source["validation_status"] = "VERIFIED"
        with self.assertRaises(ResearchAttestationError):
            self.attest(source)


if __name__ == "__main__":
    unittest.main()
