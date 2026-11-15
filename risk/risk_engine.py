"""Algin risk decision engine.

Pure validation/limits only. The broker-exposure snapshot must be reconciled by
the calling layer; an empty list is not proof that a broker account is flat.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

from utils.time_tools import require_utc, utc_now


def _positive_decimal(value, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}: invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field}: invalid decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field}: must be finite and positive")
    return result


class Grade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


@dataclass
class TradeCandidate:
    instrument: str
    direction: str
    grade: Grade
    score: float
    requested_risk_pct: float
    timestamp: datetime


@dataclass
class OpenPositionRisk:
    trade_id: str
    instrument: str
    direction: str
    grade: Grade
    risk_pct: float
    opened_at: datetime


@dataclass
class RiskConfig:
    """Conservative Category-2 defaults; policy values must be explicit."""
    max_total_risk_pct: float = 1.0
    risk_pct_grade_a: float = 1.0
    risk_pct_grade_b: float = 0.5
    max_open_trades_grade_a: int = 2
    max_open_trades_grade_b: int = 3
    cooldown_minutes_after_loss: int = 30
    allow_multiple_trades_same_instrument: bool = False
    allow_hedging_same_instrument: bool = False
    max_open_trades_total: int = 3


@dataclass
class InstrumentRiskState:
    last_loss_at: Optional[datetime] = None


class RiskDecisionCode(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECT_INVALID_INPUT = "REJECT_INVALID_INPUT"
    REJECT_GRADE = "REJECT_GRADE"
    REJECT_GRADE_RISK = "REJECT_GRADE_RISK"
    REJECT_TOTAL_RISK = "REJECT_TOTAL_RISK"
    REJECT_TOO_MANY_A = "REJECT_TOO_MANY_A"
    REJECT_TOO_MANY_B = "REJECT_TOO_MANY_B"
    REJECT_TOO_MANY_TOTAL = "REJECT_TOO_MANY_TOTAL"
    REJECT_INSTRUMENT_COOLDOWN = "REJECT_INSTRUMENT_COOLDOWN"
    REJECT_SAME_INSTRUMENT = "REJECT_SAME_INSTRUMENT"
    REJECT_HEDGING = "REJECT_HEDGING"


@dataclass
class RiskDecision:
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
    """Reject unsafe candidates; never communicate with the broker."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        cfg = self.config
        cap = _positive_decimal(cfg.max_total_risk_pct, "max_total_risk_pct")
        a = _positive_decimal(cfg.risk_pct_grade_a, "risk_pct_grade_a")
        b = _positive_decimal(cfg.risk_pct_grade_b, "risk_pct_grade_b")
        if cap > 100 or a > cap or b > a:
            raise ValueError("Grade and total risk budgets are inconsistent")
        for field in (
            "max_open_trades_grade_a", "max_open_trades_grade_b",
            "max_open_trades_total",
        ):
            item = getattr(cfg, field)
            if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                raise ValueError(f"{field}: invalid")
        if (
            not isinstance(cfg.cooldown_minutes_after_loss, int)
            or isinstance(cfg.cooldown_minutes_after_loss, bool)
            or cfg.cooldown_minutes_after_loss < 0
        ):
            raise ValueError("Invalid cooldown_minutes_after_loss")
        self._instrument_state: Dict[str, InstrumentRiskState] = {}

    @staticmethod
    def _reject(code: RiskDecisionCode, reason: str) -> RiskDecision:
        return RiskDecision(accept=False, code=code, reason=reason)

    def evaluate_candidate(
        self, candidate: TradeCandidate, open_positions: List[OpenPositionRisk]
    ) -> RiskDecision:
        """Pure decision over a complete, caller-verified reservation snapshot."""
        cfg = self.config
        try:
            if not isinstance(candidate.instrument, str) or not candidate.instrument:
                raise ValueError("Missing instrument")
            if candidate.direction not in ("LONG", "SHORT"):
                raise ValueError("Invalid direction")
            if candidate.grade not in (Grade.A, Grade.B, Grade.C, Grade.D):
                raise ValueError("Invalid grade")
            now = require_utc(candidate.timestamp)
            score = Decimal(str(candidate.score))
            if not score.is_finite() or not 0 <= score <= 100:
                raise ValueError("Invalid signal score")
            requested = _positive_decimal(candidate.requested_risk_pct, "requested risk")
            if requested > 100:
                raise ValueError("Requested risk exceeds 100%")
            if not isinstance(open_positions, (list, tuple)):
                raise ValueError("Exposure snapshot is not a collection")
            positions = list(open_positions)
            ids = set()
            total = Decimal("0")
            for pos in positions:
                if (
                    not isinstance(pos.trade_id, str) or not pos.trade_id
                    or pos.trade_id in ids
                    or not isinstance(pos.instrument, str) or not pos.instrument
                    or pos.direction not in ("LONG", "SHORT")
                    or pos.grade not in (Grade.A, Grade.B)
                ):
                    raise ValueError("Malformed or duplicate open/reserved exposure")
                require_utc(pos.opened_at)
                ids.add(pos.trade_id)
                total += _positive_decimal(pos.risk_pct, "reserved risk")
        except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
            return self._reject(RiskDecisionCode.REJECT_INVALID_INPUT, str(exc))

        if candidate.grade not in (Grade.A, Grade.B):
            return self._reject(RiskDecisionCode.REJECT_GRADE, "Only Grade A/B permitted")
        grade_cap = (
            _positive_decimal(cfg.risk_pct_grade_a, "A cap")
            if candidate.grade == Grade.A
            else _positive_decimal(cfg.risk_pct_grade_b, "B cap")
        )
        if requested > grade_cap:
            return self._reject(
                RiskDecisionCode.REJECT_GRADE_RISK, "Requested risk exceeds grade cap"
            )
        if self._is_in_cooldown(candidate.instrument, now):
            return self._reject(
                RiskDecisionCode.REJECT_INSTRUMENT_COOLDOWN, "Loss cooldown active"
            )
        if total + requested > _positive_decimal(cfg.max_total_risk_pct, "risk cap"):
            return self._reject(
                RiskDecisionCode.REJECT_TOTAL_RISK, "Reserved exposure exceeds total cap"
            )
        if len(positions) >= cfg.max_open_trades_total:
            return self._reject(
                RiskDecisionCode.REJECT_TOO_MANY_TOTAL, "Maximum total reservations"
            )
        count_a = sum(pos.grade == Grade.A for pos in positions)
        count_b = sum(pos.grade == Grade.B for pos in positions)
        if candidate.grade == Grade.A and count_a >= cfg.max_open_trades_grade_a:
            return self._reject(RiskDecisionCode.REJECT_TOO_MANY_A, "Maximum Grade A trades")
        if candidate.grade == Grade.B and count_b >= cfg.max_open_trades_grade_b:
            return self._reject(RiskDecisionCode.REJECT_TOO_MANY_B, "Maximum Grade B trades")
        same = [pos for pos in positions if pos.instrument == candidate.instrument]
        if not cfg.allow_hedging_same_instrument and any(
            pos.direction != candidate.direction for pos in same
        ):
            return self._reject(RiskDecisionCode.REJECT_HEDGING, "Hedging forbidden")
        if not cfg.allow_multiple_trades_same_instrument and same:
            return self._reject(
                RiskDecisionCode.REJECT_SAME_INSTRUMENT, "Duplicate instrument"
            )
        return RiskDecision(True, RiskDecisionCode.ACCEPTED, "Risk checks passed")

    def register_loss(self, instrument: str, when: Optional[datetime] = None) -> None:
        at = require_utc(when or utc_now())
        if not instrument:
            raise ValueError("Missing instrument for cooldown")
        self._instrument_state[instrument] = InstrumentRiskState(last_loss_at=at)

    def set_instrument_state(self, instrument: str, state: InstrumentRiskState) -> None:
        if not instrument or not isinstance(state, InstrumentRiskState):
            raise ValueError("Invalid instrument cooldown state")
        if state.last_loss_at is not None:
            state.last_loss_at = require_utc(state.last_loss_at)
        self._instrument_state[instrument] = state

    def get_instrument_state(self, instrument: str) -> InstrumentRiskState:
        return self._instrument_state.setdefault(instrument, InstrumentRiskState())

    def _is_in_cooldown(self, instrument: str, now: datetime) -> bool:
        now = require_utc(now)
        state = self._instrument_state.get(instrument)
        if state is None or state.last_loss_at is None:
            return False
        return now < require_utc(state.last_loss_at) + timedelta(
            minutes=self.config.cooldown_minutes_after_loss
        )
