from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict

@dataclass
class PatternFilterResult:
    is_valid: bool
    quality: float   # 0.0–1.0
    reason: str

class PatternFilters:
    """Filtrat shtesë të pattern-eve për të shmangur sinjale të rrezikshme.

    Rregullat tona:
    - Konsolidim shumë i ngushtë (range < 30% ATR) → cilësi e ulët.
    - Inside-bar clusters → sinjale të dobëta.
    - 3+ qirinj pa drejtim → ranging → shmangim.
    - Candle që bie ndesh trendit të EMA → cilësi e ulët.
    - High volatility spikes → refuzim.
    """

    def check(
        self,
        candles: List[Dict],
        atr: float,
        trend_direction: str|None,
    ) -> PatternFilterResult:
        if len(candles) < 5:
            return PatternFilterResult(True, 0.5, "Pak qirinj — nuk filtrojmë.")

        last5 = candles[-5:]

        highs = [c["high"] for c in last5]
        lows = [c["low"] for c in last5]

        range_h = max(highs)
        range_l = min(lows)
        total_range = range_h - range_l

        # -------------------------------------------------------
        # 1) Konsolidim i ngushtë < 0.3 × ATR → shmangim
        # -------------------------------------------------------
        if atr > 0 and total_range < atr * 0.30:
            return PatternFilterResult(
                False,
                0.2,
                "Konsolidim shumë i ngushtë (<30% ATR)."
            )

        # -------------------------------------------------------
        # 2) Inside-bar cluster
        # Nëse 3 ose më shumë qirinj janë brenda range të njëri-tjetrit
        # -------------------------------------------------------
        inside_count = 0
        for i in range(1, 5):
            if (
                last5[i]["high"] <= last5[i-1]["high"]
                and last5[i]["low"] >= last5[i-1]["low"]
            ):
                inside_count += 1

        if inside_count >= 3:
            return PatternFilterResult(
                False,
                0.3,
                "Inside-bar cluster (3+ qirinj)."
            )

        # -------------------------------------------------------
        # 3) 3+ qirinj pa drejtim (body shumë të vogla)
        # -------------------------------------------------------
        directionless = 0
        for c in last5:
            body = abs(c["close"] - c["open"])
            total = c["high"] - c["low"] if c["high"] != c["low"] else 1e-6
            if body / total < 0.25:
                directionless += 1

        if directionless >= 3:
            return PatternFilterResult(
                False,
                0.3,
                "Ranging (3+ qirinj pa drejtim)."
            )

        # -------------------------------------------------------
        # 4) Qirinjtë kundër trendit
        # -------------------------------------------------------
        if trend_direction:
            count_against = 0
            for c in last5:
                if trend_direction == "UP" and c["close"] < c["open"]:
                    count_against += 1
                elif trend_direction == "DOWN" and c["close"] > c["open"]:
                    count_against += 1
            if count_against >= 4:
                return PatternFilterResult(
                    False,
                    0.2,
                    "Shumë qirinj kundër trendit EMA."
                )

        # -------------------------------------------------------
        # 5) High volatility spike detection
        # -------------------------------------------------------
        for c in last5:
            if atr > 0 and (c["high"] - c["low"]) > atr * 3:
                return PatternFilterResult(
                    False,
                    0.1,
                    "Spike shumë i madh (>3× ATR)."
                )

        # Nëse kalon të gjitha filtrat
        return PatternFilterResult(
            True,
            1.0,
            "Pattern i pastër, pa rreziqe."
        )
