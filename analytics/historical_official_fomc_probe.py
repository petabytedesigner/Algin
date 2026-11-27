"""Category 30 read-only FOMC statement-time provenance, USD subset only.

The Fed's annual press-release indexes link to each actual FOMC statement.
The release date and explicit EST/EDT time are verified on each statement page.
This is research evidence, never a complete historical calendar or P/L gate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from analytics.historical_news_source_probe import build_windows


YEARS = (2023, 2024, 2025, 2026)
FED_ORIGIN = "https://www.federalreserve.gov"
FED_ZONE = ZoneInfo("America/New_York")
MAX_BYTES = 1_000_000
_STATEMENT_PATH = re.compile(r"/newsevents/pressreleases/monetary(20\d{6})a\.htm$")
_RELEASE = re.compile(r"For release at\s+(1[0-2]|[1-9]):([0-5]\d)\s+(a\.m\.|p\.m\.)\s+(EST|EDT)", re.I)


class FedEvidenceError(ValueError):
    """Unverified official FOMC source, never treated as absence of news."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _AnchorsAndText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.text = []
        self._href = None
        self._label = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []

    def handle_data(self, data):
        self.text.append(data)
        if self._label is not None:
            self._label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._label is not None:
            self.anchors.append((self._href, " ".join("".join(self._label).split())))
            self._href = None
            self._label = None


def index_url(year):
    if type(year) is not int or year not in YEARS:
        raise FedEvidenceError("FOMC_YEAR_OUTSIDE_RESEARCH_RANGE")
    return f"{FED_ORIGIN}/newsevents/pressreleases/{year}-press-fomc.htm"


def _verified_statement_url(url, year=None):
    parsed = urlsplit(url)
    matched = _STATEMENT_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "www.federalreserve.gov"
        or parsed.port is not None or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
        or not matched):
        raise FedEvidenceError("FOMC_UNAPPROVED_STATEMENT_URL")
    day = datetime.strptime(matched.group(1), "%Y%m%d").date()
    if day.year not in YEARS or (year is not None and day.year != year):
        raise FedEvidenceError("FOMC_STATEMENT_YEAR_MISMATCH")
    return day


