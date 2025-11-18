from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta


class Grade(str, Enum):
    """Signal quality grade.

    A = sinjal premium, i pastër, me probabilitet më të lartë.
    B = sinjal i mirë, por jo aq perfekt sa A.
    C/D = nuk lejohen për hapje tregtish.
    """
    A = "A"
    B = "B"
    C = "C"
    D = "D"


@dataclass
class TradeCandidate:
    """Kandidat për tregti që po vlerësohet nga risk engine.

    Kjo strukturë përdoret nga strategjia kur ka një sinjal të ri.
    RiskEngine nuk vendos logjikën e sinjalit – vetëm riskun dhe ekspozimin.
    """
    instrument: str
    direction: str  # "LONG" ose "SHORT"
    grade: Grade
    score: float  # rezultat numerik nga scoring_engine (p.sh. 0–100)
    requested_risk_pct: float  # sa % e balancës do përdoret në këtë tregti
    timestamp: datetime


@dataclass
class OpenPositionRisk:
    """Përfaqëson një tregti tashmë të hapur, nga këndvështrimi i riskut."""
    trade_id: str
    instrument: str
    direction: str  # "LONG" ose "SHORT"
    grade: Grade
    risk_pct: float  # risk fillestar i planifikuar (jo P/L aktual)
    opened_at: datetime


@dataclass
class RiskConfig:
    """Konfigurim fleksibël për risk engine.

    Këtu vendosen limitet e ekspozimit, risk për grade, cooldown, etj.
    Në praktikë këto vlera do vijnë nga config_v10.py, por i mbajmë
    këtu në një strukturë të vetëm për t'i kaluar lehtësisht në RiskEngine.
    """
    max_total_risk_pct: float = 6.0  # limiti total (p.sh. 6%)
    risk_pct_grade_a: float = 3.0
    risk_pct_grade_b: float = 2.0
    max_open_trades_grade_a: int = 2
    max_open_trades_grade_b: int = 3
    cooldown_minutes_after_loss: int = 30
    allow_multiple_trades_same_instrument: bool = False
    allow_hedging_same_instrument: bool = False  # LONG dhe SHORT njëkohësisht


@dataclass
class InstrumentRiskState:
    """Gjendja e riskut për një instrument të caktuar."""
    last_loss_at: Optional[datetime] = None
    # më vonë mund të shtojmë statistika shtesë (winrate, rr, etj.)


class RiskDecisionCode(str, Enum):
    """Kodet e vendimeve të risk engine për debug dhe CSV."""
    ACCEPTED = "ACCEPTED"
    REJECT_GRADE = "REJECT_GRADE"
    REJECT_TOTAL_RISK = "REJECT_TOTAL_RISK"
    REJECT_TOO_MANY_A = "REJECT_TOO_MANY_A"
    REJECT_TOO_MANY_B = "REJECT_TOO_MANY_B"
    REJECT_INSTRUMENT_COOLDOWN = "REJECT_INSTRUMENT_COOLDOWN"
    REJECT_SAME_INSTRUMENT = "REJECT_SAME_INSTRUMENT"
    REJECT_HEDGING = "REJECT_HEDGING"


@dataclass
class RiskDecision:
    """Rezultati i vlerësimit nga RiskEngine."""
    accept: bool
    code: RiskDecisionCode
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "accept": "1" if self.accept else "0",
            "code": self.code.value,
            "reason": self.reason,
        }


