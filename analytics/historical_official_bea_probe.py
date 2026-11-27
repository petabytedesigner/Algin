"""Category 30 research: archive-backed BEA PCE and GDP release timestamps.

No API key, no market orders. Official BEA archive dates are cross-checked
against each news release's explicit embargo expiry (EST/EDT). Only source
pages within the frozen signal windows are downloaded, never inferred.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
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


BEA_ORIGIN = "https://www.bea.gov"
BEA_ZONE = ZoneInfo("America/New_York")
PRODUCTS = {"GDP": 456, "PCE": 716}
MAX_PAGES_PER_PRODUCT = 7
MAX_BYTES = 2_000_000
_RELEASE_PATH = re.compile(r"/(?:index\.php/)?news/(20\d{2})/[a-zA-Z0-9-]+$")
_STAMP = re.compile(
    r"EMBARGOED UNTIL RELEASE AT\s+(1[0-2]|[1-9]):([0-5]\d)\s+"
    r"(a\.m\.|p\.m\.)\s+(EST|EDT),?\s+"
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(20\d{2})",
    re.I,
)


class BeaEvidenceError(ValueError):
    """BEA archive or the scheduled economic release is unverified."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _ArchiveTable(HTMLParser):
    """Capture archived published dates alongside their release hyperlinks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.all_text = []
        self._row = None
        self._cell = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "links": []}
        elif tag == "a" and self._cell is not None:
            self._anchor = {"href": dict(attrs).get("href"), "text": []}

    def handle_data(self, data):
        self.all_text.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join("".join(self._anchor["text"]).split())
            self._cell["links"].append(self._anchor)
            self._anchor = None
        elif tag == "td" and self._cell is not None:
            self._cell["text"] = " ".join("".join(self._cell["text"]).split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def archive_url(product, page):
    if product not in PRODUCTS or type(page) is not int or not 0 <= page < MAX_PAGES_PER_PRODUCT:
        raise BeaEvidenceError("BEA_ARCHIVE_QUERY_NOT_APPROVED")
    return (f"{BEA_ORIGIN}/news/archive?field_related_product_target_id="
            f"{PRODUCTS[product]}&page={page}")


def _verified_release_url(url):
    parts = urlsplit(url)
    match = _RELEASE_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.hostname != "www.bea.gov"
        or parts.port is not None or parts.username is not None
        or parts.password is not None or parts.query or parts.fragment or not match):
        raise BeaEvidenceError("BEA_RELEASE_URL_UNAPPROVED")
    year = int(match.group(1))
    if not 2023 <= year <= 2026:
        raise BeaEvidenceError("BEA_RELEASE_OUTSIDE_HISTORICAL_RANGE")
    return year


def download(url):
    if url not in {archive_url(p, n) for p in PRODUCTS for n in range(MAX_PAGES_PER_PRODUCT)}:
        _verified_release_url(url)
    request = Request(url, headers={
        "Accept": "text/html", "User-Agent": "AlginResearchEvidence/1.0",
    }, method="GET")
    try:
        with build_opener(_NoRedirects).open(request, timeout=15) as response:
            if response.status != 200:
                raise BeaEvidenceError("BEA_HTTP_NON_200")
            if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                raise BeaEvidenceError("BEA_CONTENT_TYPE_UNEXPECTED")
            return response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise BeaEvidenceError(f"BEA_HTTP_ERROR_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise BeaEvidenceError("BEA_NETWORK_UNAVAILABLE") from None


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise BeaEvidenceError("BEA_SOURCE_SIZE_INVALID")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BeaEvidenceError("BEA_SOURCE_ENCODING_INVALID") from exc


def archive_rows(raw, *, product, page):
    parser = _ArchiveTable()
    parser.feed(_decode(raw))
    if "Archive" not in " ".join(parser.all_text):
        raise BeaEvidenceError("BEA_ARCHIVE_IDENTITY_MISSING")
    rows = []
    for cells in parser.rows:
        if len(cells) < 2 or not cells[0]["links"]:
            continue
        label = cells[0]["links"][0]["text"]
        if product == "PCE" and not label.startswith("Personal Income and Outlays"):
            continue
        if product == "GDP" and not (label.startswith("GDP ") or label.startswith("Gross Domestic Product")):
            continue
        try:
            published = datetime.strptime(cells[1]["text"], "%B %d, %Y").date()
        except ValueError as exc:
            raise BeaEvidenceError("BEA_INDEX_PUBLICATION_DATE_INVALID") from exc
        href = cells[0]["links"][0]["href"]
        if not href:
            raise BeaEvidenceError("BEA_INDEX_RELEASE_LINK_MISSING")
        link = urljoin(archive_url(product, page), href)
        if published.year in (2023, 2024, 2025, 2026):
            _verified_release_url(link)
        rows.append({"publication_date": published, "title": label, "url": link,
                     "product": product})
    if not 1 <= len(rows) <= 50:
        raise BeaEvidenceError("BEA_INDEX_RELEASE_INVENTORY_INVALID")
    return rows


def parse_release(raw, entry):
    _verified_release_url(entry["url"])
    page = _ArchiveTable()
    page.feed(_decode(raw))
    text = " ".join(" ".join(page.all_text).split())
    if entry["title"] not in text:
        raise BeaEvidenceError("BEA_RELEASE_TITLE_MISMATCH")
    matches = list(_STAMP.finditer(text))
    if len(matches) != 1:
        raise BeaEvidenceError("BEA_RELEASE_EMBARGO_TIME_UNVERIFIED")
    hh, mm, ampm, zone, weekday, month, day, year = matches[0].groups()
    stamp_date = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date()
    if stamp_date != entry["publication_date"] or stamp_date.strftime("%A").lower() != weekday.lower():
        raise BeaEvidenceError("BEA_RELEASE_DATE_ARCHIVE_MISMATCH")
    hour = int(hh) % 12 + (12 if ampm.lower() == "p.m." else 0)
    local = datetime(stamp_date.year, stamp_date.month, stamp_date.day,
                     hour, int(mm), tzinfo=BEA_ZONE)
    if local.tzname() != zone.upper():
        raise BeaEvidenceError("BEA_RELEASE_DST_MISMATCH")
    return {
        "time_utc": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "time_eastern": local.isoformat(),
        "currency": "USD", "title": entry["title"],
        "source": entry["url"],
        "source_product": entry["product"],
        "importance": "UNCLASSIFIED_BY_SOURCE",
    }


def make_report(signals_dir, *, getter=None):
    source, candidates, windows = build_windows(signals_dir)
    fetch = download if getter is None else getter
    selected_days = set()
    for window in windows:
        start = date.fromisoformat(window["from"])
        selected_days.update(start + timedelta(days=i) for i in range(3))
    snapshots = {}
    yearly_release_indexes = []
    entries = {}
    for product in PRODUCTS:
        found_older = False
        for page in range(MAX_PAGES_PER_PRODUCT):
            url = archive_url(product, page)
            raw = fetch(url)
            filename = f"bea-index-{product}-page{page}.html"
            snapshots[filename] = raw
            rows = archive_rows(raw, product=product, page=page)
            yearly_release_indexes.append({
                "product": product, "page": page, "source": url,
                "sha256": hashlib.sha256(raw).hexdigest(), "row_count": len(rows),
            })
            for entry in rows:
                if entry["publication_date"].year < 2023:
                    found_older = True
                    continue
                if entry["publication_date"].year > 2026:
                    continue
                if entry["url"] in entries:
                    raise BeaEvidenceError("BEA_DUPLICATE_ARCHIVE_LINK")
                entries[entry["url"]] = entry
            # Archive pages are reverse chronological. A page containing only
            # pre-2023 rows terminates the bounded history scan.
            if rows and all(x["publication_date"].year < 2023 for x in rows):
                found_older = True
                break
        if not found_older:
            raise BeaEvidenceError("BEA_ARCHIVE_PAGINATION_INCOMPLETE")
        product_entries = [x for x in entries.values() if x["product"] == product]
        if (len(product_entries) < 30
            or any(sum(x["publication_date"].year == year
                       for x in product_entries) < 4
                   for year in (2023, 2024, 2025, 2026))):
            raise BeaEvidenceError("BEA_ARCHIVE_HISTORY_INSUFFICIENT")
    events = []
    source_sha = {}
    for entry in sorted(entries.values(), key=lambda x: (x["publication_date"], x["url"])):
        if entry["publication_date"] not in selected_days:
            continue
        raw = fetch(entry["url"])
        event = parse_release(raw, entry)
        filename = "bea-release-" + hashlib.sha256(entry["url"].encode()).hexdigest()[:20] + ".html"
        snapshots[filename] = raw
        source_sha[entry["url"]] = hashlib.sha256(raw).hexdigest()
        events.append(event)
    matched = []
    for window in windows:
        start = datetime.fromisoformat(window["from"]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(window["to"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        subset = [e for e in events if start <= datetime.fromisoformat(
            e["time_utc"].replace("Z", "+00:00")) < end]
        matched.append({"decision_day_utc": window["decision_day_utc"],
                        "bea_events": subset, "bea_event_count": len(subset),
                        "full_usd_or_eur_coverage": False})
    return {
        "schema": "CATEGORY30_OFFICIAL_BEA_RESEARCH_PILOT_V1",
        "research_only": True,
        "source_scope": "USD_BEA_PCE_GDP_ONLY",
        "signals_run_id": source["source_run_id"],
        "signals_archive_sha256": source["monthly_archive_sha256"],
        "candidate_count": len(candidates), "window_count": len(windows),
        "archive_index_sources": yearly_release_indexes,
        "archive_release_inventory": {
            p: len([x for x in entries.values() if x["product"] == p])
            for p in PRODUCTS},
        "sample_release_sha256": source_sha,
        "windows": matched,
        "historical_as_known_then_verified": False,
        "historical_eur_usd_news_completeness_attested": False,
        "net_pnl_backtest_permitted": False,
        "blocking_reasons": [
            "BEA_PCE_GDP_ONLY_SUBSET_OF_USD_ECONOMIC_NEWS",
            "EURO_AND_OTHER_USD_DATA_NOT_YET_ATTESTED",
            "NO_COMPLETE_AS_KNOWN_THEN_NEWS_COVERAGE",
            "NO_SOURCE_IMPACT_RATINGS",
            "BROKER_FILL_AND_COSTS_UNVERIFIED",
        ],
    }, snapshots


def _write_once(path, raw):
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise BeaEvidenceError("BEA_OUTPUT_ALREADY_EXISTS")
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
    print("CATEGORY30_OFFICIAL_BEA " + json.dumps({
        "archive_releases": report["archive_release_inventory"],
        "sampled": len(report["sample_release_sha256"]),
        "events_in_windows": sum(x["bea_event_count"] for x in report["windows"]),
        "backtest_permitted": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (BeaEvidenceError, ValueError, OSError) as exc:
        print("CATEGORY30_OFFICIAL_BEA_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
