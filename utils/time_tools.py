from __future__ import annotations
import datetime as dt
from typing import Optional

# ------------------------------------------------------
#  time_tools.py
#
#  Funksione ndihmëse për kohën:
#   - konvertime UTC
#   - parsing ISO
#   - kontroll i dritareve kohore
#   - orari aktual i tregut
# ------------------------------------------------------

class TimeTools:

    # -----------------------------------
    #  Timestamp ISO
    # -----------------------------------
    def now_iso(self) -> str:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat() + "Z"

    # -----------------------------------
    #  Parsing ISO8601 → datetime
    # -----------------------------------
    def parse_iso(self, ts: str) -> Optional[dt.datetime]:
        try:
            if ts.endswith("Z"):
                ts = ts.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(ts)
        except:
            return None

    # -----------------------------------
    #  Kontrollon intervalin kohor
    # -----------------------------------
    def in_time_window(self, now: dt.datetime, start: dt.time, end: dt.time) -> bool:
        t = now.time()
        if start < end:
            return start <= t <= end
        # interval që kalon mesnatën
        return t >= start or t <= end

    # -----------------------------------
    #  Diferencë minutash
    # -----------------------------------
    def minutes_diff(self, t1: dt.datetime, t2: dt.datetime) -> float:
        return (t1 - t2).total_seconds() / 60.0

    # -----------------------------------
    #  Shto minuta
    # -----------------------------------
    def add_minutes(self, t: dt.datetime, m: int) -> dt.datetime:
        return t + dt.timedelta(minutes=m)