class RiskEngine:
    """Motor i menaxhimit të riskut dhe ekspozimit për V10.

    Përgjegjësitë kryesore:
    - Zbaton kufirin total të riskut (p.sh. max 6%).
    - Zbaton rregullat e gradave (A/B/C/D).
    - Zbaton numrin maksimal të tregtive A dhe B.
    - Menaxhon cooldown pas humbjeve për çdo instrument.
    - Parandalon mbifutjen në të njëjtin instrument ose hedging-un (nëse ndalohet).

    KLASA NUK PREK OANDA DREJTPËRDREJT. Vetëm merr të dhëna nga
    strategjia / state dhe kthen vendim nëse lejohet një tregti e re.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        # state risku per instrument; në praktikë mund të persistohen nga jashtë
        self._instrument_state: Dict[str, InstrumentRiskState] = {}

    # -----------------------------
    #  API publike
    # -----------------------------

    def evaluate_candidate(
        self,
        candidate: TradeCandidate,
        open_positions: List[OpenPositionRisk],
    ) -> RiskDecision:
        """Vlerëson nëse kandidati mund të hapet, bazuar në risk.

        Nuk verifikon strukturën, EMA, ATR, etj. – vetëm risk & ekspozim.
        """
        cfg = self.config

        # 1) Filtri i gradës – C dhe D nuk lejohen
        if candidate.grade not in (Grade.A, Grade.B):
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_GRADE,
                reason=f"Grade {candidate.grade} nuk lejohet për hapje tregtie.",
            )

        # 2) Kontrollo cooldown për instrumentin
        if self._is_in_cooldown(candidate.instrument, candidate.timestamp):
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_INSTRUMENT_COOLDOWN,
                reason="Instrumenti është në cooldown pas një humbjeje.",
            )

        # 3) Kontrollo kufirin total të riskut
        total_risk = sum(p.risk_pct for p in open_positions)
        requested_risk = candidate.requested_risk_pct

        if total_risk + requested_risk > cfg.max_total_risk_pct + 1e-9:
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_TOTAL_RISK,
                reason=(
                    f"Risk total ({total_risk:.2f}%) + i ri "
                    f"({requested_risk:.2f}%) kalon kufirin "
                    f"{cfg.max_total_risk_pct:.2f}%."
                ),
            )

        # 4) Kontrollo numrin e tregtive A dhe B
        num_a = sum(1 for p in open_positions if p.grade == Grade.A)
        num_b = sum(1 for p in open_positions if p.grade == Grade.B)

        if candidate.grade == Grade.A and num_a >= cfg.max_open_trades_grade_a:
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_TOO_MANY_A,
                reason=(
                    f"Ekzistojnë tashmë {num_a} tregti me Grade A, "
                    f"limiti është {cfg.max_open_trades_grade_a}."
                ),
            )

        if candidate.grade == Grade.B and num_b >= cfg.max_open_trades_grade_b:
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_TOO_MANY_B,
                reason=(
                    f"Ekzistojnë tashmë {num_b} tregti me Grade B, "
                    f"limiti është {cfg.max_open_trades_grade_b}."
                ),
            )

        # 5) Kontrollo nëse lejojmë më shumë se një tregti në të njëjtin instrument
        same_instrument_positions = [
            p for p in open_positions if p.instrument == candidate.instrument
        ]

        if not cfg.allow_multiple_trades_same_instrument and same_instrument_positions:
            return RiskDecision(
                accept=False,
                code=RiskDecisionCode.REJECT_SAME_INSTRUMENT,
                reason="Një tregti është tashmë e hapur në këtë instrument.",
            )

        # 6) Kontrollo hedging nëse nuk lejohet
        if not cfg.allow_hedging_same_instrument and same_instrument_positions:
            # nëse ekziston një tregti në drejtim të kundërt, refuzo
            for p in same_instrument_positions:
                if p.direction != candidate.direction:
                    return RiskDecision(
                        accept=False,
                        code=RiskDecisionCode.REJECT_HEDGING,
                        reason="Hedging në të njëjtin instrument nuk lejohet.",
                    )

        # Nëse kalon të gjitha filtrat – pranohet
        return RiskDecision(
            accept=True,
            code=RiskDecisionCode.ACCEPTED,
            reason="Kandidati kaloi të gjitha rregullat e riskut.",
        )

    def register_loss(
        self,
        instrument: str,
        when: Optional[datetime] = None,
    ) -> None:
        """Thirret kur një tregti për një instrument mbyllet në humbje.

        Përdoret për të futur instrumentin në cooldown.
        """
        when = when or datetime.utcnow()
        state = self._instrument_state.get(instrument) or InstrumentRiskState()
        state.last_loss_at = when
        self._instrument_state[instrument] = state

    def set_instrument_state(self, instrument: str, state: InstrumentRiskState) -> None:
        """Lejon ngarkimin e state nga jashtë (p.sh. nga state_v10.json)."""
        self._instrument_state[instrument] = state

    def get_instrument_state(self, instrument: str) -> InstrumentRiskState:
        """Kthen gjendjen e riskut për një instrument (krijon të re nëse mungon)."""
        state = self._instrument_state.get(instrument)
        if state is None:
            state = InstrumentRiskState()
            self._instrument_state[instrument] = state
        return state

    # -----------------------------
    #  Funksione të brendshme
    # -----------------------------

    def _is_in_cooldown(self, instrument: str, now: datetime) -> bool:
        cfg = self.config
        state = self._instrument_state.get(instrument)
        if not state or not state.last_loss_at:
            return False

        return now < state.last_loss_at + timedelta(
            minutes=cfg.cooldown_minutes_after_loss
        )
