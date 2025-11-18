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
        swings = []
        for i in range(2, len(candles) - 2):
            p = candles[i]
            if p["high"] > candles[i-1]["high"] and p["high"] > candles[i+1]["high"]:
                swings.append(SwingPoint(i, p["high"], True))
            if p["low"] < candles[i-1]["low"] and p["low"] < candles[i+1]["low"]:
                swings.append(SwingPoint(i, p["low"], False))
        return swings

    def _detect_breaks(self, candles: List[dict], swings: List[SwingPoint]):
        bos = False
        choch = False
        direction = None

        if len(swings) < 2:
            return bos, choch, direction

        last = swings[-1]
        prev = swings[-2]

        close = candles[-1]["close"]

        # BOS logic: body close over previous swing
        if last.is_high and close > last.price:
            bos = True
            direction = "UP"
        if (not last.is_high) and close < last.price:
            bos = True
            direction = "DOWN"

        # CHoCH logic: break opposite structure
        if direction == "UP" and close < prev.price:
            choch = True
        if direction == "DOWN" and close > prev.price:
            choch = True

        return bos, choch, direction

    def _structure_quality(self, swings: List[SwingPoint]) -> float:
        if len(swings) < 3:
            return 0.3
        spacing = abs(swings[-1].index - swings[-2].index)
        if spacing > 10:
            return 1.0
        if spacing > 5:
            return 0.7
        return 0.4
