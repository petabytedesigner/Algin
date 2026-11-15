"""Strict timezone-aware UTC conversion primitives."""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from typing import Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return require_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str, *, legacy_naive_utc: bool = False) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing ISO8601 timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid ISO8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        if not legacy_naive_utc:
            raise ValueError("Naive timestamp rejected")
        result = result.replace(tzinfo=timezone.utc)
    return require_utc(result)


class TimeTools:
    def now_iso(self) -> str:
        return iso_utc(utc_now())

    def parse_iso(self, ts: str) -> Optional[datetime]:
        try:
            return parse_utc(ts)
        except ValueError:
            return None

    def in_time_window(self, now: datetime, start: time, end: time) -> bool:
        t = require_utc(now).time()
        if start < end:
            return start <= t <= end
        return t >= start or t <= end

    def minutes_diff(self, t1: datetime, t2: datetime) -> float:
        return (require_utc(t1) - require_utc(t2)).total_seconds() / 60.0

    def add_minutes(self, t: datetime, m: int) -> datetime:
        return require_utc(t) + timedelta(minutes=m)
