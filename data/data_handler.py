from __future__ import annotations
import config as config
from utils.common import safe_request, log
from broker.oanda_v20_contract import price_tradeable, account_id
from broker.account_instruments import (
    InstrumentCatalogError, parse_account_instruments,
)
from broker.execution_validation import PreflightRejected, parse_market_quote
from risk.trade_plan import InstrumentRules
from decimal import Decimal
from data.candle_validation import CandleDataError, parse_oanda_candles
from utils.time_tools import parse_utc, utc_now
from datetime import timedelta
from math import isfinite
from typing import Optional, Dict, Any, List

class DataHandler:
    """Merr candles, pricing dhe të dhëna të tjera nga OANDA.
    I përdor strategy dhe main.
    """

    def __init__(self):
        self.base_url=config.OANDA_URL
        self.account_id=config.ACCOUNT_ID
        self.api_key=config.API_KEY


    def get_candles(self, instrument: str, granularity: str = "M15",
                    count: int = 200) -> Optional[List[Dict[str, Any]]]:
        """P50/P51: complete, ordered, structurally valid M15 candles only."""
        if (granularity != "M15" or not isinstance(count, int)
            or isinstance(count, bool) or not 1 <= count <= 5000):
            return None
        if not isinstance(instrument, str) or not instrument.replace("_", "").isalnum():
            return None
        if not self.api_key or not self.account_id:
            return None
        url = (
            f"{self.base_url}/accounts/{self.account_id}"
            f"/instruments/{instrument}/candles"
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        params = {"granularity": "M15", "count": str(count), "price": "M"}
        reply = safe_request("GET", url, headers=headers, params=params)
        if reply is None or reply.status_code != 200:
            log(f"DataHandler: OANDA candles unavailable for {instrument}")
            return None
        try:
            payload = reply.json()
            return parse_oanda_candles(payload, instrument=instrument)
        except (CandleDataError, ValueError, TypeError, KeyError) as exc:
            log(f"DataHandler: invalid complete M15 history for {instrument}: {exc}")
            return None


    def get_account_instruments(self) -> dict[str, InstrumentRules] | None:
        """Account-specific OANDA FX catalog; None means unverified.

        This performs a fresh GET on each scan, not a cached assumption.
        An empty dict means a verified catalog with no eligible FX pairs.
        """
        if not self.api_key or not account_id(self.account_id):
            return None
        url = f"{self.base_url}/accounts/{self.account_id}/instruments"
        response = safe_request(
            "GET", url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            retries=2,
        )
        if response is None or response.status_code != 200:
            return None
        try:
            return parse_account_instruments(response.json())
        except (InstrumentCatalogError, ValueError, TypeError, KeyError):
            return None


    def get_price(
        self, instrument: str, *, rules: InstrumentRules | None = None,
    ) -> Optional[Dict[str, float]]:
        """P54: nontradeable/stale/invalid OANDA prices never signal eligibility."""
        if not self.api_key or not self.account_id:
            return None
        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        reply = safe_request(
            "GET", url, headers={"Authorization": f"Bearer {self.api_key}"},
            params={"instruments": instrument}
        )
        if reply is None or reply.status_code != 200:
            return None
        try:
            data = reply.json()
            if not isinstance(data, dict) or not isinstance(data.get("prices"), list):
                return None
            matches = [p for p in data["prices"] if (
                isinstance(p, dict) and p.get("instrument") == instrument
            )]
            if len(matches) != 1:
                return None
            # The operational scan supplies independently verified account
            # instrument rules. Reuse the exact OrderEngine quote parser:
            # same time, pip size, precision, spread policy and status.
            if rules is not None:
                if not isinstance(rules, InstrumentRules):
                    return None
                verified = parse_market_quote(
                    data, instrument, rules, now=utc_now(),
                    max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
                    max_spread_pips=Decimal(str(config.MAX_SPREAD_PIPS)),
                )
                bid, ask = float(verified.bid), float(verified.ask)
                return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
            price = matches[0]
            # Legacy display-only compatibility. This branch does NOT
            # certify broker instrument metadata or spread eligibility.
            # Current ClientPrice.tradeable is authoritative. Deprecated
            # status is used only for legacy replies without the boolean.
            if not price_tradeable(price):
                return None
            timestamp = parse_utc(price["time"])
            now = utc_now()
            if timestamp > now + timedelta(seconds=2) or (
                now - timestamp > timedelta(
                    seconds=config.MAX_QUOTE_AGE_SECONDS
                )
            ):
                return None
            bid = float(price["bids"][0]["price"])
            ask = float(price["asks"][0]["price"])
            if (not isfinite(bid) or not isfinite(ask)
                or bid <= 0 or bid >= ask):
                return None
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        except (KeyError, IndexError, TypeError, ValueError,
                AttributeError, PreflightRejected):
            return None
