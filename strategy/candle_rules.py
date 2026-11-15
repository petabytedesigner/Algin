from __future__ import annotations
from dataclasses import dataclass
from typing import Dict

@dataclass
class CandleQualityResult:
    quality: float   # 0.0–1.0
    is_doji: bool
    is_strong: bool
    reason: str

class CandleRules:
    """Analiza profesionale e qirinjve për Algin.

    - body_ratio ≥ 50%   → qiri i fortë (strong candle)
    - body_ratio < 20%   → doji / pa fuqi
    - wick_ratio i madh  → manipulim / cilësi e ulët
    - wick_small & body_strong → cilësi e lartë (A-grade candle)
    """

    def evaluate(self, candle: Dict[str, float]) -> CandleQualityResult:
        open_ = candle["open"]
        close = candle["close"]
        high = candle["high"]
        low = candle["low"]

        body = abs(close - open_)
        total = high - low if (high - low) != 0 else 1e-6

        body_ratio = body / total

        # Wick ratio
        upper_wick = high - max(open_, close)
        lower_wick = min(open_, close) - low
        wick_ratio = (upper_wick + lower_wick) / total

        # -------------------------------------------------
        # 1) Doji detection (trup shumë i vogël)
        # -------------------------------------------------
        is_doji = body_ratio < 0.20

        # -------------------------------------------------
        # 2) Strong Candle detection (body dominon)
        # -------------------------------------------------
        is_strong = body_ratio >= 0.50

        # -------------------------------------------------
        # 3) Quality score (0–1)
        # -------------------------------------------------
        quality = 0.0

        if is_doji:
            return CandleQualityResult(
                quality=0.1,
                is_doji=True,
                is_strong=False,
                reason="Doji / pa fuqi",
            )

        # Wick manipulimi (shumë i madh > 60% e qiririt)
        if wick_ratio > 0.60:
            return CandleQualityResult(
                quality=0.2,
                is_doji=False,
                is_strong=False,
                reason="Wick shumë i madh (manipulim).",
            )

        # Strong bullish ose bearish
        if is_strong and wick_ratio < 0.30:
            quality = 1.0
            reason = "Qiri i fortë, wick i vogël — premium."
        elif is_strong:
            quality = 0.8
            reason = "Qiri i fortë, por wick pak më i madh."
        else:
            quality = 0.5
            reason = "Qiri mesatar, body i moderuar."

        return CandleQualityResult(
            quality=quality,
            is_doji=False,
            is_strong=is_strong,
            reason=reason,
        )
