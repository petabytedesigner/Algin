"""Category 30 read-only signal-candidate audit; no broker or network."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from analytics.historical_signal_diagnostic import (
    DiagnosticError, load_attested_export, scan_months,
)
from data.historical_research_attestation import (
    HOLIDAY_GAPS, ORIGINAL_SHA256, RESEARCH_START, RESEARCH_END,
)
from strategy.strategy import Strategy
from utils.time_tools import iso_utc, parse_utc


def fixture(n=120, start=None):
    origin = start or datetime(2025, 1, 31, 0, tzinfo=timezone.utc)
    records = []
    for i in range(n):
        t = origin + timedelta(minutes=i * 15)
        records.append({
            "time": iso_utc(t), "complete": True, "volume": 15,
            "mid": {"o": "1.10100", "h": "1.10140", "l": "1.10060", "c": "1.10110"},
            "bid": {"o": "1.10090", "h": "1.10130", "l": "1.10050", "c": "1.10100"},
            "ask": {"o": "1.10110", "h": "1.10150", "l": "1.10070", "c": "1.10120"},
        })
    return {
        "source": "OANDA_PRACTICE_V20_GET", "instrument": "EUR_USD",
        "granularity": "M15", "price": "MBA",
        "validation_status": "UNVERIFIED_NOT_BACKTEST_APPROVED",
        "gaps": [], "candles": records,
    }


class ReadOnlyAuditTests(unittest.TestCase):
    def test_attested_payload_requires_pinned_hash_and_matching_manifest(self):
        with TemporaryDirectory() as tmp:
            payload = fixture(
                start=datetime(2023, 1, 3, tzinfo=timezone.utc)
            )
            payload = {
                "instrument": "EUR_USD", "granularity": "M15",
                "candles": payload["candles"],
            }
            raw = json.dumps(payload).encode()
            digest = hashlib.sha256(raw).hexdigest()
            dataset = Path(tmp) / "prices.json"
            dataset.write_bytes(raw)
            manifest_path = Path(tmp) / "manifest.json"
            manifest = {
                "status": "RESEARCH_PRICE_COVERAGE_ATTESTED",
                "research_only": True, "not_broker_fill_verified": True,
                "no_prices_synthesized": True,
                "original_source_sha256": ORIGINAL_SHA256,
                "payload_sha256": digest,
                "original_candles": 218,
                "retained_candles": 120,
                "excluded_early_candles": 98,
                "research_from": RESEARCH_START,
                "research_to": RESEARCH_END,
                "holiday_evidence_count": len(HOLIDAY_GAPS),
                "remaining_unexplained_market_open_gaps": 0,
                "gaps": [],
            }
            manifest_path.write_text(json.dumps(manifest))
            with (patch("analytics.historical_signal_diagnostic.EXPECTED_PRICE_SHA256", digest),
                  patch("analytics.historical_signal_diagnostic.EXPECTED_RETAINED_BARS", 120),
                  patch("analytics.historical_signal_diagnostic.EXPECTED_SOURCE_BARS", 218)):
                result = load_attested_export(dataset, digest, manifest_path)
                self.assertEqual(len(result["candles"]), 120)
                with self.assertRaisesRegex(DiagnosticError, "checksum"):
                    load_attested_export(dataset, "0" * 64, manifest_path)
                with self.assertRaisesRegex(DiagnosticError, "unsafe"):
                    load_attested_export(dataset, digest, Path(tmp) / "absent.json")
                manifest["not_broker_fill_verified"] = False
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(DiagnosticError, "manifest"):
                    load_attested_export(dataset, digest, manifest_path)
                manifest["not_broker_fill_verified"] = True
                manifest_path.write_text(json.dumps(manifest))
                dataset.write_bytes(raw + b" ")
                with self.assertRaisesRegex(DiagnosticError, "checksum"):
                    load_attested_export(dataset, digest, manifest_path)

    def test_unpinned_price_export_rejected_even_with_matching_self_hash(self):
        with TemporaryDirectory() as tmp:
            raw = json.dumps(fixture()).encode()
            path = Path(tmp) / "history.json"
            path.write_bytes(raw)
            with self.assertRaisesRegex(DiagnosticError, "Incorrect frozen"):
                load_attested_export(path, hashlib.sha256(raw).hexdigest(),
                                     Path(tmp) / "absent.json")

    def test_months_are_checkpointed_separately_without_trades(self):
        sent = []
        rows = list(scan_months(fixture(), year=2025, emit=sent.append))
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows, sent)
        self.assertEqual(rows[0]["month"], "2025-01")
        self.assertEqual(rows[1]["month"], "2025-02")
        self.assertGreater(rows[0]["decision_bars"], 0)
        self.assertGreater(rows[1]["decision_bars"], 0)
        self.assertTrue(all(isinstance(row["candidate_details"], list) for row in rows))
        self.assertTrue(all(row["trades_executed"] == 0 for row in rows))
        self.assertTrue(all(not row["profit_calculated"] for row in rows))
        self.assertTrue(all(row["historical_news"] == "UNKNOWN_NOT_BYPASSED"
                            for row in rows))

    def test_signal_windows_do_not_include_future_bars(self):
        class GuardedStrategy(Strategy):
            def evaluate_signal(self, instrument, direction, history,
                                ema20, ema50, atr, zone_low, zone_high, timestamp):
                last = parse_utc(history[-1]["time"])
                assert timestamp - last == timedelta(minutes=15)
                assert all(parse_utc(bar["time"]) <= last for bar in history)
                return None

        with patch("analytics.historical_signal_diagnostic.Strategy", GuardedStrategy):
            result = list(scan_months(fixture(), year=2025, month=1))
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["schedule_eligible_bars"], 0)
        self.assertEqual(result[0].get("candidate_decisions", 0), 0)
        row = result[0]
        rejected = sum(v for k, v in row.items() if k.startswith("reject_"))
        self.assertEqual(rejected, 2 * row["schedule_eligible_bars"])

    def test_outside_supported_year_rejected(self):
        with self.assertRaisesRegex(DiagnosticError, "year"):
            list(scan_months(fixture(), year=2022))


if __name__ == "__main__":
    unittest.main()
