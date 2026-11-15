"""Legacy exposure calculator, aligned to the Category-2 total risk cap.

This module has no proof of broker completeness. It is intentionally only an
advisory helper; actual trade planning uses ExposureBook reservations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from utils.time_tools import require_utc


def _risk(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid risk percent")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid risk percent") from None
    if not d.is_finite() or d <= 0 or d > 100:
        raise ValueError("Risk must be finite and positive")
    return d


@dataclass
class ExposureConfig:
    max_total_risk_pct: float = 1.0


@dataclass
class Position:
    instrument: str
    risk_pct: float
    opened_at: datetime


class ExposureEngine:
    """Read-only compatibility API. Does not authorize a new trade."""

    def __init__(self, config: Optional[ExposureConfig] = None):
        self.config = config or ExposureConfig()
        _risk(self.config.max_total_risk_pct)

    @staticmethod
    def _total_decimal(positions: List[Position]) -> Decimal:
        if not isinstance(positions, (list, tuple)):
            raise ValueError("Exposure list missing")
        total = Decimal("0")
        for position in positions:
            if not isinstance(position, Position) or not position.instrument:
                raise ValueError("Invalid position")
            require_utc(position.opened_at)
            total += _risk(position.risk_pct)
        return total

    def total_risk(self, positions: List[Position]) -> float:
        # Legacy return type: float. Trade decisions use _total_decimal.
        return float(self._total_decimal(positions))

    def can_open(self, positions: List[Position], requested: float) -> bool:
        try:
            request = _risk(requested)
            return (
                self._total_decimal(positions) + request
                <= _risk(self.config.max_total_risk_pct)
            )
        except (InvalidOperation, ValueError, TypeError):
            return False
