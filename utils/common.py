"""Read-only compatibility utilities.

No POST/PUT operation is accepted by this HTTP adapter. Mutation execution
will be centralized with durable intent/reconciliation in Category 3.
"""
from __future__ import annotations

import logging
from math import isfinite
import time
from typing import Any, Dict, Optional

import requests

import config
from broker.oanda_v20_contract import price_tradeable
from utils.read_retry import GET_ATTEMPTS, get_backoff, get_status_retry_delay
from utils.time_tools import iso_utc, utc_now


LOG = logging.getLogger("algin.common")


def log(msg: str) -> None:
    LOG.info("%s", msg)


def safe_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[str] = None,
    retries: int = 3,
    timeout: int = 10,
) -> Optional[requests.Response]:
    """GET-only adapter; a broker read failure returns None, never success."""
    if not isinstance(method, str) or method.upper() != "GET":
        raise RuntimeError("Read-only adapter: broker mutation prohibited")
    # Retain the historical signature, but only read-only requests can retry.
    if not isinstance(retries, int) or retries < 1:
        raise ValueError("GET retries must be a positive integer")
    attempt_limit = min(retries, GET_ATTEMPTS)
    for attempt in range(attempt_limit):
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            LOG.warning("GET failed (attempt %s/%s)", attempt + 1, attempt_limit)
            if attempt + 1 < attempt_limit:
                time.sleep(get_backoff(attempt))
            continue
        if attempt + 1 < attempt_limit:
            delay = get_status_retry_delay(
                response.status_code, getattr(response, "headers", {}), attempt
            )
            if delay is not None:
                LOG.warning("GET response retryable (attempt %s/%s)", attempt + 1, attempt_limit)
                time.sleep(delay)
                continue
        return response
    return None


def get_pricing(instrument: str) -> Optional[Dict[str, float]]:
    """Legacy read-only helper; reject ambiguous or non-tradeable quotes."""
    url = f"{config.OANDA_URL}/accounts/{config.ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {config.API_KEY}"}
    response = safe_request(
        "GET", url, headers=headers, params={"instruments": instrument}
    )
    if response is None or response.status_code != 200:
        return None
    try:
        prices = response.json()["prices"]
        if not isinstance(prices, list):
            return None
        matches = [
            price for price in prices
            if isinstance(price, dict) and price.get("instrument") == instrument
            and price_tradeable(price)
        ]
        if len(matches) != 1:
            return None
        bid = float(matches[0]["bids"][0]["price"])
        ask = float(matches[0]["asks"][0]["price"])
        if not (isfinite(bid) and isfinite(ask) and 0 < bid < ask):
            return None
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None


def sleep_minutes(minutes: int) -> None:
    time.sleep(minutes * 60)


def now_utc_iso() -> str:
    return iso_utc(utc_now())


def pip_size(instrument: str) -> float:
    """Legacy approximate helper; broker metadata required for risk sizing."""
    if instrument.endswith("JPY"):
        return 0.01
    if "XAU" in instrument:
        return 0.10
    if "BTC" in instrument:
        return 1.0
    return 0.0001
