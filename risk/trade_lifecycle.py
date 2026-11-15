"""Validated persistent trade lifecycle and pure directional R calculations.

Category 4 never infers initial risk from a moved stop. It preserves original
entry, initial stop, signed units, grade, journal ID and management phase state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
import re

from risk.trade_plan import InstrumentRules, number
from utils.time_tools import iso_utc, parse_utc, require_utc


class LifecycleError(ValueError):
    """Trade ownership, protection, phase or confirmed result is not reliable."""


_NUMERIC_ID = re.compile(r"^[0-9]{1,50}$")
_PHASES = frozenset({"NONE", "INTENT", "UNKNOWN", "CONFIRMED"})
_GRADE = frozenset({"A", "B"})


def _positive(v, name):
    try:
        return number(v, name, positive=True)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


def _signed(v, name):
    try:
        return number(v, name, zero_ok=False)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    attempt_id: str
    instrument: str
    direction: str
    grade: str
    entry_price: Decimal
    initial_stop: Decimal
    take_profit: Decimal
    initial_units: Decimal
    current_units: Decimal
    current_stop: Decimal
    risk_pct: Decimal
    opened_at: datetime
    be_state: str = "NONE"
    partial_phases: dict = field(default_factory=dict)
    closed: bool = False
    close_transaction_ids: tuple = ()
    net_pl_home: Decimal = Decimal("0")
    result_counted: bool = False
    closed_at: datetime | None = None

    def __post_init__(self):
        if not isinstance(self.trade_id, str) or not _NUMERIC_ID.fullmatch(self.trade_id):
            raise LifecycleError("Invalid trade ID")
        if not isinstance(self.attempt_id, str) or not self.attempt_id.startswith("algin-"):
            raise LifecycleError("Unknown trade origin")
        if not isinstance(self.instrument, str) or self.instrument.count("_") != 1:
            raise LifecycleError("Malformed instrument")
        if self.direction not in ("LONG", "SHORT") or self.grade not in _GRADE:
            raise LifecycleError("Missing trade direction/grade")
        for field_name in ("entry_price", "initial_stop", "take_profit",
                           "risk_pct"):
            object.__setattr__(
                self, field_name, _positive(getattr(self, field_name), field_name)
            )
        for field_name in ("initial_units", "current_units", "current_stop"):
            val = getattr(self, field_name)
            if field_name == "current_units":
                val = number(val, field_name)
            else:
                val = _positive(val, field_name) if field_name == "current_stop" else _signed(val, field_name)
            object.__setattr__(self, field_name, val)
        direction = 1 if self.direction == "LONG" else -1
        if direction * self.initial_units <= 0:
            raise LifecycleError("Initial units disagree with trade direction")
        if self.closed:
            if self.current_units != 0 or self.closed_at is None:
                raise LifecycleError("Closed trade still has units or lacks close time")
        elif direction * self.current_units <= 0:
            raise LifecycleError("Open trade units disagree with trade direction")
        if abs(self.current_units) > abs(self.initial_units):
            raise LifecycleError("Trade units exceed original position")
        if self.direction == "LONG":
            if not self.initial_stop < self.entry_price < self.take_profit:
                raise LifecycleError("Bad original LONG protections")
            if self.current_stop < self.initial_stop:
                raise LifecycleError("LONG stop moved adversely")
        else:
            if not self.take_profit < self.entry_price < self.initial_stop:
                raise LifecycleError("Bad original SHORT protections")
            if self.current_stop > self.initial_stop:
                raise LifecycleError("SHORT stop moved adversely")
        require_utc(self.opened_at)
        if self.closed_at is not None:
            require_utc(self.closed_at)
        if self.be_state not in _PHASES or not isinstance(self.partial_phases, dict):
            raise LifecycleError("Malformed management phase state")
        for name, value in self.partial_phases.items():
            if (not isinstance(name, str) or not name
                or not isinstance(value, dict) or value.get("state") not in _PHASES
                or not isinstance(value.get("units"), str)):
                raise LifecycleError("Invalid partial-close phase")
            _positive(value["units"], "partial units")
        if not isinstance(self.close_transaction_ids, tuple) or len(set(
            self.close_transaction_ids
        )) != len(self.close_transaction_ids):
            raise LifecycleError("Duplicate close transaction ID")
        if any(not isinstance(v, str) or not _NUMERIC_ID.fullmatch(v)
               for v in self.close_transaction_ids):
            raise LifecycleError("Malformed close transaction")
        if self.result_counted and not self.closed:
            raise LifecycleError("Counted result for open trade")
        object.__setattr__(self, "net_pl_home", number(self.net_pl_home, "net_pl_home"))

    @property
    def initial_risk_distance(self):
        return abs(self.entry_price - self.initial_stop)

    def favorable_r(self, *, bid, ask):
        """LONG exits at bid; SHORT exits at ask. Never absolute price move."""
        bid = _positive(bid, "bid")
        ask = _positive(ask, "ask")
        if bid > ask or self.initial_risk_distance <= 0:
            raise LifecycleError("Unusable executable broker quote")
        mark = bid if self.direction == "LONG" else ask
        movement = (
            mark - self.entry_price if self.direction == "LONG"
            else self.entry_price - mark
        )
        return movement / self.initial_risk_distance

    def break_even_target(self, *, bid, ask, trigger_r=Decimal("1")):
        if self.closed or self.be_state != "NONE":
            return None
        if self.favorable_r(bid=bid, ask=ask) < _positive(trigger_r, "trigger_R"):
            return None
        # Never request an adverse movement or a redundant stop amendment.
        target = self.entry_price
        if self.direction == "LONG" and target <= self.current_stop:
            return None
        if self.direction == "SHORT" and target >= self.current_stop:
            return None
        return target

    def partial_close_units(self, rules: InstrumentRules, *, bid, ask,
                            phase="phase1", trigger_r=Decimal("1"),
                            fraction=Decimal("0.5")):
        if self.closed or phase in self.partial_phases:
            return None
        if rules.instrument != self.instrument:
            raise LifecycleError("Broker instrument metadata mismatch")
        if self.favorable_r(bid=bid, ask=ask) < _positive(trigger_r, "trigger_R"):
            return None
        f = _positive(fraction, "partial fraction")
        if f >= 1:
            raise LifecycleError("Partial fraction must be strictly less than one")
        # Based on original magnitude, capped to remaining. OANDA close units
        # are always POSITIVE, even for a SHORT (negative trade units).
        amount = min(abs(self.initial_units) * f, abs(self.current_units))
        amount = amount.quantize(rules.unit_step, rounding=ROUND_DOWN)
        if amount <= 0 or amount >= abs(self.current_units):
            return None
        if (amount < rules.minimum_trade_size
            or abs(self.current_units) - amount < rules.minimum_trade_size):
            return None
        return amount

    def to_dict(self):
        return {
            "trade_id": self.trade_id, "attempt_id": self.attempt_id,
            "instrument": self.instrument, "direction": self.direction,
            "grade": self.grade,
            "entry_price": str(self.entry_price),
            "initial_stop": str(self.initial_stop),
            "take_profit": str(self.take_profit),
            "initial_units": str(self.initial_units),
            "current_units": str(self.current_units),
            "current_stop": str(self.current_stop),
            "risk_pct": str(self.risk_pct),
            "opened_at": iso_utc(self.opened_at),
            "be_state": self.be_state,
            "partial_phases": self.partial_phases,
            "closed": self.closed,
            "close_transaction_ids": list(self.close_transaction_ids),
            "net_pl_home": str(self.net_pl_home),
            "result_counted": self.result_counted,
            "closed_at": iso_utc(self.closed_at) if self.closed_at else None,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise LifecycleError("Invalid trade record")
        required = {
            "trade_id", "attempt_id", "instrument", "direction", "grade",
            "entry_price", "initial_stop", "take_profit", "initial_units",
            "current_units", "current_stop", "risk_pct", "opened_at",
            "be_state", "partial_phases", "closed", "close_transaction_ids",
            "net_pl_home", "result_counted", "closed_at"
        }
        if set(data) != required:
            raise LifecycleError("Missing/unknown trade lifecycle fields")
        return cls(
            trade_id=data["trade_id"], attempt_id=data["attempt_id"],
            instrument=data["instrument"], direction=data["direction"],
            grade=data["grade"], entry_price=data["entry_price"],
            initial_stop=data["initial_stop"], take_profit=data["take_profit"],
            initial_units=data["initial_units"], current_units=data["current_units"],
            current_stop=data["current_stop"], risk_pct=data["risk_pct"],
            opened_at=parse_utc(data["opened_at"]),
            be_state=data["be_state"],
            partial_phases=data["partial_phases"],
            closed=data["closed"], close_transaction_ids=tuple(data["close_transaction_ids"]),
            net_pl_home=data["net_pl_home"], result_counted=data["result_counted"],
            closed_at=parse_utc(data["closed_at"]) if data["closed_at"] else None
        )

    def with_stop_confirmed(self, new_stop):
        target = _positive(new_stop, "confirmed stop")
        if self.be_state not in ("INTENT", "UNKNOWN"):
            raise LifecycleError("No BE intent to confirm")
        if target != self.entry_price:
            raise LifecycleError("Unexpected broker BE stop")
        return replace(self, current_stop=target, be_state="CONFIRMED")

    def with_partial_confirmed(self, phase, close_units, remaining_signed):
        record = self.partial_phases.get(phase)
        if record is None or record["state"] not in ("INTENT", "UNKNOWN"):
            raise LifecycleError("No partial intent to confirm")
        amount = _positive(close_units, "confirmed close units")
        if amount != _positive(record["units"], "reserved close units"):
            raise LifecycleError("Partial close differs from intended units")
        remaining = number(remaining_signed, "remaining_signed")
        sign = 1 if self.direction == "LONG" else -1
        if sign * remaining <= 0 or abs(remaining) != abs(self.current_units) - amount:
            raise LifecycleError("Confirmed broker remainder mismatch")
        phases = dict(self.partial_phases)
        phases[phase] = {"state": "CONFIRMED", "units": str(amount)}
        return replace(self, current_units=remaining, partial_phases=phases)
