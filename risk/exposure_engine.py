from __future__ import annotations
from dataclasses import dataclass
from typing import List
from datetime import datetime

@dataclass
class ExposureConfig:
    max_total_risk_pct: float = 6.0

@dataclass
class Position:
    instrument: str
    risk_pct: float
    opened_at: datetime

class ExposureEngine:
    def __init__(self, config: ExposureConfig|None=None):
        self.config = config or ExposureConfig()

    def total_risk(self, positions: List[Position]) -> float:
        return sum(p.risk_pct for p in positions)

    def can_open(self, positions: List[Position], requested: float) -> bool:
        return (self.total_risk(positions) + requested) <= self.config.max_total_risk_pct
