"""Algin closed-candle EMA20/50 + ATR + confirmed BOS/retest strategy.

No broker orders. Historical structure is fixed before breakout; signal bar
cannot create its own retest zone. The public evaluate_signal interface stays
compatible with prior categories; old caller-provided zone is intentionally
ignored to avoid including the signal candle itself (P41).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
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
    # Immutable BOS identity across sliding history windows and retest bars.
    # Optional for legacy/injected signals; journal remains the authority.
    setup_id: str | None = None


class Strategy:
    """Pure signal quality gate: breakout -> retest -> confirmation only."""

    def __init__(self, scoring_config: Optional[ScoringConfig] = None):
        self.structure_engine = StructureEngine()
        # One policy source: config.MAX_RETEST_BARS, including for direct
        # BreakoutRetest callers. No hidden per-strategy expiry constant.
        self.breakout_retest = BreakoutRetest()
        self.retest_engine = RetestEngine()
        self.trend_engine = TrendEngine()
        self.ema_engine = EMAEngine()
        self.atr_engine = ATREngine()
        self.candle_rules = CandleRules()
        self.pattern_filters = PatternFilters()
        self.scoring = ScoringEngine(scoring_config or ScoringConfig())

        # P59: reason for the last synchronous evaluation; not permission
        # to trade. Read immediately after evaluating each direction.
        self.last_rejection_reason = None

    def _reject(self, reason):
        self.last_rejection_reason = reason
        return None

    def evaluate_signal(
        self, instrument: str, direction: str,
        candles: List[Dict[str, Any]], ema20: float, ema50: float,
        atr: float, zone_low: float, zone_high: float, timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """Return a signal only when all directional and data gates pass."""
        self.last_rejection_reason = None
        if direction not in ("LONG", "SHORT"):
            return self._reject("INVALID_DIRECTION")
        if not isinstance(instrument, str) or "_" not in instrument:
            return self._reject("INVALID_INSTRUMENT")
        if any(not isinstance(x, (int, float)) or not math.isfinite(x)
               or x <= 0 for x in (ema20, ema50, atr)):
            return self._reject("INVALID_INDICATORS")
        try:
            clean = validate_candles(candles, now=timestamp, min_count=50)
        except (CandleDataError, ValueError, TypeError):
            return self._reject("INVALID_CANDLES")
        last = clean[-1]
        trend = self.trend_engine.evaluate(
            price=last["close"], ema20=ema20, ema50=ema50, atr=atr
        )
        wanted = "UP" if direction == "LONG" else "DOWN"
        if trend.direction != wanted or trend.is_overextended or trend.in_reset_zone:
            return self._reject("TREND_OR_RESET")

        # Explicit bullish LONG / bearish SHORT candle confirmation.
        if (direction == "LONG" and last["close"] <= last["open"]) or (
            direction == "SHORT" and last["close"] >= last["open"]
        ):
            return self._reject("CANDLE_DIRECTION")

        sequence = self.breakout_retest.confirm(clean, direction, atr)
        if sequence is None or sequence.state != "CONFIRMED":
            return self._reject("BREAKOUT_RETEST_MISSING")
        # All structural evidence must exist *by the breakout candle*, not
        # be added retroactively by the retest/signal candle.
        structure = self.structure_engine.detect_breakout(
            clean, breakout_index=sequence.breakout_index,
            pivot_index=sequence.pivot_index,
            pivot_price=sequence.pivot_price, direction=direction,
        )
        if not structure.bos or structure.direction != wanted:
            return self._reject("STRUCTURE_NOT_CONFIRMED")

        retest = self.retest_engine.check_retest(
            last, sequence.zone_low, sequence.zone_high, direction
        )
        if not retest.is_valid:
            return self._reject("RETEST_INVALID")
        candle_quality = self.candle_rules.evaluate(last)
        if candle_quality.is_doji:
            return self._reject("DOJI_CANDLE")
        patterns = self.pattern_filters.check(
            candles=clean, atr=atr, trend_direction=trend.direction
        )
        if not patterns.is_valid:
            return self._reject("PATTERN_REJECTED")
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
            return self._reject("SCORING_INVALID")
        if grade == "D":
            return self._reject("SCORE_BELOW_THRESHOLD")
        if grade not in ("A", "B"):
            # Grade C is observational only, never an executable signal.
            return self._reject("GRADE_NOT_TRADEABLE")
        reason = ["CHoCH" if structure.choch else "BOS",
                  "Breakout->Retest", f"Trend {trend.direction}"]
        if patterns.quality < 1:
            reason.append("Pattern penalty applied")
        # Sliding windows renumber pivot/breakout indices. A stable setup
        # identity depends on the actual completed UTC bars, never offsets
        # in the current 200-candle window or the latest retest candle.
        setup_key = json.dumps((
            "M15", instrument, direction,
            clean[sequence.pivot_index]["time"],
            clean[sequence.breakout_index]["time"],
        ), separators=(",", ":"), ensure_ascii=True).encode("ascii")
        setup_id = (
            "algin-setup-" +
            hashlib.sha256(setup_key).hexdigest()[:40]
        )
        return StrategySignal(
            instrument=instrument, direction=direction, score=score,
            grade=grade, metrics=metrics,
            reason=", ".join(reason), timestamp=timestamp,
            setup_id=setup_id,
        )
