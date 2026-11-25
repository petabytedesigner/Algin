"""Fail-closed tests for Category 30 Stage 4 retrospective source probing."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analytics import historical_news_source_probe as source


class HistoricNewsSourceTests(unittest.TestCase):
    def setUp(self):
        self.window = {"decision_day_utc": "2026-10-15", "from": "2026-10-14",
                       "to": "2026-10-16", "candidate_count": 1}

    @staticmethod
    def _sample(**fields):
        row = {"date": "2026-10-15 12:30:00", "currency": "USD",
               "impact": "High", "event": "Inflation release"}
        row.update(fields)
        return json.dumps([row], sort_keys=True).encode()

    def test_provider_utc_events_are_samples_not_proven_calendar(self):
        result = source.validate_provider_json(self._sample(), self.window)
        self.assertEqual(result["all_currency_event_count"], 1)
        self.assertEqual(result["eur_usd_event_count"], 1)
        self.assertEqual(result["eur_usd_events"][0]["impact"], "HIGH")
        self.assertFalse(result["historical_as_known_then_verified"])
        self.assertFalse(result["provider_completeness_verified"])

    def test_absent_malformed_out_of_range_or_duplicates_fail_closed(self):
        bad_payloads = [b"[]", b"{}", b"not JSON",
                        self._sample(currency="US"),
                        self._sample(date="2026-10-18 00:00:00"),
                        self._sample(impact="Unknown")]
        sample = json.loads(self._sample())
        bad_payloads.append(json.dumps(sample + sample).encode())
        for value in bad_payloads:
            with self.subTest(value=value[:50]):
                with self.assertRaises(source.NewsEvidenceError):
                    source.validate_provider_json(value, self.window)

    def test_rejects_incomplete_or_wide_windows(self):
        wide = dict(self.window, **{"to": "2026-10-17"})
        with self.assertRaises(source.NewsEvidenceError):
            source.validate_provider_json(self._sample(), wide)

    def test_source_plan_uses_exact_frozen_candidate_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for month, day in (("2025-11", "2025-11-12"),
                               ("2026-02", "2026-02-09")):
                data = {"candidate_details": [{
                    "setup_id": f"algin-setup-{month}-{i}",
                    "decision_utc": day + "T14:15:00Z",
                    "direction": "LONG", "grade": "B",
                } for i in range(8)]}
                (root / (month + ".json")).write_text(json.dumps(data))
            with patch.object(source, "MONTHS", ("2025-11", "2026-02")), \
                 patch.object(source, "inspect_signals", return_value={
                     "source_run_id": 45645175399,
                     "monthly_archive_sha256": "pinned-test-digest",
                 }):
                inventory, candidates, windows = source.build_windows(root)
        self.assertEqual(len(candidates), 16)
        self.assertEqual(len(windows), 2)
        self.assertEqual([w["candidate_count"] for w in windows], [8, 8])
        self.assertEqual(windows[0]["from"], "2025-11-11")
        self.assertEqual(windows[1]["to"], "2026-02-10")

    def test_no_credential_makes_no_requests_and_stays_blocked(self):
        with patch.object(source, "build_windows", return_value=(
            {"source_run_id": 45645175399, "monthly_archive_sha256": "digest"},
            [{"setup_id": "A"}], [self.window],
        )):
            report = source.build_report("unused", probe=True, token="",
                                         getter=lambda *_: self.fail("GET called"))
        self.assertEqual(report["provider_status"], "BLOCKED_NO_FMP_API_KEY")
        self.assertFalse(report["net_pnl_backtest_permitted"])
        self.assertEqual(report["provider_responses"], [])

    def test_retrospective_response_never_unlocks_backtest_or_leaks_key(self):
        with patch.object(source, "build_windows", return_value=(
            {"source_run_id": 45645175399, "monthly_archive_sha256": "digest"},
            [{"setup_id": "A"}], [self.window],
        )):
            report = source.build_report("unused", probe=True,
                                         token="SENSITIVE_TEST_CREDENTIAL",
                                         getter=lambda *_: self._sample())
        self.assertEqual(report["provider_status"],
                         "RETROSPECTIVE_SOURCE_SAMPLES_ONLY")
        self.assertFalse(report["historical_news_completeness_attested"])
        self.assertFalse(report["net_pnl_backtest_permitted"])
        self.assertNotIn("SENSITIVE_TEST_CREDENTIAL", json.dumps(report))

    def test_partial_source_failure_is_recorded_without_blind_retry(self):
        other = dict(self.window, decision_day_utc="2026-10-16",
                     **{"from": "2026-10-15", "to": "2026-10-17"})
        count = []
        def get(window, token):
            count.append(window["decision_day_utc"])
            if len(count) == 1:
                raise source.NewsEvidenceError("SOURCE_HTTP_ERROR_403")
            return self._sample()
        with patch.object(source, "build_windows", return_value=(
            {"source_run_id": 45645175399, "monthly_archive_sha256": "digest"},
            [{"setup_id": "A"}], [self.window, other],
        )):
            report = source.build_report("unused", probe=True, token="TOKEN",
                                         getter=get)
        self.assertEqual(len(count), 1)
        self.assertEqual(report["provider_status"],
                         "BLOCKED_SOURCE_INCOMPLETE_OR_UNAVAILABLE")
        self.assertEqual(report["provider_responses"][0]["error_code"],
                         "SOURCE_HTTP_ERROR_403")


if __name__ == "__main__":
    unittest.main()
