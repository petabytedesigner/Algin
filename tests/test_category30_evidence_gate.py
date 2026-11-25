"""Fail-closed Category 30 historical evidence inventory; no external I/O."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from analytics import historical_evidence_gate as gate


def make_monthly(root):
    remaining = gate.YEAR_COUNTS.copy()
    for month in gate.MONTHS:
        year = int(month[:4])
        count = 1 if remaining[year] else 0
        remaining[year] -= count
        details = []
        if count:
            details = [{
                "decision_utc": month + "-15T10:15:00+00:00",
                "direction": "LONG" if year % 2 else "SHORT",
                "grade": "B", "score": 62.0,
                "setup_id": "algin-setup-" + (month.replace("-", "") + "0" * 40)[:40],
            }]
        (root / (month + ".json")).write_text(json.dumps({
            "month": month,
            "status": "RESEARCH_SIGNAL_CANDIDATES_ONLY",
            "dataset_sha256": gate.PRICE_SHA256,
            "historical_news": "UNKNOWN_NOT_BYPASSED",
            "historical_broker_tradeability": "UNKNOWN",
            "trades_executed": 0,
            "profit_calculated": False,
            "decision_bars": 100,
            "schedule_eligible_bars": 80,
            "candidate_decisions": count,
            "grade_B": count,
            "candidate_details": details,
        }))


class MonthlySignalEvidenceTests(unittest.TestCase):
    def test_accept_exact_frozen_inventory_and_holdout(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_monthly(root)
            signals = gate.inspect_signals(root)
            self.assertEqual(signals["candidate_decisions"], 16)
            self.assertEqual(signals["monthly_report_count"], 46)
            self.assertEqual(signals["unique_setup_ids"], 16)
            report = gate.build_report({"retained_bars": 95320}, signals)
            self.assertFalse(report["backtest_readiness"]["realistic_net_pnl_allowed"])
            self.assertEqual(report["frozen_holdout"]["reserved_candidates"], 7)
            self.assertFalse(report["backtest_readiness"]["profit_calculated"])

    def test_missing_month_or_altered_digest_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_monthly(root)
            (root / "2023-01.json").unlink()
            with self.assertRaisesRegex(gate.EvidenceGateError, "month"):
                gate.inspect_signals(root)
            make_monthly(root)
            path = root / "2023-01.json"
            payload = json.loads(path.read_text())
            payload["dataset_sha256"] = "0" * 64
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(gate.EvidenceGateError, "promoted"):
                gate.inspect_signals(root)

    def test_never_accept_fabricated_profit_or_news_coverage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_monthly(root)
            path = root / "2023-03.json"
            original = json.loads(path.read_text())
            for field, value in (
                ("trades_executed", 1),
                ("profit_calculated", True),
                ("historical_news", "CLEAR"),
                ("historical_broker_tradeability", True),
            ):
                modified = dict(original)
                modified[field] = value
                path.write_text(json.dumps(modified))
                with self.assertRaisesRegex(gate.EvidenceGateError, "promoted"):
                    gate.inspect_signals(root)

    def test_duplicate_candidate_or_wrong_month_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_monthly(root)
            path = root / "2024-02.json"
            row = json.loads(path.read_text())
            duplicate = json.loads((root / "2024-01.json").read_text())
            row["candidate_details"][0]["setup_id"] = duplicate["candidate_details"][0]["setup_id"]
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(gate.EvidenceGateError, "repeated"):
                gate.inspect_signals(root)
            row["candidate_details"][0]["setup_id"] = "algin-setup-unique"
            row["candidate_details"][0]["decision_utc"] = "2027-01-01T00:00:00+00:00"
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(gate.EvidenceGateError, "future"):
                gate.inspect_signals(root)


class AttestedPriceEvidenceTests(unittest.TestCase):
    def test_pinned_coverage_is_not_promoted_to_broker_fills(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars = [
                {"time": "2023-01-03T00:00:00Z"},
                {"time": "2023-01-03T00:15:00Z"},
            ]
            raw = json.dumps({"instrument": "EUR_USD", "granularity": "M15",
                              "candles": bars}).encode()
            digest = hashlib.sha256(raw).hexdigest()
            price_path = root / "price.json"
            price_path.write_bytes(raw)
            manifest = {
                "status": "RESEARCH_PRICE_COVERAGE_ATTESTED",
                "original_source_sha256": gate.ORIGINAL_SHA256,
                "payload_sha256": digest,
                "retained_candles": 2, "original_candles": 100,
                "excluded_early_candles": 98,
                "research_from": gate.PRICE_START,
                "research_to": gate.PRICE_END,
                "remaining_unexplained_market_open_gaps": 0,
                "holiday_evidence_count": 6,
                "research_only": True,
                "not_broker_fill_verified": True,
                "no_prices_synthesized": True,
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            with (patch.object(gate, "PRICE_SHA256", digest),
                  patch.object(gate, "EXPECTED_RETAINED_BARS", 2),
                  patch.object(gate, "EXPECTED_SOURCE_BARS", 100)):
                passed = gate.inspect_price(price_path, manifest_path)
                self.assertFalse(passed["broker_fill_verified"])
                self.assertEqual(passed["retained_bars"], 2)
                manifest["not_broker_fill_verified"] = False
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(gate.EvidenceGateError, "manifest"):
                    gate.inspect_price(price_path, manifest_path)
                manifest["not_broker_fill_verified"] = True
                manifest_path.write_text(json.dumps(manifest))
                price_path.write_bytes(raw + b" ")
                with self.assertRaisesRegex(gate.EvidenceGateError, "SHA-256"):
                    gate.inspect_price(price_path, manifest_path)


if __name__ == "__main__":
    unittest.main()
