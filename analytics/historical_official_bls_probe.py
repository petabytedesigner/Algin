"""Category 30: read-only BLS historical release schedule evidence (USD subset).

This is NOT a complete EUR/USD economic calendar, an as-known-then archive,
or authorization for retrospective trade/P&L claims. No credentials or broker
access are used. Each source snapshot is bounded and SHA-256 attested.
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
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from analytics.historical_news_source_probe import build_windows


YEARS = (2023, 2024, 2025, 2026)
MAX_HTML_BYTES = 4_000_000
BLS_TIMEZONE = ZoneInfo("America/New_York")
_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}$"
)
_TIME_RE = re.compile(r"(?:0?[1-9]|1[0-2]):[0-5]\d\s+[AP]M$")
_IMPORTANT = (
    "Consumer Price Index for ",
    "Employment Situation for ",
    "Producer Price Index for ",
)


class SourceEvidenceError(ValueError):
    """A source snapshot cannot be verified or used as historical evidence."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _TableRows(HTMLParser):
    """Extract cells from published HTML tables, without external packages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._cells = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cells = []
            self._cell = None
        elif tag in ("td", "th") and self._cells is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            self.rows.append(self._cells)
            self._cells = None
            self._cell = None


def source_url(year):
    if type(year) is not int or year not in YEARS:
        raise SourceEvidenceError("BLS_YEAR_OUTSIDE_FROZEN_RESEARCH_RANGE")
    return f"https://www.bls.gov/schedule/{year}/home.htm"


def download_year(year):
    req = Request(source_url(year), headers={
        "Accept": "text/html", "User-Agent": "AlginResearchEvidence/1.0",
    }, method="GET")
    try:
        with build_opener(_NoRedirects).open(req, timeout=15) as response:
            if response.status != 200:
                raise SourceEvidenceError("BLS_HTTP_NON_200")
            content_type = response.headers.get_content_type()
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise SourceEvidenceError("BLS_CONTENT_TYPE_UNEXPECTED")
            return response.read(MAX_HTML_BYTES + 1)
    except HTTPError as exc:
        raise SourceEvidenceError(f"BLS_HTTP_ERROR_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SourceEvidenceError("BLS_NETWORK_UNAVAILABLE") from None


def parse_bls_year(raw, year):
    """Normalize agency-scheduled release instants; do not infer market impact."""
    source_url(year)
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_HTML_BYTES:
        raise SourceEvidenceError("BLS_SOURCE_SIZE_INVALID")
    try:
        page = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceEvidenceError("BLS_SOURCE_ENCODING_INVALID") from exc
    if "All times on calendar are Eastern Time" not in page:
        raise SourceEvidenceError("BLS_TIMEZONE_STATEMENT_MISSING")
    parser = _TableRows()
    parser.feed(page)
    seen = set()
    events = []
    untimed = 0
    for cells in parser.rows:
        if len(cells) < 3 or not _DATE_RE.fullmatch(cells[0]):
            continue
        try:
            date = datetime.strptime(cells[0].replace(",", ""), "%A %B %d %Y")
        except ValueError as exc:
            raise SourceEvidenceError("BLS_RELEASE_DATE_INVALID") from exc
        if cells[0].split(",", 1)[0] != date.strftime("%A"):
            raise SourceEvidenceError("BLS_RELEASE_WEEKDAY_MISMATCH")
        if date.year != year:
            raise SourceEvidenceError("BLS_YEAR_MISMATCH")
        clock, title = cells[1], cells[2]
        if not clock:
            untimed += 1  # Holidays and un-timed listings are NOT evidence of CLEAR.
            continue
        if not _TIME_RE.fullmatch(clock) or not 1 <= len(title) <= 300:
            raise SourceEvidenceError("BLS_RELEASE_TIME_OR_TITLE_INVALID")
        local = datetime.strptime(clock, "%I:%M %p")
        stamp = date.replace(hour=local.hour, minute=local.minute, tzinfo=BLS_TIMEZONE)
        # Check calendar correctness across DST transitions, not host timezone.
        roundtrip = stamp.astimezone(timezone.utc).astimezone(BLS_TIMEZONE)
        if (roundtrip.date(), roundtrip.hour, roundtrip.minute) != (
            stamp.date(), stamp.hour, stamp.minute
        ):
            raise SourceEvidenceError("BLS_AMBIGUOUS_RELEASE_TIME")
        when = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        identity = (when, title)
        if identity in seen:
            raise SourceEvidenceError("BLS_DUPLICATE_RELEASE")
        seen.add(identity)
        events.append({
            "time_utc": when,
            "time_eastern": stamp.isoformat(),
            "currency": "USD",
            "title": title,
            "source": source_url(year),
            "importance": "UNCLASSIFIED_BY_SOURCE",
            "algin_research_watchlist": title.startswith(_IMPORTANT),
        })
    if len(events) < 12:
        raise SourceEvidenceError("BLS_YEAR_RELEASE_INVENTORY_INCOMPLETE")
    return sorted(events, key=lambda e: (e["time_utc"], e["title"])), untimed


def make_report(signals_dir, *, downloader=None):
    signal_source, candidates, windows = build_windows(signals_dir)
    getter = download_year if downloader is None else downloader
    yearly = []
    events = []
    raw_snapshots = {}
    for year in YEARS:
        raw = getter(year)
        parsed, untimed = parse_bls_year(raw, year)
        yearly.append({
            "year": year, "source": source_url(year),
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_bytes": len(raw), "timed_release_count": len(parsed),
            "untimed_rows_excluded": untimed,
        })
        raw_snapshots[year] = raw
        events.extend(parsed)
    matched = []
    for window in windows:
        start = datetime.fromisoformat(window["from"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(window["to"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        subset = [e for e in events if start <= datetime.fromisoformat(
            e["time_utc"].replace("Z", "+00:00")) < end]
        matched.append({
            "decision_day_utc": window["decision_day_utc"],
            "from": window["from"], "to": window["to"],
            "bls_usd_events": subset,
            "bls_usd_event_count": len(subset),
            "full_usd_or_eur_coverage": False,
        })
    return {
        "schema": "CATEGORY30_OFFICIAL_BLS_RESEARCH_PILOT_V1",
        "research_only": True,
        "source_id": "US_BLS_YEARLY_RELEASE_CALENDARS",
        "signal_run_id": signal_source["source_run_id"],
        "signal_archive_sha256": signal_source["monthly_archive_sha256"],
        "candidate_count": len(candidates),
        "window_count": len(windows),
        "snapshots": yearly,
        "source_scope": "USD_BLS_ONLY",
        "windows": matched,
        "source_importance_ratings_available": False,
        "historical_as_known_then_verified": False,
        "historical_eur_usd_news_completeness_attested": False,
        "historical_broker_tradeability_attested": False,
        "net_pnl_backtest_permitted": False,
        "blocking_reasons": [
            "BLS_IS_ONLY_PARTIAL_USD_SCHEDULE_NOT_EUR_USD_CALENDAR",
            "OTHER_USD_AND_EUR_SOURCES_NOT_YET_INGESTED",
            "REVISED_ARCHIVE_DOES_NOT_ATTEST_AS_KNOWN_THEN_SCHEDULE",
            "NO_SOURCE_IMPACT_RATINGS",
            "BROKER_FILL_AND_SLIPPAGE_UNVERIFIED",
        ],
    }, raw_snapshots


def _write_once(path, data):
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise SourceEvidenceError("EVIDENCE_OUTPUT_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--snapshots-dir", required=True)
    args = parser.parse_args(argv)
    report, snapshots = make_report(args.signals_dir)
    for year, raw in snapshots.items():
        _write_once(Path(args.snapshots_dir) / f"bls-{year}.html", raw)
    _write_once(args.output, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print("CATEGORY30_OFFICIAL_BLS " + json.dumps({
        "years": list(YEARS),
        "windows": report["window_count"],
        "bls_events_in_windows": sum(x["bls_usd_event_count"] for x in report["windows"]),
        "backtest_permitted": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (SourceEvidenceError, ValueError, OSError) as exc:
        print("CATEGORY30_OFFICIAL_BLS_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
