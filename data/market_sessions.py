"""Timezone-aware FX trading sessions and weekly market boundaries (P54).

DST is calculated using IANA zoneinfo, not fixed UTC hour offsets. Session
eligibility is separate from current broker tradeability; both are necessary
before actual execution. This module never assumes crypto/metal hours.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from utils.time_tools import require_utc, utc_now


class MarketSessions:
    LONDON = ZoneInfo("Europe/London")
    NEW_YORK = ZoneInfo("America/New_York")
    TOKYO = ZoneInfo("Asia/Tokyo")

    @staticmethod
    def _in_window(local: datetime, start: time, end: time) -> bool:
        t = local.time()
        return start <= t < end if start < end else t >= start or t < end

    def market_week_open(self, now=None) -> bool:
        current = (utc_now() if now is None else require_utc(now))
        new_york = current.astimezone(self.NEW_YORK)
        day = new_york.weekday()  # Monday 0 ... Sunday 6
        clock = new_york.time()
        if day == 5:  # Saturday
            return False
        if day == 6:
            return clock >= time(17, 0)
        if day == 4:
            return clock < time(17, 0)
        return True

    def active_session(self, now=None):
        current = utc_now() if now is None else require_utc(now)
        if not self.market_week_open(current):
            return None
        london = current.astimezone(self.LONDON)
        new_york = current.astimezone(self.NEW_YORK)
        tokyo = current.astimezone(self.TOKYO)
        if self._in_window(london, time(8), time(17)):
            return "London"
        if self._in_window(new_york, time(8), time(17)):
            return "New York"
        if self._in_window(tokyo, time(9), time(18)):
            return "Asia"
        return None

    def can_trade(self, now=None):
        """Session schedule only: does NOT attest to live broker tradeability."""
        return self.active_session(now) is not None

    def instrument_tradeable(self, instrument, *, now, broker_tradeable):
        """P54 require separate verified broker status; never assume market open."""
        if not isinstance(instrument, str) or instrument.count("_") != 1:
            return False
        # OANDA instrument-specific schedules vary; FX only in this policy.
        base, quote = instrument.split("_")
        fx = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
        if base not in fx or quote not in fx:
            return False
        return broker_tradeable is True and self.can_trade(now)
