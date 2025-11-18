from __future__ import annotations
from typing import List, Dict

class EMAEngine:
    """Llogarit EMA-t dhe jep informacion për alignment midis EMA20/50.

    Funksionet kryesore:
    - ema(values, period)
    - ema_alignment(ema20, ema50)
    - ema_slope(ema_values) → për të matur pjerrësinë (trend angle)
    """

    def ema(self, values: List[float], period: int) -> float:
        """Llogarit EMA për një listë vlerash.

        values: listë me çmime (close)
        period: periudha e EMA-s (p.sh. 20 ose 50)

        Kthehet EMA e fundit.
        """
        if not values or len(values) < period:
            return values[-1] if values else 0.0

        k = 2 / (period + 1)
        ema_val = values[0]
        for v in values[1:]:
            ema_val = v * k + ema_val * (1 - k)
        return ema_val

    def ema_series(self, values: List[float], period: int) -> List[float]:
        """Kthen seri EMA për gjithë listën (opsionale)."""
        if not values:
            return []
        k = 2 / (period + 1)
        ema_vals = [values[0]]
        for v in values[1:]:
            ema_vals.append(v * k + ema_vals[-1] * (1 - k))
        return ema_vals

    def ema_alignment(self, ema20: float, ema50: float) -> float:
        """Kthen një vlerë 0.0–1.0 që tregon sa mirë janë të alinjuara EMA-t.

        - 1.0 = shumë mirë (EMA20 shumë më lart se EMA50 për LONG, ose shumë më poshtë për SHORT)
        - 0.5 = trend neutral
        - 0.0 = kundërtrend
        """
        if ema20 > ema50:
            # alignment UP
            diff = ema20 - ema50
            return min(1.0, max(0.0, diff / (abs(ema50) * 0.01 + 1e-6)))
        elif ema20 < ema50:
            # alignment DOWN
            diff = ema50 - ema20
            return min(1.0, max(0.0, diff / (abs(ema50) * 0.01 + 1e-6)))
        else:
            return 0.5

    def ema_slope(self, ema_values: List[float]) -> float:
        """Llogarit pjerrësinë (slope) e EMA-s si metrikë 0.0–1.0.

        Slope i fortë = trend i fortë
        Slope i dobët = trend i dobët / ranging
        """
        if len(ema_values) < 3:
            return 0.0
        diffs = [ema_values[i] - ema_values[i-1] for i in range(1, len(ema_values))]
        avg = sum(diffs) / len(diffs)
        # Normalizojmë slope: shumë e thjeshtë → cap në 1.0
        return max(0.0, min(1.0, abs(avg) / (abs(ema_values[-1]) * 0.001 + 1e-6)))
