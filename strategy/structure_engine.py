from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool  # True=Swing High, False=Swing Low

@dataclass
class StructureResult:
    bos: bool
    choch: bool
    direction: Optional[str]  # 'UP', 'DOWN', None
    swing_points: List[SwingPoint]
    quality: float  # 0.0–1.0

class StructureEngine:
    """Detekton BOS, CHoCH dhe strukturën e trendit sipas logjikës institucionale.

    ROBOTI V10:
    - BOS kërkon body close, jo wick.
    - CHoCH kërkon body close kundër trendit.
    - Strukturë e pastër (clean structure) rrit score.
    - Wick manipulimi ul score.
    - Returnimit të strukturës (higher low / lower high).
    """

    def detect(self, candles: List[dict]) -> StructureResult:
        swings = self._find_swings(candles)
        bos, choch, direction = self._detect_breaks(candles, swings)
        quality = self._structure_quality(swings)
        return StructureResult(
            bos=bos,
            choch=choch,
            direction=direction,
            swing_points=swings,
            quality=quality,
        )

    # ------------------------------
    # Internal helpers
    # ------------------------------

    def _find_swings(self, candles: List[dict]) -> List[SwingPoint]:
        """A pivot only exists after TWO later candles confirm its value."""
        swings = []
        for i in range(2, len(candles) - 2):
            p = candles[i]
            left = candles[i-2:i]
            right = candles[i+1:i+3]
            if p["high"] > max(x["high"] for x in left + right):
                swings.append(SwingPoint(i, p["high"], True))
            if p["low"] < min(x["low"] for x in left + right):
                swings.append(SwingPoint(i, p["low"], False))
        return swings

    def _detect_breaks(self, candles: List[dict], swings: List[SwingPoint]):
        highs = [x for x in swings if x.is_high]
        lows = [x for x in swings if not x.is_high]
        if not highs or not lows:
            return False, False, None
        latest_close = candles[-1]["close"]
        up = latest_close > highs[-1].price
        down = latest_close < lows[-1].price
        if up == down:
            return False, False, None
        previous_up = (len(highs) >= 2 and len(lows) >= 2 and
                       highs[-1].price > highs[-2].price and
                       lows[-1].price > lows[-2].price)
        previous_down = (len(highs) >= 2 and len(lows) >= 2 and
                         highs[-1].price < highs[-2].price and
                         lows[-1].price < lows[-2].price)
        change_of_character = previous_down if up else previous_up
        return not change_of_character, change_of_character, "UP" if up else "DOWN"

    def _structure_quality(self, swings: List[SwingPoint]) -> float:
        if len(swings) < 3:
            return 0.3
        spacing = abs(swings[-1].index - swings[-2].index)
        if spacing > 10:
            return 1.0
        if spacing > 5:
            return 0.7
        return 0.4
