"""Finite weighted signal score, explicit metric availability (P46/P47).

Unimplemented liquidity_sweep is NOT zero: omit it from numerator AND weight
denominator until a verified measurement exists. pattern_quality is an
explicit multiplicative penalty; in_reset_zone blocks before grading.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict


@dataclass
class ScoringConfig:
    w_trend: float = 1.0
    w_structure: float = 1.0
    w_retest: float = 1.0
    w_candle_quality: float = 1.0
    w_ema_alignment: float = 1.0
    w_atr_volatility: float = 1.0
    w_liquidity_sweep: float = 1.0
    grade_a_min: float = 80.0
    grade_b_min: float = 60.0
    grade_c_min: float = 40.0


class ScoringEngine:
    _WEIGHTS = {
        "trend_strength": "w_trend",
        "structure_quality": "w_structure",
        "retest_quality": "w_retest",
        "candle_quality": "w_candle_quality",
        "ema_alignment": "w_ema_alignment",
        "atr_normalized": "w_atr_volatility",
        "liquidity_sweep": "w_liquidity_sweep",
    }

    def __init__(self, config: ScoringConfig | None = None):
        self.config = config or ScoringConfig()
        for name in self._WEIGHTS.values():
            w = getattr(self.config, name)
            if isinstance(w, bool) or not isinstance(w, (int, float)) or (
                not math.isfinite(w) or w < 0
            ):
                raise ValueError("Invalid score weight")
        boundaries = (
            self.config.grade_c_min, self.config.grade_b_min,
            self.config.grade_a_min
        )
        if (not all(isinstance(x, (int, float)) and math.isfinite(x)
                    for x in boundaries)
            or not 0 <= boundaries[0] < boundaries[1] < boundaries[2] <= 100):
            raise ValueError("Invalid grade thresholds")

    @staticmethod
    def _metric(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Unusable {name} metric")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"Out-of-range {name} metric")
        return float(value)

    def score(self, metrics: Dict[str, float]) -> float:
        if not isinstance(metrics, dict):
            raise ValueError("Missing score metrics")
        reset = metrics.get("in_reset_zone", False)
        if not isinstance(reset, bool):
            raise ValueError("Unverified in_reset_zone status")
        if reset:
            return 0.0
        numerator = denominator = 0.0
        for metric, config_name in self._WEIGHTS.items():
            weight = float(getattr(self.config, config_name))
            if weight == 0:
                continue
            if metric == "liquidity_sweep" and metrics.get(metric) is None:
                # An unimplemented optional measurement is not a zero signal.
                continue
            value = self._metric(metrics.get(metric, 0.0), metric)
            numerator += value * weight
            denominator += weight
        if denominator <= 0:
            return 0.0
        pattern = self._metric(metrics.get("pattern_quality", 1.0),
                               "pattern_quality")
        result = numerator / denominator * 100.0 * pattern
        return max(0.0, min(100.0, result))

    def grade_from_score(self, score: float) -> str:
        if isinstance(score, bool) or not isinstance(score, (float, int)) or (
            not math.isfinite(score) or not 0 <= score <= 100
        ):
            raise ValueError("Invalid final score")
        if score >= self.config.grade_a_min:
            return "A"
        if score >= self.config.grade_b_min:
            return "B"
        if score >= self.config.grade_c_min:
            return "C"
        return "D"
