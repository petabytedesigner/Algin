from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Any

from .structure_engine import StructureEngine
from .retest_engine import RetestEngine
from .trend_engine import TrendEngine
from .ema_engine import EMAEngine
from .atr_engine import ATREngine
from .candle_rules import CandleRules
from .pattern_filters import PatternFilters
from risk.scoring_engine import ScoringEngine, ScoringConfig


@dataclass
class StrategySignal:
    """Rezultati i strategjisë për një sinjal të vetëm.

    Ky objekt NUK hap tregti vetë; përdoret nga layer-i më lart
    (p.sh. main_v10) për të ndërtuar TradeCandidate dhe për t'ia
    kaluar RiskEngine-it.
    """
    instrument: str
    direction: str  # 'LONG' ose 'SHORT'
    score: float
    grade: str      # 'A', 'B', 'C', 'D'
    metrics: Dict[str, float]
    reason: str
    timestamp: datetime


class StrategyV10:
    """Bërthama e logjikës së sinjalit për robotin V10.

    Kombinon:
    - Strukturën (BOS / CHoCH)
    - Break & Retest
    - Trend (EMA20/50 + ATR)
    - Cilësinë e qiririt
    - Pattern filters (konsolidim, inside bars, ranging, spikes)
    - Scoring engine → score 0–100 → Grade A/B/C/D

    Kjo klasë është full-dinamike: nuk vendos risk-in, vetëm cilësinë
    e sinjalit dhe nëse duhet marrë në konsideratë.
    """

    def __init__(
        self,
        scoring_config: Optional[ScoringConfig] = None,
    ):
        self.structure_engine = StructureEngine()
        self.retest_engine = RetestEngine()
        self.trend_engine = TrendEngine()
        self.ema_engine = EMAEngine()
        self.atr_engine = ATREngine()
        self.candle_rules = CandleRules()
        self.pattern_filters = PatternFilters()
        self.scoring = ScoringEngine(scoring_config or ScoringConfig())

    def evaluate_signal(
        self,
        instrument: str,
        direction: str,               # 'LONG' ose 'SHORT'
        candles: List[Dict[str, Any]],
        ema20: float,
        ema50: float,
        atr: float,
        zone_low: float,
        zone_high: float,
        timestamp: datetime,
    ) -> Optional[StrategySignal]:
        """Analizon një sinjal të mundshëm Break & Retest.

        Kthen:
        - StrategySignal nëse sinjali kalon filtrat bazë
        - None nëse sinjali hidhet poshtë

        Shënim:
        - 'candles' duhet të jetë listë qirinjsh (open, high, low, close, time)
        - qiriu i fundit konsiderohet si qiri i retestit / sinjalit
        """
        if len(candles) < 20:
            return None  # nuk kemi mjaftueshëm histori

        last_candle = candles[-1]

        # 1) Strukturë: BOS / CHoCH / drejtim
        structure = self.structure_engine.detect(candles)

        # 2) Trend: EMA20/50 + ATR
        trend = self.trend_engine.evaluate(
            price=last_candle["close"],
            ema20=ema20,
            ema50=ema50,
            atr=atr,
        )

        # 3) Retest: qiriu i fundit ndaj zonës së dhënë
        retest = self.retest_engine.check_retest(
            candle=last_candle,
            zone_low=zone_low,
            zone_high=zone_high,
            direction=direction,
        )

        if not retest.is_valid:
            return None  # pa retest të vlefshëm, nuk ka sinjal

        # 4) Cilësia e qiririt
        candle_q = self.candle_rules.evaluate(last_candle)

        if candle_q.is_doji:
            return None  # nuk hyjmë me doji

        # 5) Pattern filters (5 qirinjtë e fundit)
        pattern = self.pattern_filters.check(
            candles=candles,
            atr=atr,
            trend_direction=trend.direction,
        )

        if not pattern.is_valid:
            return None

        # 6) EMA alignment
        ema_align = self.ema_engine.ema_alignment(ema20, ema50)

        # 7) ATR normalized për scoring
        atr_norm = self.atr_engine.normalize_for_scoring(
            atr_val=atr,
            price=last_candle["close"],
        )

        # 8) Përgatisim metrikat për scoring engine
        metrics: Dict[str, float] = {
            "trend_strength": trend.strength,
            "structure_quality": structure.quality,
            "retest_quality": retest.quality,
            "candle_quality": candle_q.quality,
            "ema_alignment": ema_align,
            "atr_normalized": atr_norm,
            # liquidity_sweep për tani 0.0 — mund të shtohet më vonë
            "liquidity_sweep": 0.0,
        }

        # Mund të ulin pak score-in nëse pattern_quality < 1.0
        if pattern.quality < 1.0:
            metrics["pattern_quality"] = pattern.quality
        else:
            metrics["pattern_quality"] = 1.0

        # 9) Score total & Grade
        score = self.scoring.score(metrics)
        grade = self.scoring.grade_from_score(score)

        # Nëse score është shumë i ulët, nuk e kthejmë si sinjal
        if score < 40.0:
            return None

        reason_parts = []
        if structure.bos:
            reason_parts.append("BOS")
        if structure.choch:
            reason_parts.append("CHoCH")
        if trend.direction:
            reason_parts.append(f"Trend {trend.direction}")
        if pattern.quality < 1.0:
            reason_parts.append("Pattern jo perfekt")
        if retest.quality > 0.8:
            reason_parts.append("Retest i pastër")
        if candle_q.is_strong:
            reason_parts.append("Qiri i fortë")

        reason = ", ".join(reason_parts) or "Sinjal i vlefshëm sipas rregullave."

        return StrategySignal(
            instrument=instrument,
            direction=direction,
            score=score,
            grade=grade,
            metrics=metrics,
            reason=reason,
            timestamp=timestamp,
        )
