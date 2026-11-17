"""Verified timezone-aware news cache and UNKNOWN lockout (P56).

This is a cache/contract, NOT an invented economic-calendar provider.
Until an authenticated functional provider (P55 external dependency) is
configured, new-entry news eligibility is UNKNOWN and must fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import config
from utils.time_tools import parse_utc, require_utc, utc_now


class NewsUnavailable(RuntimeError):
    """Unknown, unavailable, malformed or stale calendar data."""


class NewsFilter:
    def __init__(self, *, ttl_minutes=15, max_age_minutes=30):
        self.lockout_minutes_before = config.NEWS_LOCKOUT_MINUTES_BEFORE
        self.lockout_minutes_after = config.NEWS_LOCKOUT_MINUTES_AFTER
        if not 0 < ttl_minutes <= max_age_minutes <= 180:
            raise ValueError("Invalid news cache time budget")
        self.ttl = timedelta(minutes=ttl_minutes)
        self.max_age = timedelta(minutes=max_age_minutes)
        self._events = None
        self._fetched_at = None
        self._checked_at = None
        self._source_identity = None
        self._coverage = None

    @staticmethod
    def _verify(payload: Any, now: datetime, max_age: timedelta):
        if (not isinstance(payload, dict) or set(payload) != {
            "events", "fetched_at", "coverage_start", "coverage_end",
            "currencies",
        }):
            raise NewsUnavailable("Economic calendar coverage not attested")
        try:
            fetched = parse_utc(payload["fetched_at"])
        except (ValueError, TypeError) as exc:
            raise NewsUnavailable("Unknown calendar timezone") from exc
        if fetched > now or now - fetched > max_age:
            raise NewsUnavailable("Calendar fetch timestamp stale/future")
        try:
            covered_start = parse_utc(payload["coverage_start"])
            covered_end = parse_utc(payload["coverage_end"])
        except (ValueError, TypeError) as exc:
            raise NewsUnavailable("Calendar coverage timezone unverified") from exc
        if (covered_start >= covered_end
            or covered_end - covered_start > timedelta(days=30)):
            raise NewsUnavailable("Calendar coverage range invalid")
        raw_currencies = payload["currencies"]
        if (not isinstance(raw_currencies, list)
            or not 1 <= len(raw_currencies) <= 64
            or any(not isinstance(c, str)
                   or re.fullmatch(r"[A-Z]{3}", c, re.ASCII) is None
                   for c in raw_currencies)
            or len(set(raw_currencies)) != len(raw_currencies)):
            raise NewsUnavailable("Calendar currency coverage invalid")
        covered_currencies = frozenset(raw_currencies)
        raw = payload["events"]
        if not isinstance(raw, list):
            raise NewsUnavailable("News events are not a list")
        events = []
        for item in raw:
            if not isinstance(item, dict):
                raise NewsUnavailable("Malformed economic event")
            try:
                event_time = parse_utc(item["time"])
                currency = item["currency"]
                impact = item["impact"]
                title = item["title"]
            except (KeyError, ValueError, TypeError) as exc:
                raise NewsUnavailable("Unverifiable economic event") from exc
            if (not isinstance(currency, str)
                or currency not in covered_currencies
                or event_time < covered_start or event_time > covered_end
                or not isinstance(impact, str)
                or impact not in ("HIGH", "MEDIUM", "LOW")
                or not isinstance(title, str) or not title.strip()):
                raise NewsUnavailable("Unrecognized currency, impact or title")
            events.append({
                "time": event_time, "currency": currency,
                "impact": impact, "title": title
            })
        return events, fetched, (
            covered_start, covered_end, covered_currencies,
        )

    def _fetch(self, *, now=None):
        static = getattr(config, "STATIC_NEWS_LIST", None)
        static_fetched = getattr(config, "STATIC_NEWS_FETCHED_AT", None)
        if static is not None:
            # Test-only/static inputs MUST also claim explicit range and
            # currency coverage. A dated but incomplete empty list is UNKNOWN.
            return {
                "events": static, "fetched_at": static_fetched,
                "coverage_start": getattr(
                    config, "STATIC_NEWS_COVERAGE_START", None
                ),
                "coverage_end": getattr(
                    config, "STATIC_NEWS_COVERAGE_END", None
                ),
                "currencies": getattr(
                    config, "STATIC_NEWS_CURRENCIES", None
                ),
            }
        url = getattr(config, "NEWS_PROVIDER_URL", None)
        if not isinstance(url, str) or not url:
            # Separate economic-calendar subscription: OANDA credentials
            # cannot authenticate a third-party provider. Absence means
            # NEWS_UNKNOWN, never an implied absence of market news.
            api_key = os.environ.get("FMP_API_KEY", "")
            if not api_key:
                raise NewsUnavailable("No verified economic-calendar source configured")
            from data.economic_calendar_fmp import (
                FmpCalendarError, fetch_fmp_calendar
            )
            try:
                return fetch_fmp_calendar(
                    api_key=api_key,
                    now=utc_now() if now is None else now,
                )
            except FmpCalendarError as exc:
                raise NewsUnavailable("FMP economic calendar unavailable or unverified") from None
        parsed = urlsplit(url)
        pinned_host = getattr(config, "NEWS_PROVIDER_HOST", None)
        # An arbitrary HTTPS URL is not a vetted data source. Require the
        # operator to approve one exact external provider host. Network
        # access is never attempted without this explicit pin.
        if (parsed.scheme != "https"
            or not isinstance(pinned_host, str) or not pinned_host
            or parsed.hostname != pinned_host
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.port not in (None, 443)
            or pinned_host in ("localhost", "127.0.0.1", "::1")):
            raise NewsUnavailable("Economic calendar provider not approved")
        import requests
        try:
            response = requests.get(url, timeout=10, allow_redirects=False)
            if response.status_code != 200:
                raise NewsUnavailable("Calendar API response unavailable")
            return response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise NewsUnavailable("Calendar data could not be verified") from exc

    def load_news(self, *, now=None):
        current = utc_now() if now is None else require_utc(now)
        static = getattr(config, "STATIC_NEWS_LIST", None)
        try:
            fingerprint = (
                hashlib.sha256(json.dumps(
                    static, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest() if static is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise NewsUnavailable("Malformed static calendar source") from exc
        source = (
            getattr(config, "NEWS_PROVIDER_URL", None),
            getattr(config, "NEWS_PROVIDER_HOST", None),
            hashlib.sha256(os.environ.get(
                "FMP_API_KEY", "").encode("utf-8")).hexdigest(),
            fingerprint,
            getattr(config, "STATIC_NEWS_FETCHED_AT", None),
            getattr(config, "STATIC_NEWS_COVERAGE_START", None),
            getattr(config, "STATIC_NEWS_COVERAGE_END", None),
            tuple(getattr(config, "STATIC_NEWS_CURRENCIES", ()) or ())
        )
        if (self._events is not None and self._checked_at is not None
            and self._source_identity == source):
            if (current >= self._checked_at
                and current - self._checked_at <= self.ttl
                and current >= self._fetched_at
                and current - self._fetched_at <= self.max_age
                and self._coverage is not None):
                return list(self._events)
        # Never use stale cache on request failure.
        self._events = None
        self._checked_at = None
        self._fetched_at = None
        self._source_identity = None
        self._coverage = None
        payload = self._fetch(now=current)
        events, fetched, coverage = self._verify(payload, current, self.max_age)
        self._events = events
        self._fetched_at = fetched
        self._coverage = coverage
        self._checked_at = current
        self._source_identity = source
        return list(events)

    def filter_high_impact(self, news_list):
        if not isinstance(news_list, list):
            raise NewsUnavailable("Unknown calendar input")
        return [e for e in news_list if e.get("impact") == "HIGH"]

    def affects_instrument(self, instrument, news):
        if not isinstance(instrument, str) or instrument.count("_") != 1:
            return False
        return news.get("currency") in instrument.split("_")

    def is_in_lockout(self, event_time, now):
        t = require_utc(event_time)
        current = require_utc(now)
        return (t - timedelta(minutes=self.lockout_minutes_before)
                <= current <=
                t + timedelta(minutes=self.lockout_minutes_after))

    def block_trading(self, instrument, now):
        """None only when news source covers both FX currencies and lockouts."""
        current = require_utc(now)
        if (not isinstance(instrument, str)
            or re.fullmatch(r"[A-Z]{3}_[A-Z]{3}", instrument) is None):
            return "NEWS_UNKNOWN: Invalid FX instrument"
        try:
            events = self.load_news(now=current)
            if self._coverage is None:
                raise NewsUnavailable("Calendar coverage not verified")
            start, end, covered = self._coverage
            base, quote = instrument.split("_")
            if (not {base, quote}.issubset(covered)
                or start > current - timedelta(minutes=self.lockout_minutes_after)
                or end < current + timedelta(minutes=self.lockout_minutes_before)):
                raise NewsUnavailable("Calendar currency/time horizon incomplete")
        except (NewsUnavailable, ValueError, TypeError, RuntimeError) as exc:
            return f"NEWS_UNKNOWN: {exc}"
        for item in self.filter_high_impact(events):
            if self.affects_instrument(instrument, item) and self.is_in_lockout(
                item["time"], current
            ):
                return f"HIGH-impact lockout {item['currency']}: {item['title']}"
        return None
