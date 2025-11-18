from __future__ import annotations
from typing import List, Dict

class ATREngine:
    """Llogarit ATR dhe versione të normalizuara për V10.

    Përdoret për:
    - SL buffer (p.sh. 0.5 × ATR)
    - Trend overextension (distanca price–EMA / ATR)
    - Scoring (atr_normalized 0–1)
    """

    def atr(self, candles: List[Dict], period: int = 14) -> float:
        """Llogarit Average True Range (ATR) klasik.

        candles: listë me qirinj (dict: open, high, low, close)
        period: zakonisht 14
        """
        if len(candles) < period + 1:
            return 0.0

        trs = []
        for i in range(1, len(candles)):
            h = candles[i]["high"]
            l = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)

        if len(trs) < period:
            return 0.0

        # ATR i thjeshtë – mund të bëjmë edhe EMA ATR më vonë nëse duam
        return sum(trs[-period:]) / period

    def atr_series(self, candles: List[Dict], period: int = 14) -> List[float]:
        """Kthen serinë ATR për gjithë listën (opsionale)."""
        out = []
        for i in range(period, len(candles)):
            sub = candles[: i + 1]
            out.append(self.atr(sub, period))
        return out

    def normalize_for_scoring(self, atr_val: float, price: float) -> float:
        """Normalizon ATR në 0.0–1.0 për scoring.

        - ATR shumë i lartë → sinjal i rrezikshëm → ul score
        - ATR shumë i ulët → tregon qetësi → score normal
        """
        if price <= 0:
            return 0.0

        ratio = atr_val / price

        # logjikë e thjeshtë normalizimi:
        # <0.001 = shumë qetë → afër 1.0
        # 0.005 = normal → ≈ 0.5
        # >0.01 = shumë volatil → afër 0.0
        if ratio < 0.001:
            return 1.0
        if ratio > 0.01:
            return 0.0

        # mapim linear
        return max(0.0, min(1.0, 1.0 - (ratio - 0.001) / (0.01 - 0.001)))
