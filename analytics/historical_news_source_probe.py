"""Category 30 Stage 4: bounded retrospective news SOURCE probe, never replay.

The frozen signal archive supplies dates. Provider responses collected years
later cannot prove the complete as-known-then calendar or authorize trades.
No broker access, orders, strategy changes or portfolio P/L are performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analytics.historical_evidence_gate import (
    MONTHS, _utc, inspect_signals,
)


SOURCE_URL = "https://financialmodelingprep.com/stable/economic-calendar"
SOURCE_ID = "FMP_STABLE_ECONOMIC_CALENDAR"
MAX_BYTES = 2_000_000
MAX_EVENTS = 5000
MAX_QUERIES = 16
MAJOR_CURRENCIES = frozenset(("EUR", "USD"))
IMPACTS = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


class NewsEvidenceError(ValueError):
    """Historic news evidence cannot be promoted from unverified inputs."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def build_windows(monthly_root):
    """Plan one bounded 3-day source query per unique candidate UTC date."""
    source = inspect_signals(monthly_root)
    candidates = []
    for month in MONTHS:
        obj = json.loads((Path(monthly_root) / (month + ".json")).read_bytes())
        for item in obj["candidate_details"]:
            stamp = _utc(item["decision_utc"])
            candidates.append({
                "setup_id": item["setup_id"],
                "decision_utc": item["decision_utc"],
                "currency_pair": "EUR_USD",
            })
    if len(candidates) != 16:
        raise NewsEvidenceError("Frozen signal archive changed")
    days = sorted({x["decision_utc"][:10] for x in candidates})
    windows = []
    for day in days:
        date = datetime.strptime(day, "%Y-%m-%d").date()
        windows.append({
            "decision_day_utc": day,
            "from": (date - timedelta(days=1)).isoformat(),
            "to": (date + timedelta(days=1)).isoformat(),
            "candidate_count": sum(x["decision_utc"][:10] == day for x in candidates),
        })
    if not 1 <= len(windows) <= MAX_QUERIES:
        raise NewsEvidenceError("Unbounded retrospective source-query plan")
    return source, candidates, windows


