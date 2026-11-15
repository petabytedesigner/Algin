from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class ScoringConfig:
    """Konfigurim për sistemin e pikëzimit të sinjaleve.

    Vlerat reale do vijnë nga config.py; këtu ruajmë vetëm strukturën.
    Ideja:
    - çdo komponent i sinjalit (trend, strukturë, candle, ATR, etj.)
      jep një numër pikësh.
    - TOTALI i pikëve shndërrohet në një score 0–100.
    - Në bazë të këtij score, sinjali kategorizohet si Grade A/B/C/D.
    """

    # peshët (weights) bazë – DEFAULT, por në praktikë override nga config
    w_trend: float = 1.0
    w_structure: float = 1.0
    w_retest: float = 1.0
    w_candle_quality: float = 1.0
    w_ema_alignment: float = 1.0
    w_atr_volatility: float = 1.0
    w_liquidity_sweep: float = 1.0

    # pragjet për grade – score total 0–100
    grade_a_min: float = 80.0
    grade_b_min: float = 60.0
    grade_c_min: float = 40.0
    # < grade_c_min → Grade D


class ScoringEngine:
    """Motor i pikëzimit të sinjaleve për Algin.

    Kjo klasë nuk vendos vetë nëse sinjali është valid – thjesht jep një
    score numerik 0–100 bazuar në input-in që vjen nga strategy.

    strategy duhet t'i përgatisë metrikat në formën e një dictionary:

        metrics = {
            "trend_strength": 0.0–1.0,
            "structure_quality": 0.0–1.0,
            "retest_quality": 0.0–1.0,
            "candle_quality": 0.0–1.0,
            "ema_alignment": 0.0–1.0,
            "atr_normalized": 0.0–1.0,
            "liquidity_sweep": 0.0–1.0,
        }

    ScoringEngine i shumëzon këto me peshët përkatëse dhe kthen një score
    final 0–100. RiskEngine më pas e shndërron score në Grade.
    """

    def __init__(self, config: ScoringConfig | None = None):
        self.config = config or ScoringConfig()

    def score(self, metrics: Dict[str, float]) -> float:
        """Llogarit score total 0–100 nga metrikat e sinjalit.

        Çdo metrikë pritet të jetë në intervalin 0.0–1.0:
        - 0.0 = shumë keq
        - 1.0 = perfekt

        Nëse mungon ndonjë fushë, merret 0.0 (e keqe).
        """
        cfg = self.config

        trend = metrics.get("trend_strength", 0.0) * cfg.w_trend
        structure = metrics.get("structure_quality", 0.0) * cfg.w_structure
        retest = metrics.get("retest_quality", 0.0) * cfg.w_retest
        candle = metrics.get("candle_quality", 0.0) * cfg.w_candle_quality
        ema = metrics.get("ema_alignment", 0.0) * cfg.w_ema_alignment
        atr = metrics.get("atr_normalized", 0.0) * cfg.w_atr_volatility
        liq = metrics.get("liquidity_sweep", 0.0) * cfg.w_liquidity_sweep

        raw_score = trend + structure + retest + candle + ema + atr + liq

        # Normalizim i thjeshtë: supozojmë se 1.0 në çdo metrikë = 100 pikë totale.
        # Nëse peshët janë 1.0, maksimumi = 7.0 → e shkallëzojmë në 0–100.
        max_possible = (
            cfg.w_trend
            + cfg.w_structure
            + cfg.w_retest
            + cfg.w_candle_quality
            + cfg.w_ema_alignment
            + cfg.w_atr_volatility
            + cfg.w_liquidity_sweep
        )

        if max_possible <= 0:
            return 0.0

        normalized = (raw_score / max_possible) * 100.0
        # Kufizojmë score në 0–100 për siguri.
        return max(0.0, min(100.0, normalized))

    def grade_from_score(self, score: float) -> str:
        """Kthen Grade A/B/C/D bazuar në pragjet e konfigurimit."""
        cfg = self.config
        if score >= cfg.grade_a_min:
            return "A"
        if score >= cfg.grade_b_min:
            return "B"
        if score >= cfg.grade_c_min:
            return "C"
        return "D"
