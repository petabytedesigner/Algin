"""BEA official archive/provenance tests without network or secrets."""
from datetime import date, datetime
import hashlib
import unittest
from unittest.mock import patch

from analytics import historical_official_bea_probe as bea


def release_url(year, product, month, day):
    return (f"https://www.bea.gov/news/{year}/"
            f"{product.lower()}-release-{month:02d}-{day:02d}")


def fake_release(entry, *, bad_zone=False, bad_date=False):
    published = entry["publication_date"]
    title = entry["title"]
    date_line = published.strftime("%A, %B %d, %Y")
    if bad_date:
        date_line = "Monday, January 01, 2020"
    zone = datetime(published.year, published.month, published.day,
                    tzinfo=bea.BEA_ZONE).tzname()
    if bad_zone:
        zone = "EDT" if zone == "EST" else "EST"
    return (f"<html><h1>{title}</h1><p>EMBARGOED UNTIL RELEASE AT "
            f"8:30 a.m. {zone}, {date_line}</p></html>").encode()


def fake_index(product, page):
    if page == 0:
        months = [(2026, 5)] * 10 + [(2025, 8)] * 10
    elif page == 1:
        months = [(2024, 4)] * 20
    elif page == 2:
        months = [(2023, 9)] * 20
    else:
        months = [(2022, 6)] * 5
    rows = []
    for index, (year, month) in enumerate(months, start=1):
        published = date(year, month, index if index <= 20 else 20)
        label = (f"Personal Income and Outlays, Period {index}" if product == "PCE"
                 else f"GDP (Advance Estimate), Period {index}")
        url = release_url(year, product, month, index)
        rows.append(f'<tr><td><a href="{url}">{label}</a></td>'
                    f'<td>{published.strftime("%B %d, %Y")}</td></tr>')
    return ("<html><h1>Archive</h1><table>" + "".join(rows) + "</table></html>").encode()


def fake_get(url):
    if "/news/archive?" in url:
        product = "GDP" if "=456" in url else "PCE"
        page = int(url.rsplit("page=", 1)[1])
        return fake_index(product, page)
    product = "PCE" if "/pce-" in url else "GDP"
    filename = url.rsplit("/", 1)[1]
    _, _, month, day = filename.split("-")
    published = date(int(url.split("/news/")[1][:4]), int(month), int(day))
    title = (f"Personal Income and Outlays, Period {int(day)}" if product == "PCE"
             else f"GDP (Advance Estimate), Period {int(day)}")
    return fake_release({"title": title, "publication_date": published})


class OfficialBeaTests(unittest.TestCase):
    def test_archived_release_utc_dst(self):
        for d, expected in ((date(2023, 2, 1), "2023-02-01T13:30:00Z"),
                            (date(2023, 7, 1), "2023-07-01T12:30:00Z")):
            entry = {"title": "Personal Income and Outlays, Test",
                     "product": "PCE", "publication_date": d,
                     "url": release_url(d.year, "PCE", d.month, d.day)}
            event = bea.parse_release(fake_release(entry), entry)
            self.assertEqual(expected, event["time_utc"])
            self.assertEqual("UNCLASSIFIED_BY_SOURCE", event["importance"])

    def test_fail_closed_on_malformed_embargo_or_mismatched_date(self):
        entry = {"title": "Personal Income and Outlays, Test", "product": "PCE",
                 "publication_date": date(2023, 2, 1),
                 "url": release_url(2023, "PCE", 2, 1)}
        cases = (fake_release(entry, bad_zone=True), fake_release(entry, bad_date=True),
                 fake_release(entry).replace(b"EMBARGOED UNTIL RELEASE AT", b"Unknown"),
                 fake_release(entry).replace(b"Personal Income and Outlays, Test", b"Unrelated"),
                 b"")
        for raw in cases:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(bea.BeaEvidenceError):
                    bea.parse_release(raw, entry)

    def test_index_dates_and_source_pinning(self):
        rows = bea.archive_rows(fake_index("PCE", 0), product="PCE", page=0)
        self.assertEqual(20, len(rows))
        self.assertEqual("PCE", rows[0]["product"])
        self.assertEqual(date(2026, 5, 1), rows[0]["publication_date"])
        for url in ("https://evil.example/news/2023/pce-1",
                    "https://www.bea.gov/news/2023/pce-1?apikey=a",
                    "https://www.bea.gov/news/2022/pce-1"):
            with self.subTest(url=url):
                with self.assertRaises(bea.BeaEvidenceError):
                    bea._verified_release_url(url)

    def test_report_remains_partial_and_sha_pinned(self):
        source = {"source_run_id": 45645175399, "monthly_archive_sha256": "frozen"}
        candidates = [{"decision_utc": "2023-09-05T12:31:00Z"}]
        windows = [{"decision_day_utc": "2023-09-05", "from": "2023-09-04", "to": "2023-09-06"}]
        with patch.object(bea, "build_windows", return_value=(source, candidates, windows)):
            report, snapshots = bea.make_report("unused", getter=fake_get)
        self.assertEqual(6, len(report["sample_release_sha256"]))
        self.assertEqual(6, report["windows"][0]["bea_event_count"])
        self.assertFalse(report["historical_eur_usd_news_completeness_attested"])
        self.assertFalse(report["net_pnl_backtest_permitted"])
        self.assertEqual(hashlib.sha256(snapshots["bea-index-PCE-page0.html"]).hexdigest(),
                         next(x["sha256"] for x in report["archive_index_sources"]
                              if x["product"] == "PCE" and x["page"] == 0))


if __name__ == "__main__":
    unittest.main()
