"""Pinned Financial Modeling Prep economic-calendar adapter (GET-only).

This module normalizes the documented FMP global economic-calendar response
into NewsFilter's existing attested-shape contract. It does not authorize a
trade; failure or ambiguous data must always become NEWS_UNKNOWN upstream.

FMP provides a separate API subscription/credential. Never reuse OANDA tokens.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import re

import requests

from utils.time_tools import iso_utc, require_utc

FMP_ENDPOINT = "https://financialmodelingprep.com/stable/economic-calendar"
# The unfiltered FMP endpoint is documented as a global economic calendar.
# This is a provider-contract assumption, NOT a per-currency freshness proof.
MAJOR_FX_CURRENCIES = ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "USD")
_IMPACTS = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
_MAX_EVENTS = 5000
_MAX_BYTES = 2 * 1024 * 1024


class FmpCalendarError(RuntimeError):
    """Do not return CLEAR from an unavailable or unverified calendar."""


def _utc_event_time(value):
    # FMP explicitly documents calendar timestamps as UTC, with no suffix.
    # A timezone-dependent local conversion would shift news lockout windows.
    if not isinstance(value, str):
        raise FmpCalendarError("Calendar event date missing")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise FmpCalendarError("Calendar event date invalid") from exc
    return parsed.replace(tzinfo=timezone.utc)


def fetch_fmp_calendar(*, api_key, now, http_get=None):
    """Return complete-range normalized facts for a 3-day unfiltered query.

    This function makes one bounded GET; it never retries, follows redirects,
    logs a key, or fabricates a calendar when the provider is unavailable.
    """
    current = require_utc(now)
    if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 512
        or any(c.isspace() for c in api_key)):
        raise FmpCalendarError("FMP calendar authorization unavailable")
    start_date = (current - timedelta(days=1)).date()
    end_date = (current + timedelta(days=1)).date()
    coverage_start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    coverage_end = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
    get = requests.get if http_get is None else http_get
    try:
        response = get(
            FMP_ENDPOINT,
            params={"from": start_date.isoformat(), "to": end_date.isoformat(),
                    "apikey": api_key},
            headers={"Accept": "application/json"},
            timeout=10, allow_redirects=False,
        )
        if response.status_code != 200:
            raise FmpCalendarError("FMP economic calendar HTTP unavailable")
        raw_bytes = response.content
        if not isinstance(raw_bytes, bytes) or len(raw_bytes) > _MAX_BYTES:
            raise FmpCalendarError("FMP economic calendar response too large")
        raw_events = response.json()
    except requests.RequestException as exc:
        # Intentionally never include request/response objects or URL:
        # the FMP credential is transmitted as a query parameter.
        raise FmpCalendarError("FMP economic calendar request unavailable") from None
    except (ValueError, TypeError) as exc:
        raise FmpCalendarError("FMP economic calendar JSON invalid") from None

    if (not isinstance(raw_events, list)
        or not 1 <= len(raw_events) <= _MAX_EVENTS):
        # A successful but empty or truncated/unauthorized response does not
        # attest that there are no important events in the entire period.
        raise FmpCalendarError("FMP calendar event inventory unverified")

    events = []
    seen = set()
    for item in raw_events:
        if not isinstance(item, dict):
            raise FmpCalendarError("Malformed FMP economic event")
        currency = item.get("currency")
        impact = item.get("impact")
        title = item.get("event")
        if (not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency, re.ASCII) is None
            or not isinstance(impact, str)
            or impact.casefold() not in _IMPACTS
            or not isinstance(title, str) or not title.strip()
            or len(title) > 240):
            # Never drop bad records: a skipped HIGH event could falsely
            # clear an impacted FX instrument.
            raise FmpCalendarError("Unrecognized FMP event/currency/impact")
        when = _utc_event_time(item.get("date"))
        if not coverage_start <= when <= coverage_end:
            raise FmpCalendarError("FMP event outside requested date window")
        unique = (currency, when, title.strip())
        if unique in seen:
            raise FmpCalendarError("Duplicate FMP calendar event")
        seen.add(unique)
        # FMP's global calendar also includes currencies outside Algin's FX
        # universe. Validate every row first; only then exclude unrelated
        # valid currencies from the NewsFilter contract.
        if currency in MAJOR_FX_CURRENCIES:
            events.append({
                "time": iso_utc(when),
                "currency": currency,
                "impact": _IMPACTS[impact.casefold()],
                "title": title.strip(),
            })
    if not events:
        raise FmpCalendarError("No major-FX economic calendar evidence")

    return {
        "events": events,
        "fetched_at": iso_utc(current),
        "coverage_start": iso_utc(coverage_start),
        "coverage_end": iso_utc(coverage_end),
        "currencies": list(MAJOR_FX_CURRENCIES),
    }
