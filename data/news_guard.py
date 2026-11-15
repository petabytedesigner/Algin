"""Fail-closed, cached, locally supplied economic-calendar guard.

There is no claim that OANDA provides economic-news calendar data.
An external trusted exporter must write an atomic JSON snapshot with
generated_at in UTC; stale/invalid snapshots block all new entries.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from broker.oanda_client import timestamp


class NewsGuard:
    def __init__(self, mode="UNCONFIGURED_BLOCK", path="", cache_seconds=900,
                 before_minutes=30, after_minutes=30):
        self.mode = mode
        self.path = Path(path) if path else None
        self.cache_seconds = cache_seconds
        self.before_minutes = before_minutes
        self.after_minutes = after_minutes
        self._snapshot = None
        self._cached_at = None

    def _load(self, now: datetime) -> list:
        if self._cached_at and now >= self._cached_at and (now-self._cached_at).total_seconds() < self.cache_seconds:
            return self._snapshot
        if self.path is None:
            raise ValueError("No economic calendar provider configured")
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("News payload must be object")
            issued = timestamp(obj["generated_at"])
            age = (now - issued).total_seconds()
            if age < -5 or age > self.cache_seconds:
                raise ValueError("Economic calendar snapshot stale or future")
            events = obj["events"]
            if not isinstance(events, list):
                raise ValueError("Economic calendar missing events")
            parsed = []
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("Malformed event")
                impact = event.get("impact")
                currency = event.get("currency")
                time_value = timestamp(event["time"])
                if not isinstance(impact, str) or not isinstance(currency, str):
                    raise ValueError("Missing impact/currency")
                if impact.upper() not in ("HIGH", "MEDIUM", "LOW"):
                    raise ValueError("Unrecognized event importance")
                if len(currency) != 3 or not currency.isalpha():
                    raise ValueError("Invalid news currency")
                parsed.append((time_value, impact.upper(), currency.upper()))
            self._snapshot = parsed
            self._cached_at = now
            return parsed
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._snapshot = None
            self._cached_at = None
            raise ValueError("Unverified economic calendar") from exc

    def check(self, instrument: str, now: datetime) -> tuple[bool, str]:
        if self.mode == "DISABLED_ACKNOWLEDGED":
            return True, "DISABLED_EXPLICITLY"
        if self.mode != "ENABLED_JSON":
            return False, "UNCONFIGURED"
        if now.tzinfo is None:
            return False, "CLOCK_INVALID"
        try:
            events = self._load(now.astimezone(timezone.utc))
        except ValueError:
            return False, "SOURCE_UNKNOWN"
        currencies = instrument.split("_")
        if len(currencies) != 2:
            return False, "INSTRUMENT_INVALID"
        for at, impact, currency in events:
            if impact != "HIGH" or currency not in currencies:
                continue
            window_start = at - timedelta(minutes=self.before_minutes)
            window_end = at + timedelta(minutes=self.after_minutes)
            if window_start <= now <= window_end:
                return False, "HIGH_IMPACT"
        return True, "VERIFIED_NO_BLOCKING_EVENT"
