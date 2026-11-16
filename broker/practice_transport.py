"""OANDA REST v20 transport; real mutations hard-disabled in Category 3.

Default client has no retry adapter for POST/PUT and no redirect following.
Unit tests inject a fake transport. Category 4 must supply reconciled entry
authorization before any configured real Practice submission is permitted.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

import requests

import config
from broker.oanda_v20_contract import account_id


PRACTICE_API = "https://api-fxpractice.oanda.com/v3"
# Category-3 build must NEVER submit real orders, even if config is edited.
# A future reviewed category must explicitly replace this release gate.
_PRACTICE_WRITES_RELEASED = False


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
        try:
            response = self.session.request(
                verb, url, headers=self._headers(),
                json=payload, params=params, timeout=10, allow_redirects=False
            )
        except requests.RequestException as exc:
            raise BrokerCommunicationError(
                f"Broker {verb} communication outcome unknown"
            ) from exc
        try:
            body = response.json()
            if not isinstance(body, dict):
                body = None
        except (ValueError, TypeError):
            body = None
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
