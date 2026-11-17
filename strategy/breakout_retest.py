"""Confirmed-swing breakout -> retest -> confirmation state (P41/P42).

Pure historical computation, never current-bar pivots. Only CLOSED candles
passed by the data validator are eligible. No breakout/retest in same candle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import math
import config


@dataclass(frozen=True)
class BreakRetest:
    direction: str
    breakout_index: int
    pivot_index: int
    pivot_price: float
    zone_low: float
    zone_high: float
    retest_quality: float
    state: str = "CONFIRMED"


class BreakoutRetest:
    """A recent confirmed swing is broken by a CLOSE, then retested later."""

    def __init__(self, *, expiry_bars=None, zone_atr=0.20,
                 max_retest_depth=0.30, break_buffer_atr=0.05):
        if expiry_bars is None:
            expiry_bars = config.MAX_RETEST_BARS
        if type(expiry_bars) is not int or not 1 <= expiry_bars <= 12:
            raise ValueError("Invalid expiry window")
        if not 0 < zone_atr <= 1 or not 0 < max_retest_depth < 1:
            raise ValueError("Invalid breakout/retest policy")
        self.expiry_bars = expiry_bars
        self.zone_atr = zone_atr
        self.max_retest_depth = max_retest_depth
        self.break_buffer_atr = break_buffer_atr

    @staticmethod
    def _pivots(prior, direction):
        # A swing at j is known only after TWO right-hand candles close.
        key = "high" if direction == "LONG" else "low"
        cmp = max if direction == "LONG" else min
        pivots = []
        for j in range(2, len(prior) - 2):
            center = prior[j][key]
            neighbors = [prior[k][key] for k in (j-2, j-1, j+1, j+2)]
            if center == cmp([center] + neighbors) and all(
                center != x for x in neighbors
            ):
                pivots.append((j, center))
        return pivots

    def _qualifying_retest(self, candle, direction, low_zone, high_zone):
        """Only the FIRST complete qualifying candle may confirm one BOS.

        This helper uses the same rules for historical and newest candles:
        later scans of a rolling window cannot confirm the same breakout
        again on a second favorable candle.
        """
        if (direction == "LONG" and candle["close"] <= candle["open"]) or (
            direction == "SHORT" and candle["close"] >= candle["open"]
        ):
            return None
        if direction == "LONG":
            touched = candle["low"] <= high_zone and candle["high"] >= low_zone
            depth = high_zone - candle["low"]
            confirmed = candle["close"] > high_zone
        else:
            touched = candle["high"] >= low_zone and candle["low"] <= high_zone
            depth = candle["high"] - low_zone
            confirmed = candle["close"] < low_zone
        if not touched or not confirmed or depth < 0:
            return None
        fraction = depth / (high_zone - low_zone)
        if fraction > self.max_retest_depth:
            return None
        body = abs(candle["close"] - candle["open"])
        if body <= 0:
            return None
        adverse_wick = (
            min(candle["open"], candle["close"]) - candle["low"]
            if direction == "LONG" else
            candle["high"] - max(candle["open"], candle["close"])
        )
        if adverse_wick > 3 * body:
            return None
        return max(0.0, min(1.0, 1.0 - fraction))

    def confirm(self, candles, direction, atr) -> Optional[BreakRetest]:
        if direction not in ("LONG", "SHORT") or len(candles) < 20:
            return None
        if not isinstance(atr, (float, int)) or not math.isfinite(atr) or atr <= 0:
            return None
        last = len(candles) - 1
        final = candles[last]
        # A bullish/bearish retest confirmation is mandatory.
        if (direction == "LONG" and final["close"] <= final["open"]) or (
            direction == "SHORT" and final["close"] >= final["open"]
        ):
            return None
        width = atr * self.zone_atr
        if width <= 0:
            return None
        # Search recent breakout candidates only; older breakouts have expired.
        for k in range(last - 1, max(1, last - self.expiry_bars - 1), -1):
            if k < 5:
                break
            candidates = self._pivots(candles[:k], direction)
            if not candidates:
                continue
            pivot_idx, level = candidates[-1]
            prev_close, break_close = candles[k-1]["close"], candles[k]["close"]
            buffer = atr * self.break_buffer_atr
            if direction == "LONG":
                if not prev_close <= level or not break_close > level + buffer:
                    continue
            else:
                if not prev_close >= level or not break_close < level - buffer:
                    continue
            low_zone, high_zone = level - width, level + width
            if low_zone <= 0:
                continue
            # Invalidation after breakout; retest cannot be preceded by an
            # opposite close across the structural lower/upper boundary.
            middle = candles[k+1:last]
            if direction == "LONG":
                if any(c["close"] < low_zone for c in middle):
                    continue
            else:
                if any(c["close"] > high_zone for c in middle):
                    continue
            quality = self._qualifying_retest(
                final, direction, low_zone, high_zone
            )
            if quality is None:
                continue
            # The same breakout must not produce a new signal for each
            # subsequent retest candle. Historical bars are all CLOSED and
            # use the identical qualifying test; no future data is consumed.
            if any(self._qualifying_retest(
                prior, direction, low_zone, high_zone
            ) is not None for prior in middle):
                continue
            return BreakRetest(
                direction=direction, breakout_index=k,
                pivot_index=pivot_idx, pivot_price=level,
                zone_low=low_zone, zone_high=high_zone,
                retest_quality=quality
            )
        return None
