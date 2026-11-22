"""Bounded retry delays for idempotent broker GET requests only.

HTTP Retry-After is respected when short; a longer server delay is never
silently shortened to create extra traffic. Caller must fail closed instead.
"""
from __future__ import annotations

import re


RETRYABLE_GET_STATUSES = frozenset((408, 429, 500, 502, 503, 504))
GET_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 2.0
MAX_READ_CIRCUIT_SECONDS = 3600.0


def get_backoff(attempt: int) -> float:
    """Attempt is zero-based; never called after the final attempt."""
    return min(2.0, 0.25 * (2 ** min(attempt, 4)))


def server_retry_after_seconds(headers):
    """Read a numeric Retry-After without trusting unbounded remote values."""
    value = headers.get("Retry-After", "") if headers is not None else ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return None
    if len(value) > 32:
        return MAX_READ_CIRCUIT_SECONDS
    return min(float(value), MAX_READ_CIRCUIT_SECONDS)


def get_status_retry_delay(status: int, headers, attempt: int):
    """Return seconds, or None if the response must be returned as-is."""
    if status not in RETRYABLE_GET_STATUSES or attempt >= GET_ATTEMPTS - 1:
        return None
    if status in (429, 503) and headers is not None:
        value = headers.get("Retry-After", "")
        if value:
            delay = server_retry_after_seconds(headers)
            if delay is None:
                return None
            if delay > MAX_RETRY_AFTER_SECONDS:
                return None
            return delay
    return get_backoff(attempt)
