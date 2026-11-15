"""Confirmed fractal swing structure, close-only BOS/CHoCH (P43).

A swing requires two candles to its right; the current signal candle never
creates a swing used to certify itself. Wick-only breaks are not BOS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool


@dataclass
class StructureResult:
    bos: bool
    choch: bool
    direction: Optional[str]
    swing_points: List[SwingPoint]
    quality: float


class StructureEngine:
    def detect(self, candles: List[dict]) -> StructureResult:
        swings = self._find_swings(candles)
        bos, choch, direction = self._detect_breaks(candles, swings)
        quality = self._structure_quality(swings)
        return StructureResult(bos, choch, direction, swings, quality)

    def _find_swings(self, candles: List[dict]) -> List[SwingPoint]:
        result = []
        if len(candles) < 6:
            return result
        # Strict inequality avoids equating flat equal highs with breakouts.
        for index in range(2, len(candles) - 2):
            candle = candles[index]
            neighbors = [candles[j] for j in (
                index-2, index-1, index+1, index+2
            )]
            if all(candle["high"] > x["high"] for x in neighbors):
                result.append(SwingPoint(index, candle["high"], True))
            if all(candle["low"] < x["low"] for x in neighbors):
                result.append(SwingPoint(index, candle["low"], False))
        return sorted(result, key=lambda x: (x.index, not x.is_high))

    def _detect_breaks(self, candles: List[dict], swings: List[SwingPoint]):
        if len(candles) < 2:
            return False, False, None
        highs = [x for x in swings if x.is_high and x.index < len(candles) - 1]
        lows = [x for x in swings if not x.is_high and x.index < len(candles) - 1]
        close = candles[-1]["close"]
        previous = candles[-2]["close"]
        prior_trend = None
        if len(highs) > 1 and len(lows) > 1:
            if (highs[-1].price > highs[-2].price
                and lows[-1].price > lows[-2].price):
                prior_trend = "UP"
            elif (highs[-1].price < highs[-2].price
                  and lows[-1].price < lows[-2].price):
                prior_trend = "DOWN"
        up = bool(highs and previous <= highs[-1].price < close)
        down = bool(lows and previous >= lows[-1].price > close)
        if up and not down:
            return True, prior_trend == "DOWN", "UP"
        if down and not up:
            return True, prior_trend == "UP", "DOWN"
        return False, False, None

    def _structure_quality(self, swings):
        if len(swings) < 2:
            return 0.0
        spacing = swings[-1].index - swings[-2].index
        if spacing >= 10:
            return 1.0
        if spacing >= 5:
            return 0.7
        return 0.4
