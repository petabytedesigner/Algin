"""Algin closed-candle EMA20/50 + ATR + confirmed BOS/retest strategy.

No broker orders. Historical structure is fixed before breakout; signal bar
cannot create its own retest zone. The public evaluate_signal interface stays
compatible with prior categories; old caller-provided zone is intentionally
ignored to avoid including the signal candle itself (P41).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Dict, List, Optional

from data.candle_validation import CandleDataError, validate_candles
from risk.scoring_engine import ScoringConfig, ScoringEngine
from .atr_engine import ATREngine
from .breakout_retest import BreakoutRetest
from .candle_rules import CandleRules
from .ema_engine import EMAEngine
from .pattern_filters import PatternFilters
from .retest_engine import RetestEngine
from .structure_engine import StructureEngine
from .trend_engine import TrendEngine


@dataclass
class StrategySignal:
    instrument: str
    direction: str
    score: float
    grade: str
    metrics: Dict[str, float]
    reason: str
    timestamp: datetime


class Strategy:
    """Pure signal quality gate: breakout -> retest -> confirmation only."""

    def __init__(self, scoring_config: Optional[ScoringConfig] = None):
        self.structure_engine = StructureEngine()
        self.breakout_retest = BreakoutRetest(expiry_bars=4)
        self.retest_engine = RetestEngine()
        self.trend_engine = TrendEngine()
        self.ema_engine = EMAEngine()
        self.atr_engine = ATREngine()
        self.candle_rules = CandleRules()
        self.pattern_filters = PatternFilters()
        self.scoring = ScoringEngine(scoring_config or ScoringConfig())

    def evaluate_signal(
        self, instrument: str, direction: str,
        candles: List[Dict[str, Any]], ema20: float, ema50: float,
        atr: float, zone_low: float, zone_high: float, timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """Return a signal only when all directional and data gates pass."""
        if direction not in ("LONG", "SHORT"):
            return None
        if not isinstance(instrument, str) or "_" not in instrument:
            return None
        if any(not isinstance(x, (int, float)) or not math.isfinite(x)
               or x <= 0 for x in (ema20, ema50, atr)):
            return None
        try:
            clean = validate_candles(candles, now=timestamp, min_count=50)
        except (CandleDataError, ValueError, TypeError):
            return None
        last = clean[-1]
        trend = self.trend_engine.evaluate(
            price=last["close"], ema20=ema20, ema50=ema50, atr=atr
        )
        wanted = "UP" if direction == "LONG" else "DOWN"
        if trend.direction != wanted or trend.is_overextended or trend.in_reset_zone:
            return None

        # Explicit bullish LONG / bearish SHORT candle confirmation.
        if (direction == "LONG" and last["close"] <= last["open"]) or (
            direction == "SHORT" and last["close"] >= last["open"]
        ):
            return None

        sequence = self.breakout_retest.confirm(clean, direction, atr)
        if sequence is None or sequence.state != "CONFIRMED":
            return None
        # All structural evidence must exist *by the breakout candle*, not
        # be added retroactively by the retest/signal candle.
        structure = self.structure_engine.detect(
            clean[:sequence.breakout_index + 1]
        )
        if not structure.bos or structure.direction != wanted:
            return None

        retest = self.retest_engine.check_retest(
            last, sequence.zone_low, sequence.zone_high, direction
        )
        if not retest.is_valid:
            return None
        candle_quality = self.candle_rules.evaluate(last)
        if candle_quality.is_doji:
            return None
        patterns = self.pattern_filters.check(
            candles=clean, atr=atr, trend_direction=trend.direction
        )
        if not patterns.is_valid:
            return None
        ema_score = self.ema_engine.ema_alignment(ema20, ema50)
        atr_score = self.atr_engine.normalize_for_scoring(
            atr_val=atr, price=last["close"]
        )
        metrics = {
            "trend_strength": trend.strength,
            "structure_quality": structure.quality,
            "retest_quality": min(sequence.retest_quality, retest.quality),
            "candle_quality": candle_quality.quality,
            "ema_alignment": ema_score,
            "atr_normalized": atr_score,
            "pattern_quality": patterns.quality,
            "in_reset_zone": False,
            # liquidity_sweep is NOT measured and has NO denominator weight.
        }
        try:
            score = self.scoring.score(metrics)
            grade = self.scoring.grade_from_score(score)
        except ValueError:
            return None
        if score < self.scoring.config.grade_c_min:
            return None
        reason = ["CHoCH" if structure.choch else "BOS",
                  "Breakout->Retest", f"Trend {trend.direction}"]
        if patterns.quality < 1:
            reason.append("Pattern penalty applied")
        return StrategySignal(
            instrument=instrument, direction=direction, score=score,
            grade=grade, metrics=metrics,
            reason=", ".join(reason), timestamp=timestamp
        )
