"""Verified timezone-aware news cache and UNKNOWN lockout (P56).

This is a cache/contract, NOT an invented economic-calendar provider.
Until an authenticated functional provider (P55 external dependency) is
configured, new-entry news eligibility is UNKNOWN and must fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
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

    @staticmethod
    def _verify(payload: Any, now: datetime, max_age: timedelta):
        if not isinstance(payload, dict) or set(payload) != {"events", "fetched_at"}:
            raise NewsUnavailable("Unexpected economic calendar schema")
        try:
            fetched = parse_utc(payload["fetched_at"])
        except (ValueError, TypeError) as exc:
            raise NewsUnavailable("Unknown calendar timezone") from exc
        if fetched > now or now - fetched > max_age:
            raise NewsUnavailable("Calendar fetch timestamp stale/future")
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
            if (not isinstance(currency, str) or len(currency) != 3
                or not currency.isalpha() or currency != currency.upper()
                or not isinstance(impact, str)
                or impact not in ("HIGH", "MEDIUM", "LOW")
                or not isinstance(title, str) or not title.strip()):
                raise NewsUnavailable("Unrecognized currency, impact or title")
            events.append({
                "time": event_time, "currency": currency,
                "impact": impact, "title": title
            })
        return events, fetched

    def _fetch(self):
        static = getattr(config, "STATIC_NEWS_LIST", None)
        static_fetched = getattr(config, "STATIC_NEWS_FETCHED_AT", None)
        if static is not None:
            # An undated static fixture is never treated as live news evidence.
            return {"events": static, "fetched_at": static_fetched}
        url = getattr(config, "NEWS_PROVIDER_URL", None)
        if not isinstance(url, str) or not url:
            raise NewsUnavailable("No verified economic-calendar source configured")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise NewsUnavailable("Economic-calendar URL must be HTTPS")
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
        if self._events is not None and self._checked_at is not None:
            if (current >= self._checked_at
                and current - self._checked_at <= self.ttl
                and current >= self._fetched_at
                and current - self._fetched_at <= self.max_age):
                return list(self._events)
        # Never use stale cache on request failure.
        self._events = None
        self._checked_at = None
        self._fetched_at = None
        payload = self._fetch()
        events, fetched = self._verify(payload, current, self.max_age)
        self._events = events
        self._fetched_at = fetched
        self._checked_at = current
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
        """None means confirmed clear; NEWS_UNKNOWN always blocks new entries."""
        current = require_utc(now)
        try:
            events = self.load_news(now=current)
        except (NewsUnavailable, ValueError, TypeError) as exc:
            return f"NEWS_UNKNOWN: {exc}"
        for item in self.filter_high_impact(events):
            if self.affects_instrument(instrument, item) and self.is_in_lockout(
                item["time"], current
            ):
                return f"HIGH-impact lockout {item['currency']}: {item['title']}"
        return None
