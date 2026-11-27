"""Offline tests: official historical BLS schedules are partial research evidence."""
import hashlib
from datetime import date
import unittest
from unittest.mock import patch

from analytics import historical_official_bls_probe as bls


def make_html(year, *, month="January", duplicates=False,
              timezone_line=True, bad_clock=False):
    rows = []
    month_number = 1 if month == "January" else 7
    for day in range(1, 14):
        clock = "25:00 PM" if bad_clock and day == 1 else "08:30 AM"
        title = ("Consumer Price Index for Test" if day == 1
                 else f"Other scheduled release {day}")
        weekday = date(year, month_number, day).strftime("%A")
        rows.append(f"<tr><td>{weekday}, {month} {day:02d}, {year}</td>"
                    f"<td>{clock}</td><td>{title}</td></tr>")
    if duplicates:
        rows.append(rows[0])
    rows.append(f"<tr><td>{date(year, month_number, 16).strftime('%A')}, {month} 16, {year}</td>"
                "<td></td><td>Federal holiday</td></tr>")
    zone = "All times on calendar are Eastern Time" if timezone_line else "Times unverified"
    return (f"<html><p>{zone}</p><table>" + "".join(rows) + "</table></html>").encode()


class OfficialBlsProbeTests(unittest.TestCase):
    def test_timezone_and_2023_high_profile_release(self):
        events, untimed = bls.parse_bls_year(make_html(2023), 2023)
        self.assertEqual(13, len(events))
        self.assertEqual(1, untimed)
        self.assertEqual("2023-01-01T13:30:00Z", events[0]["time_utc"])
        self.assertTrue(events[0]["algin_research_watchlist"])
        self.assertEqual("UNCLASSIFIED_BY_SOURCE", events[0]["importance"])

    def test_daylight_saving_is_not_assumed_utc(self):
        raw = make_html(2023, month="July")
        events, _ = bls.parse_bls_year(raw, 2023)
        self.assertEqual("2023-07-01T12:30:00Z", events[0]["time_utc"])

    def test_fail_closed_on_unverifiable_html(self):
        cases = (
            make_html(2023, timezone_line=False),
            make_html(2023, bad_clock=True),
            make_html(2023, duplicates=True),
            make_html(2023).replace(b"2023", b"2022"),
            b"<html>All times on calendar are Eastern Time</html>",
            b"",
        )
        for raw in cases:
            with self.subTest(raw=raw[:50]):
                with self.assertRaises(bls.SourceEvidenceError):
                    bls.parse_bls_year(raw, 2023)

    def test_only_frozen_years_supported(self):
        for year in (2022, 2027, "2023", True):
            with self.subTest(year=year):
                with self.assertRaises(bls.SourceEvidenceError):
                    bls.source_url(year)

    def test_report_can_never_attest_complete_calendar_or_trades(self):
        source = {"source_run_id": 45645175399, "monthly_archive_sha256": "frozen"}
        candidates = [{"decision_utc": "2023-01-01T09:00:00Z"}]
        windows = [{"decision_day_utc": "2023-01-01", "from": "2022-12-31", "to": "2023-01-02"}]
        with patch.object(bls, "build_windows", return_value=(source, candidates, windows)):
            report, snapshots = bls.make_report("unused", downloader=make_html)
        self.assertEqual(4, len(report["snapshots"]))
        self.assertEqual(2, report["windows"][0]["bls_usd_event_count"])
        self.assertFalse(report["historical_as_known_then_verified"])
        self.assertFalse(report["historical_eur_usd_news_completeness_attested"])
        self.assertFalse(report["net_pnl_backtest_permitted"])
        self.assertEqual("USD_BLS_ONLY", report["source_scope"])
        self.assertEqual(hashlib.sha256(snapshots[2023]).hexdigest(),
                         report["snapshots"][0]["snapshot_sha256"])


if __name__ == "__main__":
    unittest.main()
