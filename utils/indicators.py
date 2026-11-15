from __future__ import annotations
from typing import List, Dict, Any, Optional

# ----------------------------------------------------------
#  Indicator Engine Algin
#
#  Llogarit EMA, SMA, ATR, Wick %, Body %, Candle Size etj.
#  Këto përdoren nga strategy, trend_engine, atr_engine, candle_rules.
# ----------------------------------------------------------

class Indicators:

    # -----------------------------
    #  EMA
    # -----------------------------
    def ema(self, values: List[float], period: int) -> Optional[float]:
        if len(values) < period:
            return None
        k = 2 / (period + 1)
        ema_val = values[0]
        for v in values[1:]:
            ema_val = v * k + ema_val * (1 - k)
        return ema_val

    # -----------------------------
    #  SMA
    # -----------------------------
    def sma(self, values: List[float], period: int) -> Optional[float]:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    # -----------------------------
    #  ATR klasik: period 14
    # -----------------------------
    def atr(self, candles: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
        if len(candles) < period + 1:
            return None

        trs = []
        for i in range(1, period+1):
            high = candles[-i]["high"]
            low = candles[-i]["low"]
            prev_close = candles[-i-1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        return sum(trs) / period

    # -----------------------------
    #  Wick % (jë superiore + inferiore)
    # -----------------------------
    def wick_percent(self, candle: Dict[str, Any]) -> Dict[str, float]:
        o = candle["open"]
        h = candle["high"]
        l = candle["low"]
        c = candle["close"]

        body = abs(c - o)
        total = h - l if (h - l) != 0 else 1e-9

        upper = (h - max(o, c)) / total
        lower = (min(o, c) - l) / total

        return {
            "upper": max(0.0, min(1.0, upper)),
            "lower": max(0.0, min(1.0, lower)),
            "body": max(0.0, min(1.0, body / total)),
        }

    # -----------------------------
    #  Candle Size relative vs ATR
    # -----------------------------
    def candle_vs_atr(self, candle: Dict[str, Any], atr: float) -> float:
        if atr <= 0:
            return 0.0
        return abs(candle["close"] - candle["open"]) / atr

    # -----------------------------
    #  Candle direction
    # -----------------------------
    def candle_direction(self, candle: Dict[str, Any]) -> str:
        if candle["close"] > candle["open"]:
            return "BULL"
        elif candle["close"] < candle["open"]:
            return "BEAR"
        return "NEUTRAL"
