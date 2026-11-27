"""Offline regression of official Fed FOMC release sources and UTC offsets."""
from datetime import datetime
import hashlib
import unittest
from unittest.mock import patch

from analytics import historical_official_fomc_probe as fomc


def statement_url(year, month, day):
    return ("https://www.federalreserve.gov/newsevents/pressreleases/"
            f"monetary{year:04d}{month:02d}{day:02d}a.htm")


def fake_statement(year, month, day, *, abbreviation=None, valid=True):
    stamp = datetime(year, month, day, 14, 0, tzinfo=fomc.FED_ZONE)
    zone = abbreviation if abbreviation else stamp.tzname()
    label = "Federal Reserve issues FOMC statement" if valid else "Unrelated release"
    return (f"<html><h1>{label}</h1><p>{stamp.strftime('%B %d, %Y')}</p>"
            f"<div>For release at 2:00 p.m. {zone}</div></html>").encode()


def fake_index(year, *, duplicate=False, short=False):
    months = range(1, 3 if short else 7)
    links = [f'<a href="{statement_url(year, m, 1)}">'
             'Federal Reserve issues FOMC statement</a>' for m in months]
    if duplicate:
        links.append(links[0])
    return (f"<html><h2>{year} FOMC Press Releases</h2>"
            + "".join(links) + "</html>").encode()


def fake_fetch(url):
    if url.endswith("-press-fomc.htm"):
        year = int(url.rsplit("/", 1)[1][:4])
        return fake_index(year)
    day = fomc._verified_statement_url(url)
    return fake_statement(day.year, day.month, day.day)


class OfficialFomcTests(unittest.TestCase):
    def test_est_edt_times_from_actual_press_release_text(self):
        winter = statement_url(2023, 2, 1)
        summer = statement_url(2023, 7, 1)
        a = fomc.parse_statement(fake_statement(2023, 2, 1), winter)
        b = fomc.parse_statement(fake_statement(2023, 7, 1), summer)
        self.assertEqual("2023-02-01T19:00:00Z", a["time_utc"])
        self.assertEqual("2023-07-01T18:00:00Z", b["time_utc"])
        self.assertEqual("UNCLASSIFIED_BY_SOURCE", a["importance"])

    def test_index_requires_unambiguous_list_of_official_statements(self):
        self.assertEqual(6, len(fomc.statement_links(fake_index(2023), 2023)))
        for raw in (fake_index(2023, duplicate=True), fake_index(2023, short=True),
                    fake_index(2023).replace(b"2023 FOMC", b"Unverified")):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(fomc.FedEvidenceError):
                    fomc.statement_links(raw, 2023)

    def test_statement_fails_closed_on_wrong_identity_clock_and_dst(self):
        url = statement_url(2023, 2, 1)
        cases = (fake_statement(2023, 2, 1, abbreviation="EDT"),
                 fake_statement(2023, 2, 1, valid=False),
                 fake_statement(2023, 2, 2),
                 fake_statement(2023, 2, 1).replace(b"2:00 p.m.", b"Soon"),
                 b"")
        for raw in cases:
            with self.subTest(raw=raw[:55]):
                with self.assertRaises(fomc.FedEvidenceError):
                    fomc.parse_statement(raw, url)

    def test_url_host_query_and_year_are_pinned(self):
        for value in (
            statement_url(2023, 1, 1).replace("www.federalreserve.gov", "evil.example"),
            statement_url(2023, 1, 1) + "?ref=1",
            statement_url(2023, 1, 1).replace("2023", "2027"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(fomc.FedEvidenceError):
                    fomc._verified_statement_url(value)

    def test_report_is_research_only_and_shas_attested(self):
        source = {"source_run_id": 45645175399, "monthly_archive_sha256": "frozen"}
        candidates = [{"decision_utc": "2023-01-01T19:01:00Z"}]
        windows = [{"decision_day_utc": "2023-01-01", "from": "2022-12-31", "to": "2023-01-02"}]
        with patch.object(fomc, "build_windows", return_value=(source, candidates, windows)):
            report, snapshots = fomc.make_report("unused", getter=fake_fetch)
        self.assertEqual(24, sum(y["statement_count"] for y in report["annual_sources"]))
        self.assertEqual(1, report["windows"][0]["fomc_statement_count"])
        self.assertFalse(report["historical_eur_usd_news_completeness_attested"])
        self.assertFalse(report["net_pnl_backtest_permitted"])
        self.assertEqual(hashlib.sha256(snapshots["fomc-2023-index.html"]).hexdigest(),
                         report["annual_sources"][0]["index_sha256"])


if __name__ == "__main__":
    unittest.main()
