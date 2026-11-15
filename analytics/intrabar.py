"""P70 conservative stop-vs-target OHLC ambiguity rule.

No mid-candle ordering can be inferred from OHLC. If SL and TP are both
touched in one available bar, resolve as SL_FIRST and label the assumption.
This is a simulation model only, NEVER broker fill evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime

from utils.time_tools import parse_utc


class IntrabarError(ValueError):
    pass


@dataclass(frozen=True)
class HitResult:
    outcome: str
    assumed_price: Decimal | None
    evidence: str


def _d(x, name):
    if isinstance(x, bool):
        raise IntrabarError(f"Invalid {name}")
    try:
        out = Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IntrabarError(f"Invalid {name}") from exc
    if not out.is_finite() or out <= 0:
        raise IntrabarError(f"Invalid nonpositive {name}")
    return out


def _bar(bar):
    if not isinstance(bar, dict) or bar.get("complete") is not True:
        raise IntrabarError("Unfinished/missing simulated OHLC")
    try:
        o, h, l, c = (_d(bar[k], k) for k in ("open", "high", "low", "close"))
    except KeyError as exc:
        raise IntrabarError("Missing OHLC") from exc
    if not l <= min(o, c) <= max(o, c) <= h:
        raise IntrabarError("Impossible OHLC")
    return h, l


def stop_or_target(bar, *, direction, entry, stop, target, finer=None):
    """P70 no optimistic ordering; optional complete chronological finer bars."""
    if direction not in ("LONG", "SHORT"):
        raise IntrabarError("Unknown direction")
    entry, stop, target = (_d(x, label) for x, label in (
        (entry, "entry"), (stop, "stop"), (target, "target")
    ))
    if not (stop < entry < target if direction == "LONG"
            else target < entry < stop):
        raise IntrabarError("Bad SL/TP hierarchy")
    parent = _bar(bar)
    bars = [bar]
    basis = "CONSERVATIVE_SINGLE_BAR"
    if finer is not None:
        if not isinstance(finer, list) or len(finer) < 2:
            raise IntrabarError("Insufficient finer candle evidence")
        times = []
        for sub in finer:
            high, low = _bar(sub)
            if high > parent[0] or low < parent[1]:
                raise IntrabarError("Finer candles outside parent OHLC")
            try:
                times.append(parse_utc(sub["time"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise IntrabarError("No verifiable finer chronology") from exc
        if len(set(times)) != len(times) or times != sorted(times):
            raise IntrabarError("Unordered or duplicate finer timestamps")
        try:
            start = parse_utc(bar["time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IntrabarError("Parent candle timestamp unavailable") from exc
        from datetime import timedelta
        span = timedelta(minutes=15)
        step = times[1] - times[0]
        if (times[0] != start or step <= timedelta(0)
            or step >= span or len(finer) * step != span
            or any(y - x != step for x, y in zip(times, times[1:]))
            or times[-1] + step != start + span):
            raise IntrabarError("Finer candles do not cover full M15 interval")
        if (_d(finer[0]["open"], "first open") != _d(bar["open"], "parent open")
            or _d(finer[-1]["close"], "last close") != _d(bar["close"], "parent close")
            or max(_d(x["high"], "sub high") for x in finer) != parent[0]
            or min(_d(x["low"], "sub low") for x in finer) != parent[1]):
            raise IntrabarError("Finer candles do not reconcile parent OHLC")
        bars = finer
        basis = "CHRONOLOGICAL_FINER_BARS"
    for candle in bars:
        high, low = _bar(candle)
        sl_hit = low <= stop if direction == "LONG" else high >= stop
        tp_hit = high >= target if direction == "LONG" else low <= target
        if sl_hit and tp_hit:
            return HitResult("STOP", stop, basis + "_BOTH_TOUCHED_STOP_FIRST")
        if sl_hit:
            return HitResult("STOP", stop, basis + "_STOP_TOUCHED")
        if tp_hit:
            return HitResult("TAKE_PROFIT", target, basis + "_TP_TOUCHED")
    return HitResult("NOT_HIT", None, basis + "_NO_HIT")
