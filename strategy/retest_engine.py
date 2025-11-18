from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

@dataclass
class RetestResult:
    is_valid: bool
    reason: str
    quality: float  # 0.0–1.0
    retest_price: Optional[float]

class RetestEngine:
    """Logjika institucionale e Break & Retest për V10.

    Rregullat tona:
    - Retest kërkon prekjen e ZONËS me TRUP ose wick të shkurtër.
    - Wick manipulimi i gjatë → REJECT.
    - Retest më shumë se 30% në thellësi → REJECT.
    - Retest që nuk prek zonën fare → REJECT.
    - Body duhet të jetë në anën e duhur (bullish mbi zonë / bearish nën zonë).
    - Retest i pastër rrit quality për scoring.
    - Retest i shkurter + trend alignment → perfect A-grade.
    """

    def check_retest(
        self,
        candle: Dict[str, Any],
        zone_low: float,
        zone_high: float,
        direction: str,
    ) -> RetestResult:
        """Kthen RetestResult duke analizuar kandilin e retestit dhe zonën.

        - candle: një qiri i vetëm (dict) me open/high/low/close
        - zone_low / zone_high: kufijtë e zonës së retestit
        - direction: 'LONG' ose 'SHORT'
        """
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]
        open_ = candle["open"]

        # -------------------------------
        # 1) A prek qiriu zonën?
        # -------------------------------
        if high < zone_low or low > zone_high:
            return RetestResult(
                is_valid=False,
                reason="Nuk preku zonën fare.",
                quality=0.0,
                retest_price=None,
            )

        # -------------------------------
        # 2) Llogarisim retest depth
        # -------------------------------
        if direction == "LONG":
            # Qiri bie në zonë → retest nga lart
            depth = zone_high - low
            full = zone_high - zone_low
        else:
            # SHORT → retest nga poshtë
            depth = high - zone_low
            full = zone_high - zone_low

        if full <= 0:
            return RetestResult(False, "Zonë invalide.", 0.0, None)

        depth_pct = depth / full

        if depth_pct > 0.30:
            return RetestResult(
                is_valid=False,
                reason="Retest thellë mbi 30% zonës.",
                quality=0.0,
                retest_price=None,
            )

        # -------------------------------
        # 3) Analiza wick manipulimi
        # -------------------------------
        wick_up = high - max(open_, close)
        wick_down = min(open_, close) - low

        avg_body = abs(close - open_) or 1e-6

        # Wick shumë i gjatë → manipulim
        if wick_up > avg_body * 3 or wick_down > avg_body * 3:
            return RetestResult(
                False,
                "Wick manipulues (më i madh se 3× trupi).",
                0.0,
                None,
            )

        # -------------------------------
        # 4) Body direction & placement
        # -------------------------------
        if direction == "LONG":
            if close < zone_high:
                return RetestResult(
                    False,
                    "Close nën zonë (s'do hyjmë LONG).",
                    0.0,
                    None,
                )
        else:
            if close > zone_low:
                return RetestResult(
                    False,
                    "Close mbi zonë (s'do hyjmë SHORT).",
                    0.0,
                    None,
                )

        # -------------------------------
        # 5) Cilësia e retestit (0–1)
        # -------------------------------
        quality = 1.0 - depth_pct
        # retest i cekët = më i pastër → quality e lartë

        return RetestResult(
            is_valid=True,
            reason="Retest i vlefshëm dhe i pastër.",
            quality=quality,
            retest_price=close,
        )
