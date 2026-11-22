"""Category 2: deterministic, read-only risk sizing and trade construction.

The output is a preview, not an authorized broker order. All monetary
calculations use Decimal. Unknown broker state or conversion means no plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from typing import Mapping, Optional, Sequence

from risk.risk_engine import (
    Grade, OpenPositionRisk, RiskConfig, RiskEngine, TradeCandidate,
)
from utils.time_tools import require_utc


class PlanRejected(ValueError):
    """No executable plan can be established from verified evidence."""


def number(value, name: str, *, positive: bool = False, zero_ok: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PlanRejected(f"{name}: invalid decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PlanRejected(f"{name}: invalid decimal") from None
    if not result.is_finite() or (positive and result <= 0) or (
        not positive and not zero_ok and result == 0
    ):
        raise PlanRejected(f"{name}: non-finite or outside allowed range")
    return result


def advance_loss_streak(previous: int, net_pl_home) -> int:
    """Only a verified net profit resets the consecutive-loss risk counter.

    Break-even (exactly zero after costs) is neutral: it neither adds a
    loss nor erases earlier losses. Shared by live-risk and offline replay.
    """
    if type(previous) is not int or previous < 0:
        raise PlanRejected("Invalid prior loss streak")
    net = number(net_pl_home, "verified trade net")
    if net < 0:
        return previous + 1
    if net > 0:
        return 0
    return previous


@dataclass(frozen=True)
class RiskPolicy:
    """Explicitly separate single-trade risk from total exposure."""
    grade_a_pct: Decimal = Decimal("1.0")
    grade_b_pct: Decimal = Decimal("0.5")
    max_total_risk_pct: Decimal = Decimal("1.0")
    max_instrument_risk_pct: Decimal = Decimal("1.0")
    max_currency_risk_pct: Decimal = Decimal("1.0")
    max_daily_loss_pct: Decimal = Decimal("2.0")
    max_trades_grade_a: int = 2
    max_trades_grade_b: int = 3
    max_active_trades: int = 3
    max_consecutive_losses: int = 3
    max_drawdown_pct: Decimal = Decimal("5.0")
    reward_to_risk: Decimal = Decimal("3")
    atr_stop_multiple: Decimal = Decimal("1.5")
    minimum_stop_pips: Decimal = Decimal("3")
    max_margin_fraction: Decimal = Decimal("0.5")
    max_quote_age_seconds: int = 20
    block_hedging: bool = True
    block_duplicate_instrument: bool = True

    def __post_init__(self):
        for name in (
            "grade_a_pct", "grade_b_pct", "max_total_risk_pct",
            "max_instrument_risk_pct", "max_currency_risk_pct",
            "max_drawdown_pct", "max_daily_loss_pct",
            "reward_to_risk", "atr_stop_multiple", "max_margin_fraction",
        ):
            value = number(getattr(self, name), name, positive=True)
            object.__setattr__(self, name, value)
            if name.endswith("_pct") and value > 100:
                raise PlanRejected(f"{name}: must not exceed 100%")
        object.__setattr__(self, "minimum_stop_pips", number(
            self.minimum_stop_pips, "minimum_stop_pips"
        ))
        if self.grade_b_pct > self.grade_a_pct:
            raise PlanRejected("Grade B risk must not exceed Grade A risk")
        if self.grade_a_pct > self.max_total_risk_pct:
            raise PlanRejected("Single-trade A risk exceeds total risk cap")
        if self.grade_a_pct > self.max_instrument_risk_pct:
            raise PlanRejected("Grade A exceeds instrument concentration cap")
        if not 0 < self.max_margin_fraction <= 1:
            raise PlanRejected("Invalid margin budget fraction")
        if number(self.minimum_stop_pips, "minimum_stop_pips") < 0:
            raise PlanRejected("Negative minimum stop distance")
        for name in (
            "max_trades_grade_a", "max_trades_grade_b",
            "max_active_trades", "max_consecutive_losses", "max_quote_age_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise PlanRejected(f"{name}: invalid positive integer")


@dataclass(frozen=True)
class AccountFunds:
    account_id: str
    currency: str
    balance: Decimal
    nav: Decimal
    margin_available: Decimal
    margin_used: Decimal
    broker_open_count: int
    broker_pending_count: int
    transaction_id: str

    def __post_init__(self):
        if not self.account_id or not self.transaction_id:
            raise PlanRejected("Unidentified account snapshot")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha():
            raise PlanRejected("Invalid account currency")
        for name in ("balance", "nav"):
            object.__setattr__(
                self, name, number(getattr(self, name), name, positive=True)
            )
        for name in ("margin_available", "margin_used"):
            value = number(getattr(self, name), name)
            if value < 0:
                raise PlanRejected(f"{name}: negative")
            object.__setattr__(self, name, value)
        for name in ("broker_open_count", "broker_pending_count"):
            x = getattr(self, name)
            if not isinstance(x, int) or isinstance(x, bool) or x < 0:
                raise PlanRejected(f"{name}: invalid")
        if self.margin_available > self.nav * Decimal("100"):
            raise PlanRejected("Implausible available margin")


@dataclass(frozen=True)
class InstrumentRules:
    instrument: str
    kind: str
    pip_location: int
    display_precision: int
    units_precision: int
    minimum_trade_size: Decimal
    maximum_order_units: Decimal
    maximum_position_size: Decimal
    margin_rate: Decimal

    def __post_init__(self):
        if not self.instrument or "_" not in self.instrument:
            raise PlanRejected("Invalid instrument symbol")
        if self.kind not in ("CURRENCY", "METAL", "CFD"):
            raise PlanRejected("Unknown instrument contract type")
        for name, lo, hi in (
            ("pip_location", -10, 5),
            ("display_precision", 0, 12),
            ("units_precision", 0, 8),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise PlanRejected(f"{name}: invalid")
        for name in ("minimum_trade_size", "maximum_order_units", "margin_rate"):
            object.__setattr__(
                self, name, number(getattr(self, name), name, positive=True)
            )
        max_position = number(self.maximum_position_size, "maximum_position_size")
        if max_position < 0:
            raise PlanRejected("Negative max position size")
        object.__setattr__(self, "maximum_position_size", max_position)
        if self.margin_rate > 1:
            raise PlanRejected("Invalid instrument margin rate")
        if self.minimum_trade_size > self.maximum_order_units:
            raise PlanRejected("Minimum trade exceeds maximum order units")

    @property
    def pip_size(self):
        return Decimal("10") ** self.pip_location

    @property
    def price_step(self):
        return Decimal("1").scaleb(-self.display_precision)

    @property
    def unit_step(self):
        return Decimal("1").scaleb(-self.units_precision)

    @property
    def base_currency(self):
        return self.instrument.split("_", 1)[0]

    @property
    def quote_currency(self):
        return self.instrument.split("_", 1)[1]


@dataclass(frozen=True)
class QuoteConversions:
    instrument: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    quote_loss_to_home: Decimal
    base_value_to_home: Decimal

    def __post_init__(self):
        if not self.instrument:
            raise PlanRejected("Missing quote instrument")
        bid = number(self.bid, "bid", positive=True)
        ask = number(self.ask, "ask", positive=True)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        if bid >= ask:
            raise PlanRejected("Unusable bid/ask spread")
        require_utc(self.timestamp)
        object.__setattr__(
            self, "quote_loss_to_home",
            number(self.quote_loss_to_home, "loss conversion", positive=True)
        )
        object.__setattr__(
            self, "base_value_to_home",
            number(self.base_value_to_home, "position conversion", positive=True)
        )


@dataclass(frozen=True)
class ExposureReservation:
    reservation_id: str
    instrument: str
    direction: str
    grade: str
    risk_pct: Decimal
    status: str   # OPEN, PENDING, OUTCOME_UNKNOWN
    reserved_margin_home: Optional[Decimal] = None

    def __post_init__(self):
        if not self.reservation_id or not self.instrument:
            raise PlanRejected("Unidentified exposure reservation")
        if self.instrument.count("_") != 1 or not all(self.instrument.split("_")):
            raise PlanRejected("Invalid reservation instrument format")
        if self.direction not in ("LONG", "SHORT") or self.grade not in ("A", "B"):
            raise PlanRejected("Invalid reserved exposure attributes")
        if self.status not in ("OPEN", "PENDING", "OUTCOME_UNKNOWN"):
            raise PlanRejected("Unknown reservation state")
        object.__setattr__(
            self, "risk_pct", number(self.risk_pct, "reserved risk", positive=True)
        )
        if self.status in ("PENDING", "OUTCOME_UNKNOWN"):
            object.__setattr__(
                self, "reserved_margin_home",
                number(self.reserved_margin_home, "unsettled margin", positive=True)
            )
        elif self.reserved_margin_home is not None:
            value = number(self.reserved_margin_home, "open margin")
            if value < 0:
                raise PlanRejected("Negative open margin")
            object.__setattr__(self, "reserved_margin_home", value)


@dataclass(frozen=True)
class ExposureBook:
    """A verified aggregate: no omissions of existing/pending/unknown intent."""
    account_id: str
    reservations: tuple[ExposureReservation, ...] = ()
    verified: bool = False
    uncertain_outcomes_accounted: bool = False

    def __post_init__(self):
        if not isinstance(self.account_id, str) or not self.account_id:
            raise PlanRejected("Unidentified exposure account")
        if not isinstance(self.reservations, (tuple, list)) or not all(
            isinstance(r, ExposureReservation) for r in self.reservations
        ):
            raise PlanRejected("Malformed exposure reservations")
        object.__setattr__(self, "reservations", tuple(self.reservations))
        if len({r.reservation_id for r in self.reservations}) != len(self.reservations):
            raise PlanRejected("Duplicate exposure reservation")


@dataclass(frozen=True)
class LossState:
    """History validation/persistence is delegated to Category 4."""
    peak_nav: Decimal
    consecutive_losses: int
    verified: bool = False
    cooldown_until_by_instrument: Mapping[str, datetime] = field(default_factory=dict)
    day_start_nav: Optional[Decimal] = None
    daily_realized_loss_home: Optional[Decimal] = None

    def __post_init__(self):
        object.__setattr__(
            self, "peak_nav", number(self.peak_nav, "peak_nav", positive=True)
        )
        if self.verified and (
            self.day_start_nav is None or self.daily_realized_loss_home is None
        ):
            raise PlanRejected("Verified daily loss state missing")
        if self.day_start_nav is not None:
            object.__setattr__(
                self, "day_start_nav",
                number(self.day_start_nav, "day_start_nav", positive=True)
            )
        if self.daily_realized_loss_home is not None:
            loss = number(self.daily_realized_loss_home, "daily_realized_loss_home")
            if loss < 0:
                raise PlanRejected("Daily realized loss must be nonnegative")
            object.__setattr__(self, "daily_realized_loss_home", loss)
        if isinstance(self.consecutive_losses, bool) or (
            not isinstance(self.consecutive_losses, int)
            or self.consecutive_losses < 0
        ):
            raise PlanRejected("Invalid loss streak")
        for time_value in self.cooldown_until_by_instrument.values():
            require_utc(time_value)


@dataclass(frozen=True)
class TradePlan:
    """Read-only proposed order parameters. Never authorize broker mutation."""
    instrument: str
    direction: str
    grade: str
    units: Decimal
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    risk_budget_home: Decimal
    computed_risk_home: Decimal
    computed_risk_pct: Decimal
    reserved_risk_pct: Decimal
    estimated_margin_home: Decimal
    reward_to_risk: Decimal
    account_currency: str
    quote_timestamp: datetime
    stop_basis: str = "ATR"
    preview_only: bool = True


class TradePlanner:
    def __init__(self, policy: Optional[RiskPolicy] = None):
        self.policy = policy or RiskPolicy()

    def plan(
        self, signal, atr, account: AccountFunds, instrument: InstrumentRules,
        quote: QuoteConversions, exposure: ExposureBook, losses: LossState,
        *, now: datetime, structure_level=None
    ) -> TradePlan:
        """P5/P9–P18: build a fail-closed, unit-quantized proposal."""
        policy = self.policy
        now = require_utc(now)
        if not exposure.verified or not exposure.uncertain_outcomes_accounted:
            raise PlanRejected("Exposure and unresolved requests not reconciled")
        if not losses.verified:
            raise PlanRejected("Unverified loss/drawdown state")
        if exposure.account_id != account.account_id:
            raise PlanRejected("Exposure belongs to a different account")
        if instrument.instrument != signal.instrument or quote.instrument != signal.instrument:
            raise PlanRejected("Signal/instrument/quote mismatch")
        if instrument.kind not in ("CURRENCY", "METAL"):
            raise PlanRejected("Unknown contract multiplier (CFD sizing blocked)")
        if signal.direction not in ("LONG", "SHORT") or signal.grade not in ("A", "B"):
            raise PlanRejected("Direction or grade rejected")
        if getattr(signal, "timestamp", now) is not None:
            require_utc(getattr(signal, "timestamp", now))
        if quote.timestamp > now + timedelta(seconds=2) or (
            now - require_utc(quote.timestamp)
        ).total_seconds() > policy.max_quote_age_seconds:
            raise PlanRejected("Stale or future-dated price")
        if account.broker_open_count != sum(
            r.status == "OPEN" for r in exposure.reservations
        ):
            raise PlanRejected("Open broker trades not fully reserved")
        if account.broker_pending_count > sum(
            r.status == "PENDING" for r in exposure.reservations
        ):
            raise PlanRejected("Pending broker orders not fully reserved")
        if account.margin_available <= 0:
            raise PlanRejected("No available margin")
        if losses.consecutive_losses >= policy.max_consecutive_losses:
            raise PlanRejected("Loss-streak entry lock")
        if losses.day_start_nav is None or losses.daily_realized_loss_home is None:
            raise PlanRejected("Unknown daily loss state")
        daily_loss_pct = (
            losses.daily_realized_loss_home / losses.day_start_nav * Decimal("100")
        )
        if daily_loss_pct >= policy.max_daily_loss_pct:
            raise PlanRejected("Daily realized-loss entry lock")
        peak = number(losses.peak_nav, "peak_nav", positive=True)
        drawdown_pct = max(Decimal("0"), (peak - account.nav) / peak * 100)
        if drawdown_pct >= policy.max_drawdown_pct:
            raise PlanRejected("Drawdown entry lock")
        cooldown = losses.cooldown_until_by_instrument.get(signal.instrument)
        if cooldown is not None and now < require_utc(cooldown):
            raise PlanRejected("Instrument loss cooldown active")

        reservations = exposure.reservations
        if len(reservations) >= policy.max_active_trades:
            raise PlanRejected("Maximum total trades and pending intents")
        existing_same = [r for r in reservations if r.instrument == signal.instrument]
        if policy.block_duplicate_instrument and existing_same:
            raise PlanRejected("Duplicate instrument/unknown order on instrument")
        if policy.block_hedging and any(
            r.direction != signal.direction for r in existing_same
        ):
            raise PlanRejected("Hedging not allowed")
        if sum(r.grade == signal.grade for r in reservations) >= (
            policy.max_trades_grade_a if signal.grade == "A"
            else policy.max_trades_grade_b
        ):
            raise PlanRejected("Grade trade limit")

        grade_pct = policy.grade_a_pct if signal.grade == "A" else policy.grade_b_pct
        existing_pct = sum((r.risk_pct for r in reservations), Decimal("0"))
        instrument_pct = sum(
            (r.risk_pct for r in existing_same), Decimal("0")
        )
        if existing_pct + grade_pct > policy.max_total_risk_pct:
            raise PlanRejected("Total reserved risk cap")
        if instrument_pct + grade_pct > policy.max_instrument_risk_pct:
            raise PlanRejected("Instrument risk concentration")
        # Conservative correlated-currency exposure: count any shared currency,
        # without making unverified netting assumptions about BUY/SELL.
        candidate_currencies = set(instrument.instrument.split("_"))
        for currency in candidate_currencies:
            reserved_currency_pct = sum(
                (
                    r.risk_pct for r in reservations
                    if currency in r.instrument.split("_")
                ),
                Decimal("0"),
            )
            if reserved_currency_pct + grade_pct > policy.max_currency_risk_pct:
                raise PlanRejected(f"Currency risk concentration: {currency}")

        # Retain the existing RiskEngine public decision contract.
        risk = RiskEngine(RiskConfig(
            max_total_risk_pct=float(policy.max_total_risk_pct),
            risk_pct_grade_a=float(policy.grade_a_pct),
            risk_pct_grade_b=float(policy.grade_b_pct),
            max_open_trades_grade_a=policy.max_trades_grade_a,
            max_open_trades_grade_b=policy.max_trades_grade_b,
            allow_multiple_trades_same_instrument=not policy.block_duplicate_instrument,
            allow_hedging_same_instrument=not policy.block_hedging,
        ))
        candidate = TradeCandidate(
            instrument=signal.instrument, direction=signal.direction,
            grade=Grade(signal.grade), score=number(signal.score, "score"),
            requested_risk_pct=grade_pct, timestamp=now,
        )
        positions = [
            OpenPositionRisk(
                trade_id=r.reservation_id, instrument=r.instrument,
                direction=r.direction, grade=Grade(r.grade), risk_pct=r.risk_pct,
                opened_at=now,
            ) for r in reservations
        ]
        decision = risk.evaluate_candidate(candidate, positions)
        if not decision.accept:
            raise PlanRejected(f"Risk engine rejected: {decision.code.value}")

        price = quote.ask if signal.direction == "LONG" else quote.bid
        atr_value = number(atr, "ATR", positive=True)
        distance = max(
            atr_value * policy.atr_stop_multiple,
            instrument.pip_size * policy.minimum_stop_pips,
        )
        stop_basis = "ATR"
        if structure_level is not None:
            boundary = number(structure_level, "structure_level", positive=True)
            # The supplied boundary is a separately verified support/resistance
            # level, not a level inferred from the current incomplete candle.
            if signal.direction == "LONG":
                if boundary >= price:
                    raise PlanRejected("LONG structure support must be below entry")
                distance = max(distance, price - boundary + instrument.pip_size)
            else:
                if boundary <= price:
                    raise PlanRejected("SHORT structure resistance must be above entry")
                distance = max(distance, boundary - price + instrument.pip_size)
            stop_basis = "ATR+STRUCTURE"
        step = instrument.price_step
        if signal.direction == "LONG":
            stop = (price - distance).quantize(step, rounding=ROUND_FLOOR)
            if stop <= 0:
                raise PlanRejected("Nonpositive stop")
            risk_distance = price - stop
            target = (price + risk_distance * policy.reward_to_risk).quantize(
                step, rounding=ROUND_CEILING
            )
        else:
            stop = (price + distance).quantize(step, rounding=ROUND_CEILING)
            risk_distance = stop - price
            target = (price - risk_distance * policy.reward_to_risk).quantize(
                step, rounding=ROUND_FLOOR
            )
            if target <= 0:
                raise PlanRejected("Nonpositive target")
        if risk_distance <= 0 or risk_distance < instrument.pip_size * policy.minimum_stop_pips:
            raise PlanRejected("Invalid stop/pip spacing")
        actual_rr = (abs(target - price) / risk_distance)
        if actual_rr < policy.reward_to_risk:
            raise PlanRejected("Rounded reward/risk ratio below policy")

        # Risk capital uses the lower of balance and NAV, never broker credit.
        capital = min(account.balance, account.nav)
        budget = capital * grade_pct / Decimal("100")
        per_unit_loss = risk_distance * quote.quote_loss_to_home
        per_unit_margin = instrument.margin_rate * quote.base_value_to_home
        if per_unit_loss <= 0 or per_unit_margin <= 0:
            raise PlanRejected("Invalid loss or margin conversion")
        units_by_risk = budget / per_unit_loss
        # Pending and outcome-unknown intents might not consume the margin
        # currently reported by OANDA. Reserve their full estimated margin.
        unsettled_margin = sum(
            (
                r.reserved_margin_home for r in reservations
                if r.status in ("PENDING", "OUTCOME_UNKNOWN")
            ),
            Decimal("0"),
        )
        usable_margin = account.margin_available - unsettled_margin
        if usable_margin <= 0:
            raise PlanRejected("Unsettled intents exhaust available margin")
        units_by_margin = (
            usable_margin * policy.max_margin_fraction
        ) / per_unit_margin
        size_cap = min(
            units_by_risk, units_by_margin, instrument.maximum_order_units
        )
        # OANDA samples use 0 for an instrument without a position cap.
        if instrument.maximum_position_size > 0:
            size_cap = min(size_cap, instrument.maximum_position_size)
        units = size_cap.quantize(instrument.unit_step, rounding=ROUND_DOWN)
        if units < instrument.minimum_trade_size or units <= 0:
            raise PlanRejected("Units smaller than broker's minimum tradable size")
        actual_loss = units * per_unit_loss
        margin = units * per_unit_margin
        risk_pct = actual_loss / capital * Decimal("100")
        if actual_loss > budget or risk_pct + existing_pct > policy.max_total_risk_pct:
            raise PlanRejected("Post-rounding capital risk cap violated")
        if margin > usable_margin * policy.max_margin_fraction:
            raise PlanRejected("Post-rounding margin cap violated")
        signed_units = units if signal.direction == "LONG" else -units
        return TradePlan(
            instrument=signal.instrument, direction=signal.direction,
            grade=signal.grade, units=signed_units, entry=price,
            stop_loss=stop, take_profit=target, risk_budget_home=budget,
            computed_risk_home=actual_loss, computed_risk_pct=risk_pct,
            reserved_risk_pct=grade_pct, estimated_margin_home=margin,
            reward_to_risk=actual_rr, account_currency=account.currency,
            quote_timestamp=quote.timestamp, stop_basis=stop_basis,
        )
