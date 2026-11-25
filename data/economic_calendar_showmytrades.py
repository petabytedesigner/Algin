"""Read-only ShowMyTrades live economic calendar adapter.

This is an optional source for prospective news lockouts, not a historical
calendar for backtesting. The returned shape preserves NewsFilter's existing
five-field coverage contract. Source completeness is not independently proven:
the covered currencies are inferred from an exact, pinned filter request.
Any ambiguous response must fail closed rather than imply no HIGH news.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import requests

from utils.time_tools import iso_utc, parse_utc, require_utc


SHOWMYTRADES_ENDPOINT = "https://showmytrades.com/api/public/calendar/v1"
SUPPORTED_CURRENCIES = ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "USD")
_PARAMS = {"currency": ",".join(SUPPORTED_CURRENCIES), "impact": "high", "days": 1}
_MAX_BYTES = 1024 * 1024
_MAX_EVENTS = 2000
_UTC_PATTERN = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z", re.ASCII)


class ShowMyTradesCalendarError(RuntimeError):
    """Provider/network/data uncertainty: new entries must be blocked."""


def _utc(value):
    if not isinstance(value, str) or _UTC_PATTERN.fullmatch(value) is None:
        raise ShowMyTradesCalendarError("ShowMyTrades UTC timestamp unverified")
    try:
        return parse_utc(value)
    except (ValueError, TypeError) as exc:
        raise ShowMyTradesCalendarError("ShowMyTrades UTC timestamp invalid") from exc


def _epoch(value):
    if type(value) is not int or value < 0:
        raise ShowMyTradesCalendarError("ShowMyTrades epoch invalid")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, ValueError, OSError) as exc:
        raise ShowMyTradesCalendarError("ShowMyTrades epoch outside UTC range") from exc


def fetch_showmytrades_calendar(*, now, http_get=None):
    """Fetch HIGH news with one bounded GET, no retry or redirect.

    There is no API key. This source is only considered usable when it states a
    fresh generation time, exact event count and a time envelope around `now`.
    """
    current = require_utc(now)
    get = requests.get if http_get is None else http_get
    try:
        response = get(
            SHOWMYTRADES_ENDPOINT, params=dict(_PARAMS),
            headers={"Accept": "application/json"},
            timeout=10, allow_redirects=False,
        )
        if response.status_code != 200:
            raise ShowMyTradesCalendarError("ShowMyTrades HTTP unavailable")
        raw = response.content
        if not isinstance(raw, bytes) or not 2 <= len(raw) <= _MAX_BYTES:
            raise ShowMyTradesCalendarError("ShowMyTrades response size invalid")
        payload = response.json()
    except requests.RequestException as exc:
        raise ShowMyTradesCalendarError("ShowMyTrades request unavailable") from None
    except (ValueError, TypeError) as exc:
        raise ShowMyTradesCalendarError("ShowMyTrades JSON invalid") from None

    if not isinstance(payload, dict):
        raise ShowMyTradesCalendarError("ShowMyTrades envelope missing")
    server = _epoch(payload.get("server_time"))
    server_iso = _utc(payload.get("server_time_iso"))
    generated = _utc(payload.get("generated_at"))
    start = _utc(payload.get("from"))
    end = _utc(payload.get("to"))
    # Provider claims its time header is current and the body is cached up to
    # 15 minutes. A stale replay, clock discrepancy or incomplete horizon is
    # UNKNOWN, even if it happens to contain no events.
    if (server != server_iso
        or abs((server - current).total_seconds()) > 300
        or generated > server + timedelta(seconds=5)
        or server - generated > timedelta(minutes=20)
        or generated > current + timedelta(seconds=5)
        or current - generated > timedelta(minutes=30)
        or start >= end or end - start > timedelta(days=3)
        or start > current - timedelta(minutes=30)
        or end < current + timedelta(minutes=30)):
        raise ShowMyTradesCalendarError("ShowMyTrades freshness or coverage unverified")

    raw_events = payload.get("events")
    count = payload.get("count")
    if (not isinstance(raw_events, list)
        or type(count) is not int or count != len(raw_events)
        or count > _MAX_EVENTS):
        raise ShowMyTradesCalendarError("ShowMyTrades event inventory incomplete")

    events = []
    seen = set()
    for item in raw_events:
        if not isinstance(item, dict):
            raise ShowMyTradesCalendarError("Malformed ShowMyTrades event")
        event_time = _utc(item.get("time"))
        event_epoch = _epoch(item.get("ts"))
        currency = item.get("currency")
        title = item.get("title")
        event_id = item.get("id")
        impact = item.get("impact")
        status = item.get("status")
        confidence = item.get("confidence")
        moved_from = item.get("moved_from")
        if (event_epoch != event_time or not start <= event_time <= end
            or currency not in SUPPORTED_CURRENCIES
            or impact != "high"
            or not isinstance(title, str) or not title.strip() or len(title) > 240
            or not isinstance(event_id, str) or not event_id.strip()
            or len(event_id) > 240
            or status not in ("scheduled", "moved")
            or confidence not in ("official", "sources_agree", "single_source")
            or event_id in seen):
            # Tentative dates, conflicting sources and unexpected records
            # cannot prove the absence of a HIGH event in a lockout window.
            raise ShowMyTradesCalendarError("ShowMyTrades event unverified")
        if status == "moved":
            if _utc(moved_from) == event_time:
                raise ShowMyTradesCalendarError("Moved event date unverified")
        elif moved_from != "":
            raise ShowMyTradesCalendarError("Unexpected moved event date")
        seen.add(event_id)
        events.append({
            "time": iso_utc(event_time), "currency": currency,
            "impact": "HIGH", "title": title.strip(),
        })

    return {
        "events": events, "fetched_at": iso_utc(generated),
        "coverage_start": iso_utc(start), "coverage_end": iso_utc(end),
        "currencies": list(SUPPORTED_CURRENCIES),
    }
