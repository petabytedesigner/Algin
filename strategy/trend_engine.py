from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any

@dataclass
class TrendResult:
    direction: Optional[str]  # 'UP', 'DOWN', ose None
    strength: float           # 0.0–1.0
    is_overextended: bool
    in_reset_zone: bool

@dataclass
class TrendConfig:
    max_ema_distance_atr: float = 1.5  # nëse çmimi është më larg se kaq × ATR nga EMA20 → overextended

class TrendEngine:
    """Motor trendi për V10 bazuar në EMA20/50 dhe ATR.

    Rregullat tona:
    - Trend bazë: orientimi i EMA20 ndaj EMA50.
    - Nëse çmimi është shumë larg EMA20 (> 1.5 × ATR) → overextended.
    - Nëse overextended + kundra strukturës → in_reset_zone (trend reset filter).
    - Strength 0–1, ku:
        - 0.0 = pa trend / shumë i dobët
        - 1.0 = trend shumë i pastër dhe konsistent
    """

    def __init__(self, config: TrendConfig | None = None):
        self.config = config or TrendConfig()

    def evaluate(
        self,
        price: float,
        ema20: float,
        ema50: float,
        atr: float,
    ) -> TrendResult:
        """Vlerëson trendin nga EMA20/50 dhe ATR.

        - price: çmimi aktual
        - ema20, ema50: EMA-t përkatëse
        - atr: ATR aktual (në pips ose njësi çmimi, homogjen me price)
        """
        direction: Optional[str] = None
        strength: float = 0.0
        is_overextended = False
        in_reset_zone = False

        # 1) Përcakto drejtimin bazuar në EMA20 / EMA50
        if ema20 > ema50:
            direction = "UP"
        elif ema20 < ema50:
            direction = "DOWN"
        else:
            direction = None

        # 2) Llogarit distancën nga EMA20 në njësi ATR
        if atr <= 0:
            distance_atr = 0.0
        else:
            distance_atr = abs(price - ema20) / atr

        if distance_atr > self.config.max_ema_distance_atr:
            is_overextended = True

        # 3) Llogarit strength të trendit
        if direction is None:
            strength = 0.0
        else:
            # Sa më larg nga njëri-tjetri EMA20/EMA50, aq më i fortë trendi.
            if atr > 0:
                ema_distance_atr = abs(ema20 - ema50) / atr
            else:
                ema_distance_atr = 0.0

            # Normalizim i thjeshtë për strength (cap në 1.0)
            strength = max(0.0, min(1.0, ema_distance_atr / 2.0))

            # Overextension ul pak strength-in efektiv
            if is_overextended:
                strength *= 0.7

        # 4) Trend reset zone:
        # nëse trendi është i fortë por çmimi shumë larg EMA20 → in_reset_zone = True
        if strength > 0.5 and is_overextended:
            in_reset_zone = True

        return TrendResult(
            direction=direction,
            strength=strength,
            is_overextended=is_overextended,
            in_reset_zone=in_reset_zone,
        )
