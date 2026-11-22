"""OANDA REST v20 transport; real mutations hard-disabled in Category 3.

Default client has no retry adapter for POST/PUT and no redirect following.
Unit tests inject a fake transport. Category 4 must supply reconciled entry
authorization before any configured real Practice submission is permitted.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time
from typing import Optional

import requests

import config
from broker.oanda_v20_contract import account_id
from utils.read_retry import (
    GET_ATTEMPTS, RETRYABLE_GET_STATUSES, get_backoff,
    get_status_retry_delay, server_retry_after_seconds,
)


PRACTICE_API = "https://api-fxpractice.oanda.com/v3"
# Category-3 build must NEVER submit real orders, even if config is edited.
# A future reviewed category must explicitly replace this release gate.
_PRACTICE_WRITES_RELEASED = False
LOG = logging.getLogger("algin.practice_transport")


class BrokerCommunicationError(RuntimeError):
    """Transport could not establish a reliable broker response."""


class BrokerWriteDisabled(RuntimeError):
    """No account-changing network request is authorized in this build."""


@dataclass(frozen=True)
class BrokerReply:
    status_code: int
    body: Optional[dict]
    request_id: Optional[str] = None


class PracticeTransport:
    def __init__(self, *, session=None):
        self.session = session or requests.Session()
        # Disable library-managed mutation retries; never silently resend.
        self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self._read_cooldown_until = None
        self.read_reconciliation_required = False

    def _trip_read_circuit(self, *, retry_after=None):
        # Three failed GET attempts are enough to temporarily stop polling.
        # Broker replies alone cannot clear the need for full reconciliation.
        cooldown = max(10.0, retry_after or 0.0)
        self._read_cooldown_until = time.monotonic() + cooldown
        self.read_reconciliation_required = True
        LOG.warning("Broker GET circuit open; cooldown_seconds=%s", cooldown)

    def mark_reconciled(self):
        """Called only after a complete, validated, durable broker snapshot."""
        if self.read_reconciliation_required:
            LOG.info("Broker GET circuit reset after verified account reconciliation")
        self._read_cooldown_until = None
        self.read_reconciliation_required = False

    def _base(self):
        if config.OANDA_URL != PRACTICE_API:
            raise BrokerWriteDisabled("Only OANDA Practice endpoint is supported")
        if not config.API_KEY or not account_id(config.ACCOUNT_ID):
            raise BrokerCommunicationError(
                "Practice credentials or account identifier invalid"
            )
        return f"{PRACTICE_API}/accounts/{config.ACCOUNT_ID}"

    def _headers(self):
        return {"Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json"}

    def _call(self, verb, path, *, payload=None, params=None):
        # The lowest-level HTTP boundary independently enforces the release
        # gate; calling this private helper cannot bypass the POST block.
        if verb != "GET":
            if (verb != "POST" or path != "/orders"
                or not _PRACTICE_WRITES_RELEASED
                or config.ORDER_WRITES_ENABLED is not True):
                raise BrokerWriteDisabled("Mutating broker HTTP is not released")
        # Never allow query strings, fragments, escapes, backslashes, empty
        # path segments or traversal inside a caller-supplied API path.
        # Query parameters must be supplied through the params argument.
        if (not isinstance(path, str)
            or re.fullmatch(r"(?:/[A-Za-z0-9_.@-]+)*", path, re.ASCII) is None
            or ".." in path):
            raise BrokerCommunicationError("Invalid account API path")
        url = self._base() + path
        if (verb == "GET" and self._read_cooldown_until is not None
            and time.monotonic() < self._read_cooldown_until):
            raise BrokerCommunicationError("Broker GET circuit cooling down")
        # Only reads retry. Mutations make exactly one attempt, regardless of
        # network timeout or HTTP status; outcome may already be committed.
        attempts = GET_ATTEMPTS if verb == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    verb, url, headers=self._headers(),
                    json=payload, params=params, timeout=10,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if verb == "GET" and attempt + 1 < attempts:
                    LOG.warning(
                        "Broker GET transport failure attempt=%s/%s class=%s",
                        attempt + 1, attempts, type(exc).__name__,
                    )
                    time.sleep(get_backoff(attempt))
                    continue
                if verb == "GET":
                    self._trip_read_circuit()
                raise BrokerCommunicationError(
                    f"Broker {verb} communication outcome unknown"
                ) from exc
            if verb == "GET" and response.status_code in RETRYABLE_GET_STATUSES:
                delay = get_status_retry_delay(
                    response.status_code, getattr(response, "headers", {}), attempt
                )
                if delay is not None:
                    LOG.warning(
                        "Broker GET retryable HTTP=%s attempt=%s/%s",
                        response.status_code, attempt + 1, attempts,
                    )
                    time.sleep(delay)
                    continue
                self._trip_read_circuit(
                    retry_after=server_retry_after_seconds(
                        getattr(response, "headers", {})
                    )
                )
            break
        try:
            body = response.json()
            if not isinstance(body, dict):
                body = None
        except (ValueError, TypeError):
            body = None
        if verb == "GET" and response.status_code == 200 and body is None:
            self._trip_read_circuit()
        return BrokerReply(
            response.status_code, body,
            request_id=response.headers.get("RequestID")
        )

    def read(self, path: str, *, params=None) -> BrokerReply:
        return self._call("GET", path, params=params)

    def submit_market_order(self, payload: dict) -> BrokerReply:
        # Deliberately not environment-configurable. Category-4 activation
        # requires an explicit code review & change to this constant.
        if not _PRACTICE_WRITES_RELEASED or config.ORDER_WRITES_ENABLED is not True:
            raise BrokerWriteDisabled("Category-3 broker writes are disabled")
        return self._call("POST", "/orders", payload=payload)