def validate_provider_json(raw, window):
    """Validate a query RESPONSE; it does not prove provider completeness."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise NewsEvidenceError("SOURCE_PAYLOAD_BYTES_INVALID")
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise NewsEvidenceError("SOURCE_JSON_INVALID") from exc
    if not isinstance(obj, list) or not 0 < len(obj) <= MAX_EVENTS:
        raise NewsEvidenceError("SOURCE_EVENTS_EMPTY_OR_UNBOUNDED")
    date_start = datetime.strptime(window["from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_end = datetime.strptime(window["to"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    if (date_end - date_start) != timedelta(days=3):
        raise NewsEvidenceError("SOURCE_WINDOW_NOT_THREE_DAYS")
    seen = set()
    relevant = []
    for record in obj:
        if not isinstance(record, dict):
            raise NewsEvidenceError("SOURCE_EVENT_NOT_OBJECT")
        when, currency, impact, title = (record.get("date"),
                                         record.get("currency"),
                                         record.get("impact"),
                                         record.get("event"))
        if not isinstance(when, str):
            raise NewsEvidenceError("SOURCE_EVENT_TIME_INVALID")
        try:
            stamp = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise NewsEvidenceError("SOURCE_EVENT_TIME_INVALID") from exc
        if (not date_start <= stamp < date_end
            or not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency, re.ASCII) is None
            or not isinstance(impact, str) or impact.casefold() not in IMPACTS
            or not isinstance(title, str) or not title.strip()
            or len(title) > 240):
            raise NewsEvidenceError("SOURCE_EVENT_FIELDS_INVALID")
        identity = (stamp, currency, title.strip())
        if identity in seen:
            raise NewsEvidenceError("SOURCE_EVENT_DUPLICATE")
        seen.add(identity)
        if currency in MAJOR_CURRENCIES:
            relevant.append({
                "time": stamp.isoformat().replace("+00:00", "Z"),
                "currency": currency, "impact": IMPACTS[impact.casefold()],
                "title": title.strip(),
            })
    return {
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        "all_currency_event_count": len(obj),
        "eur_usd_event_count": len(relevant),
        "eur_usd_events": sorted(relevant, key=lambda x: (x["time"], x["currency"], x["title"])),
        "provider_completeness_verified": False,
        "historical_as_known_then_verified": False,
    }


def _http_get(window, token):
    if (not isinstance(token, str) or not 1 <= len(token) <= 512
        or any(c.isspace() for c in token)):
        raise NewsEvidenceError("SOURCE_CREDENTIAL_UNAVAILABLE")
    query = urlencode({"from": window["from"], "to": window["to"], "apikey": token})
    req = Request(SOURCE_URL + "?" + query, headers={"Accept": "application/json"}, method="GET")
    try:
        with build_opener(NoRedirects).open(req, timeout=10) as response:
            if response.status != 200:
                raise NewsEvidenceError("SOURCE_HTTP_NON_200")
            content = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        # Never include the exception's URL: it contains the query API key.
        raise NewsEvidenceError("SOURCE_HTTP_ERROR_" + str(exc.code)) from None
    except (URLError, TimeoutError, OSError):
        raise NewsEvidenceError("SOURCE_NETWORK_UNAVAILABLE") from None
    return content


def build_report(monthly_root, *, probe=False, token="", getter=None,
                 probe_limit=None):
    """Fail closed on any unavailable query; never infer zero-news coverage."""
    source, candidates, windows = build_windows(monthly_root)
    if probe_limit is None:
        probe_limit = len(windows)
    if (type(probe_limit) is not int
        or not 1 <= probe_limit <= len(windows)):
        raise NewsEvidenceError("Invalid bounded source-query limit")
    status = "NOT_PROBED"
    results = []
    if probe:
        if not token:
            status = "BLOCKED_NO_FMP_API_KEY"
        else:
            fetch = _http_get if getter is None else getter
            for window in windows[:probe_limit]:
                try:
                    result = validate_provider_json(fetch(window, token), window)
                except NewsEvidenceError as exc:
                    results.append({"decision_day_utc": window["decision_day_utc"],
                                    "status": "UNVERIFIED", "error_code": str(exc)})
                    # A failed/limited provider query is not silently retried.
                    break
                results.append({"decision_day_utc": window["decision_day_utc"],
                                "status": "RETROSPECTIVE_SOURCE_SAMPLE_ONLY", **result})
            if (len(results) == probe_limit
                and all(x["status"] == "RETROSPECTIVE_SOURCE_SAMPLE_ONLY"
                        for x in results)):
                status = ("RETROSPECTIVE_SOURCE_SAMPLES_ONLY"
                          if probe_limit == len(windows)
                          else "PARTIAL_RETROSPECTIVE_SOURCE_SAMPLE")
            else:
                status = "BLOCKED_SOURCE_INCOMPLETE_OR_UNAVAILABLE"
    return {
        "schema": "CATEGORY30_STAGE4_HISTORIC_NEWS_SOURCE_PROBE_V1",
        "research_only": True,
        "source": SOURCE_ID,
        "signals_run": source["source_run_id"],
        "signals_archive_sha256": source["monthly_archive_sha256"],
        "candidate_count": len(candidates),
        "candidate_decisions": candidates,
        "windows": windows,
        "provider_probe_requested": bool(probe),
        "provider_query_limit": probe_limit,
        "provider_status": status,
        "provider_responses": results,
        "historical_news_completeness_attested": False,
        "as_known_then_events_attested": False,
        "historical_broker_tradeability_attested": False,
        "account_conversions_attested": False,
        "net_pnl_backtest_permitted": False,
        "blocking_reasons": [
            "RETROSPECTIVE_CALENDAR_RESPONSE_DOES_NOT_PROVE_COMPLETE_AS_KNOWN_THEN_DATA",
            "HISTORICAL_BROKER_TRADEABILITY_UNKNOWN",
            "HISTORICAL_ACCOUNT_CONVERSION_AND_COSTS_UNVERIFIED",
            "BROKER_FILL_AND_SLIPPAGE_UNVERIFIED",
            "HOLDOUT_SAMPLE_INSUFFICIENT",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--probe-fmp", action="store_true")
    parser.add_argument("--probe-limit", type=int)
    args = parser.parse_args(argv)
    report = build_report(args.signals_dir, probe=args.probe_fmp,
                          token=os.environ.get("FMP_API_KEY", ""),
                          probe_limit=args.probe_limit)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise NewsEvidenceError("Output already exists; evidence must be immutable")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    print("CATEGORY30_STAGE4_NEWS " + json.dumps({
        "candidates": report["candidate_count"],
        "windows": len(report["windows"]),
        "provider_status": report["provider_status"],
        "source_responses": len(report["provider_responses"]),
        "net_pnl_backtest_permitted": False,
    }, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (NewsEvidenceError, OSError, ValueError, TypeError) as exc:
        print("CATEGORY30_STAGE4_NEWS_FAILED: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