def download(url):
    if url not in tuple(index_url(y) for y in YEARS):
        _verified_statement_url(url)
    req = Request(url, headers={
        "Accept": "text/html", "User-Agent": "AlginResearchEvidence/1.0",
    }, method="GET")
    try:
        with build_opener(_NoRedirects).open(req, timeout=15) as response:
            if response.status != 200:
                raise FedEvidenceError("FOMC_HTTP_NON_200")
            if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                raise FedEvidenceError("FOMC_CONTENT_TYPE_UNEXPECTED")
            return response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise FedEvidenceError(f"FOMC_HTTP_ERROR_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise FedEvidenceError("FOMC_NETWORK_UNAVAILABLE") from None


def _parse_page(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise FedEvidenceError("FOMC_SOURCE_SIZE_INVALID")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FedEvidenceError("FOMC_ENCODING_INVALID") from exc
    parser = _AnchorsAndText()
    parser.feed(decoded)
    return parser, " ".join(" ".join(parser.text).split())


def statement_links(raw, year):
    parser, text = _parse_page(raw)
    if f"{year} FOMC Press Releases" not in text:
        raise FedEvidenceError("FOMC_INDEX_IDENTITY_MISSING")
    links = []
    seen = set()
    for href, label in parser.anchors:
        if "Federal Reserve issues FOMC statement" not in label:
            continue
        if not href:
            raise FedEvidenceError("FOMC_STATEMENT_LINK_MISSING")
        resolved = urljoin(index_url(year), href)
        _verified_statement_url(resolved, year)
        if resolved in seen:
            raise FedEvidenceError("FOMC_INDEX_DUPLICATE")
        seen.add(resolved)
        links.append(resolved)
    if not 6 <= len(links) <= 12:
        raise FedEvidenceError("FOMC_INDEX_INVENTORY_UNVERIFIED")
    return sorted(links)


def parse_statement(raw, url):
    day = _verified_statement_url(url)
    _, text = _parse_page(raw)
    if "Federal Reserve issues FOMC statement" not in text:
        raise FedEvidenceError("FOMC_STATEMENT_IDENTITY_MISSING")
    date_text = day.strftime("%B %d, %Y")
    # Some official HTML pages use unpadded day numbers.
    if date_text not in text and date_text.replace(" 0", " ") not in text:
        raise FedEvidenceError("FOMC_STATEMENT_RELEASE_DATE_MISMATCH")
    stamps = list(_RELEASE.finditer(text))
    if len(stamps) != 1:
        raise FedEvidenceError("FOMC_RELEASE_CLOCK_UNVERIFIED")
    hour, minute, ampm, abbreviation = stamps[0].groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "p.m." else 0)
    local = datetime(day.year, day.month, day.day, hour, int(minute), tzinfo=FED_ZONE)
    if local.tzname() != abbreviation.upper():
        raise FedEvidenceError("FOMC_RELEASE_DST_ABBREVIATION_MISMATCH")
    return {
        "time_utc": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "time_eastern": local.isoformat(),
        "currency": "USD",
        "title": "Federal Reserve issues FOMC statement",
        "source": url,
        "importance": "UNCLASSIFIED_BY_SOURCE",
        "agency": "FED_FOMC",
    }


def make_report(signals_dir, *, getter=None):
    source, candidates, windows = build_windows(signals_dir)
    fetch = download if getter is None else getter
    snapshots = {}
    years = []
    events = []
    for year in YEARS:
        iurl = index_url(year)
        index_raw = fetch(iurl)
        links = statement_links(index_raw, year)
        snapshots[f"fomc-{year}-index.html"] = index_raw
        year_events = []
        for link in links:
            raw = fetch(link)
            event = parse_statement(raw, link)
            key = link.rsplit("/", 1)[1]
            snapshots[key] = raw
            year_events.append(event)
        years.append({
            "year": year, "index_url": iurl,
            "index_sha256": hashlib.sha256(index_raw).hexdigest(),
            "statement_count": len(year_events),
            "statement_sha256": {e["source"]: hashlib.sha256(
                snapshots[e["source"].rsplit("/", 1)[1]]).hexdigest()
                for e in year_events},
        })
        events.extend(year_events)
    if len({e["time_utc"] for e in events}) != len(events):
        raise FedEvidenceError("FOMC_DUPLICATE_RELEASE_IN_YEARS")
    matched = []
    for window in windows:
        start = datetime.fromisoformat(window["from"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(window["to"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        subset = [e for e in events if start <= datetime.fromisoformat(
            e["time_utc"].replace("Z", "+00:00")) < end]
        matched.append({
            "decision_day_utc": window["decision_day_utc"],
            "fomc_statements": subset,
            "fomc_statement_count": len(subset),
            "full_usd_or_eur_coverage": False,
        })
    return {
        "schema": "CATEGORY30_OFFICIAL_FOMC_RESEARCH_PILOT_V1",
        "research_only": True,
        "source_scope": "USD_FED_FOMC_STATEMENTS_ONLY",
        "signals_run_id": source["source_run_id"],
        "signals_archive_sha256": source["monthly_archive_sha256"],
        "candidate_count": len(candidates),
        "window_count": len(windows),
        "annual_sources": years,
        "windows": matched,
        "historical_as_known_then_verified": False,
        "historical_eur_usd_news_completeness_attested": False,
        "net_pnl_backtest_permitted": False,
        "blocking_reasons": [
            "FED_FOMC_ONLY_SUBSET_OF_USD_ECONOMIC_EVENTS",
            "ECB_EUROSTAT_BEA_AND_OTHER_RELEASES_MISSING",
            "NO_COMPLETE_AS_KNOWN_THEN_NEWS_COVERAGE",
            "NO_SOURCE_IMPACT_RATINGS",
            "BROKER_FILL_AND_COSTS_NOT_VERIFIED",
        ],
    }, snapshots


def _write_once(path, raw):
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FedEvidenceError("FOMC_OUTPUT_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--snapshots-dir", required=True)
    args = parser.parse_args(argv)
    report, snapshots = make_report(args.signals_dir)
    for name, raw in snapshots.items():
        _write_once(Path(args.snapshots_dir) / name, raw)
    _write_once(args.output, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    print("CATEGORY30_OFFICIAL_FOMC " + json.dumps({
        "statements": sum(y["statement_count"] for y in report["annual_sources"]),
        "windows": report["window_count"],
        "statements_in_windows": sum(x["fomc_statement_count"] for x in report["windows"]),
        "backtest_permitted": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (FedEvidenceError, ValueError, OSError) as exc:
        print("CATEGORY30_OFFICIAL_FOMC_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
