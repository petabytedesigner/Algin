from __future__ import annotations
import datetime as dt
from typing import Optional

class MarketSessions:
    """Menaxhon sesionet kryesore të tregtimit për V10.

    Qëllimi:
    - Kontrollon nëse jemi brenda një sesioni aktiv.
    - Kontrollet bëhen sipas orës UTC.
    - Këto sesione mund të përditësohen nga config në të ardhmen.

    Sesionet tipike:
    - Asia:   23:00 – 06:00 UTC
    - London: 07:00 – 16:00 UTC
    - NY:     13:00 – 22:00 UTC
    """

    def __init__(self):
        # Mund të bëhen dinamike nga config nëse do
        self.asia_start  = dt.time(23, 0)
        self.asia_end    = dt.time(6, 0)
        self.london_start = dt.time(7, 0)
        self.london_end   = dt.time(16, 0)
        self.ny_start     = dt.time(13, 0)
        self.ny_end       = dt.time(22, 0)

    def _in_range(self, now: dt.datetime, start: dt.time, end: dt.time) -> bool:
        """Kontrollon nëse një orë është brenda fillim–fund.

        Trajton edhe rastet ku intervali kalon mesnatën (p.sh. 23:00–06:00).
        """
        t = now.time()
        if start < end:
            return start <= t <= end
        else:
            # Interval që kalon mesnatën
            return t >= start or t <= end

    def active_session(self, now: Optional[dt.datetime] = None) -> Optional[str]:
        if now is None:
            now = dt.datetime.now(dt.timezone.utc)

        if self._in_range(now, self.asia_start, self.asia_end):
            return "Asia"
        if self._in_range(now, self.london_start, self.london_end):
            return "London"
        if self._in_range(now, self.ny_start, self.ny_end):
            return "New York"
        return None

    def can_trade(self, now: Optional[dt.datetime] = None) -> bool:
        """Roboti mund të tregtojë vetëm brenda sesioneve kryesore."""
        return self.active_session(now) is not None
