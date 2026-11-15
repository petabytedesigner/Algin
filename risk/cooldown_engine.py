"""UTC-safe cooldown helper retained for compatible external imports.

RiskEngine is the authoritative risk decision point in the new main pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from utils.time_tools import require_utc, utc_now


@dataclass
class CooldownConfig:
    minutes_after_loss: int = 30


@dataclass
class InstrumentCooldownState:
    last_loss_at: Optional[datetime] = None


class CooldownEngine:
    def __init__(self, config: Optional[CooldownConfig] = None):
        self.config = config or CooldownConfig()
        self._state: dict[str, InstrumentCooldownState] = {}

    def register_loss(self, instrument: str, when: Optional[datetime] = None) -> None:
        self._state[instrument] = InstrumentCooldownState(require_utc(when or utc_now()))

    def is_in_cooldown(self, instrument: str, now: datetime) -> bool:
        now = require_utc(now)
        state = self._state.get(instrument)
        if not state or not state.last_loss_at:
            return False
        return now < require_utc(state.last_loss_at) + timedelta(
            minutes=self.config.minutes_after_loss
        )

    def load_state(self, instrument: str, state: InstrumentCooldownState) -> None:
        if state.last_loss_at is not None:
            state.last_loss_at = require_utc(state.last_loss_at)
        self._state[instrument] = state

    def get_state(self, instrument: str) -> InstrumentCooldownState:
        return self._state.setdefault(instrument, InstrumentCooldownState())
