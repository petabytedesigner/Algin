from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

@dataclass
class CooldownConfig:
    minutes_after_loss: int = 30

@dataclass
class InstrumentCooldownState:
    last_loss_at: Optional[datetime] = None

class CooldownEngine:
    def __init__(self, config: CooldownConfig|None=None):
        self.config = config or CooldownConfig()
        self._state: dict[str, InstrumentCooldownState] = {}

    def register_loss(self, instrument: str, when: Optional[datetime]=None):
        when = when or datetime.now(timezone.utc)
        state = self._state.get(instrument) or InstrumentCooldownState()
        state.last_loss_at = when
        self._state[instrument] = state

    def is_in_cooldown(self, instrument: str, now: datetime) -> bool:
        state = self._state.get(instrument)
        if not state or not state.last_loss_at:
            return False
        now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
        last = state.last_loss_at.replace(tzinfo=timezone.utc) if state.last_loss_at.tzinfo is None else state.last_loss_at
        return now < last + timedelta(minutes=self.config.minutes_after_loss)

    def load_state(self, instrument: str, state: InstrumentCooldownState):
        self._state[instrument] = state

    def get_state(self, instrument: str) -> InstrumentCooldownState:
        st = self._state.get(instrument)
        if not st:
            st = InstrumentCooldownState()
            self._state[instrument] = st
        return st
