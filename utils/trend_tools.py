from __future__ import annotations
from typing import Dict, Any, List

# ------------------------------------------------------
#  trend_tools.py
#
#  Funksione ndihmëse për trend-in që përdoren nga:
#  - trend_engine
#  - strategy_v10
#  - scoring_engine
# ------------------------------------------------------

class TrendToolsV10:

    # -----------------------------
    #  Llogarit pjerrësinë (slope)
    # -----------------------------
    def slope(self, values: List[float]) -> float:
        if len(values) < 3:
            return 0.0
        x = list(range(len(values)))
        y = values
        n = len(values)

        sum_x  = sum(x)
        sum_y  = sum(y)
        sum_x2 = sum(i*i for i in x)
        sum_xy = sum(i*j for i, j in zip(x, y))

        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return 0.0

        m = (n * sum_xy - sum_x * sum_y) / denom
        return m  # slope pozitiv → trend rritës

    # -----------------------------
    #  Forca e trendit (0.0–1.0)
    # -----------------------------
    def trend_strength(self, ema20: float, ema50: float, atr: float) -> float:
        if atr <= 0:
            return 0.0
        diff = abs(ema20 - ema50)
        return max(0.0, min(1.0, diff / (5 * atr)))

    # -----------------------------
    #  Kontrollon nëse trendi është i përputhur
    # -----------------------------
    def is_aligned(self, direction: str, ema20: float, ema50: float) -> bool:
        if direction == "UP":
            return ema20 > ema50
        if direction == "DOWN":
            return ema20 < ema50
        return False

    # -----------------------------
    #  Kontrollon overextension
    # -----------------------------
    def is_overextended(self, ema20: float, ema50: float, atr: float) -> bool:
        if atr <= 0:
            return False
        return abs(ema20 - ema50) > (6 * atr)

    # -----------------------------
    #  Kontrollon consolidation
    # -----------------------------
    def is_consolidating(self, ema20: float, ema50: float, atr: float) -> bool:
        if atr <= 0:
            return True
        return abs(ema20 - ema50) < (1.5 * atr)
