"""M15 historical research replay with production signal, sizing, preflight.

This is a deliberately limited, deterministic OFFLINE model. No broker HTTP,
no actual orders, no calibrated win-rate claim. OANDA-shaped M/B/A candles are
required: midpoint candles drive Strategy, bid/ask candles drive fills.
Unknown economic calendar, conversion or market evidence never becomes clear.

Important limits: one position at a time, realized-only NAV, synthetic margin
and immediate (assumed) fills within an explicit price bound. No broker ledger,
realistic intrabar ordering, order queue, variable liquidity or actual swaps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from collections import Counter
import copy

import config
from analytics.conservative_backtest import (
    BacktestError, FillCosts, dec, net_result, stop_first_bar,
)
from broker.execution_validation import (
    PreflightRejected, build_market_order, parse_market_quote,
)
from data.candle_validation import CandleDataError, parse_oanda_candles, validate_candles
from data.market_sessions import MarketSessions
from data.news_filter import NewsFilter
from execution.trade_coordinator import stable_signal_id
from risk.trade_lifecycle import TradeRecord
from risk.trade_plan import (
    AccountFunds, ExposureBook, InstrumentRules, LossState,
    PlanRejected, QuoteConversions, TradePlanner,
)
from strategy.strategy import Strategy
from utils.time_tools import iso_utc, parse_utc, require_utc


class ReplayError(ValueError):
    """Historical input or simulation assumptions are unverified."""


@dataclass(frozen=True)
class ReplayAssumptions:
    starting_equity: Decimal
    account_currency: str
    entry_slippage_pips: Decimal = Decimal("0")
    exit_slippage_pips: Decimal = Decimal("0")
    commission_home_per_unit_side: Decimal = Decimal("0")
    financing_home_per_unit_bar: Decimal = Decimal("0")
    management: str = "HOLD"  # or "BE_PARTIAL"
    source_kind: str = "SYNTHETIC"  # or "UNVERIFIED_EXPORT"

    def __post_init__(self):
        for k in ("starting_equity", "entry_slippage_pips",
                  "exit_slippage_pips", "commission_home_per_unit_side",
                  "financing_home_per_unit_bar"):
            v = dec(getattr(self, k), k)
            if (k == "starting_equity" and v <= 0) or (
                k != "starting_equity" and v < 0
            ):
                raise ReplayError("Invalid research price/cost assumption")
            object.__setattr__(self, k, v)
        if (not isinstance(self.account_currency, str)
            or len(self.account_currency) != 3
            or not self.account_currency.isalpha()
            or self.account_currency != self.account_currency.upper()
            or self.management not in ("HOLD", "BE_PARTIAL")
            or self.source_kind not in ("SYNTHETIC", "UNVERIFIED_EXPORT")):
            raise ReplayError("Unidentified account/management/research source")
        if self.entry_slippage_pips > dec(config.SLIPPAGE_ALLOWED, "slippage cap"):
            raise ReplayError("Entry slippage exceeds production priceBound policy")


@dataclass(frozen=True)
class ResearchTrade:
    trade_id: str
    setup_id: str
    instrument: str
    direction: str
    grade: str
    score: float
    signal_time: datetime
    opened_at: datetime
    closed_at: datetime
    entry: Decimal
    exit: Decimal
    stop: Decimal
    target: Decimal
    initial_units: Decimal
    gross_home: Decimal
    costs_home: Decimal
    net_home: Decimal
    net_r: Decimal
    close_reason: str
    partial_executed: bool
    be_executed: bool


@dataclass(frozen=True)
class ReplayResult:
    trades: tuple[ResearchTrade, ...]
    blocked: dict[str, int]
    unresolved: int
    source_kind: str
    mode: str
    evaluated_bars: int
    # Never assert that synthetic fills or unverified exports are actual.
    broker_verified: bool = False


@dataclass(frozen=True)
class _Bar:
    time: datetime
    mid: dict
    bid: dict
    ask: dict


@dataclass
class _Position:
    signal: object
    signal_time: datetime
    opened_at: datetime
    plan: object
    rules: InstrumentRules
    entry: Decimal
    units: Decimal
    remaining: Decimal
    stop: Decimal
    original_stop: Decimal
    target: Decimal
    initial_risk_home: Decimal
    gross: Decimal = Decimal("0")
    financing: Decimal = Decimal("0")
    partial_done: bool = False
    be_done: bool = False
    management_due: bool = False
    last_exit: Decimal | None = None


class _OfflineCalendar(NewsFilter):
    """No URL/config/static source: use only supplied historic snapshot."""
    def __init__(self, payload):
        super().__init__(
            ttl_minutes=config.NEWS_CACHE_TTL_MINUTES,
            max_age_minutes=config.NEWS_CACHE_MAX_AGE_MINUTES,
        )
        self.payload = copy.deepcopy(payload)

    def _fetch(self):
        return self.payload


def _side(raw, rules, *, label):
    if not isinstance(raw, dict):
        raise ReplayError("Missing historical " + label + " OHLC side")
    points = {}
    for name in ("o", "h", "l", "c"):
        if name not in raw:
            raise ReplayError("Missing historical OHLC price")
        try:
            x = dec(raw[name], name)
        except BacktestError as exc:
            raise ReplayError("Nonfinite or invalid executable OHLC") from exc
        if x <= 0 or x != x.quantize(rules.price_step):
            raise ReplayError("Invalid executable candle precision")
        points[name] = x
    if (points["l"] > points["h"]
        or not points["l"] <= points["o"] <= points["h"]
        or not points["l"] <= points["c"] <= points["h"]):
        raise ReplayError("Invalid executable OHLC ordering")
    return points


def _bars(payload, rules):
    if (not isinstance(payload, dict)
        or payload.get("instrument") != rules.instrument
        or payload.get("granularity") != "M15"):
        raise ReplayError("Wrong instrument or timeframe in research export")
    raw = payload.get("candles")
    if not isinstance(raw, list) or len(raw) < 52 or len(raw) > 100000:
        raise ReplayError("Insufficient/oversized M15 research export")
    if not all(isinstance(c, dict) and c.get("complete") is True for c in raw):
        raise ReplayError("Incomplete candle in replay history")
    try:
        mid = parse_oanda_candles(payload, instrument=rules.instrument)
    except (CandleDataError, ValueError, TypeError) as exc:
        raise ReplayError("Midpoint history fails production validation") from exc
    if len(mid) != len(raw):
        raise ReplayError("Midpoint timeline changed during normalization")
    out = []
    prev = None
    for item, midpoint in zip(raw, mid):
        t = parse_utc(midpoint["time"])
        if prev is not None and t <= prev:
            raise ReplayError("Unordered replay history")
        prev = t
        bid = _side(item.get("bid"), rules, label="bid")
        ask = _side(item.get("ask"), rules, label="ask")
        if bid["o"] >= ask["o"] or bid["c"] >= ask["c"]:
            raise ReplayError("Crossed or locked executable history")
        out.append(_Bar(t, midpoint, bid, ask))
    return out


def _facts(evidence, key):
    fact = evidence.get(key) if isinstance(evidence, dict) else None
    if not isinstance(fact, dict):
        raise ReplayError("Missing historical broker/conversion evidence")
    for k in ("loss_to_home", "gain_to_home", "base_value_to_home"):
        try:
            verified = dec(fact[k], k)
        except (KeyError, BacktestError) as exc:
            raise ReplayError("Missing or nonfinite historical conversion") from exc
        if verified <= 0:
            raise ReplayError("Nonpositive/missing historical conversion")
    if type(fact.get("broker_tradeable")) is not bool:
        raise ReplayError("Historical broker-tradeability not attested")
    return fact


def _quote(bar, instrument, rules, fact):
    payload = {"prices": [{
        "instrument": instrument,
        "tradeable": fact["broker_tradeable"],
        "time": iso_utc(bar.time),
        "bids": [{"price": str(bar.bid["o"])}],
        "asks": [{"price": str(bar.ask["o"])}],
    }]}
    return parse_market_quote(
        payload, instrument, rules, now=bar.time,
        max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
        max_spread_pips=dec(config.MAX_SPREAD_PIPS, "max spread"),
    )


def _record(pos, *, now):
    return TradeRecord(
        trade_id="1", attempt_id="algin-replay", instrument=pos.plan.instrument,
        direction=pos.plan.direction, grade=pos.plan.grade,
        entry_price=pos.entry, initial_stop=pos.original_stop,
        take_profit=pos.target,
        initial_units=(pos.units if pos.plan.direction == "LONG" else -pos.units),
        current_units=(pos.remaining if pos.plan.direction == "LONG"
                       else -pos.remaining),
        current_stop=pos.stop, risk_pct=pos.plan.computed_risk_pct,
        opened_at=pos.opened_at,
        be_state="CONFIRMED" if pos.be_done else "NONE",
        partial_phases=(
            {"phase1": {"state": "CONFIRMED",
                        "units": str(pos.units - pos.remaining)}}
            if pos.partial_done else {}
        ),
    )


def _exit_price(pos, raw_price, cause, *, assumed_exit_slippage):
    slip = assumed_exit_slippage * pos.rules.pip_size
    if pos.plan.direction == "LONG":
        # Target limit: no favorable gap improvement; stop: gap through or
        # worse at open, then adverse execution slippage.
        return raw_price if cause == "TP" else raw_price - slip
    return raw_price if cause == "TP" else raw_price + slip


def _realize(pos, amount, exit_price, fact):
    sign = Decimal("1") if pos.plan.direction == "LONG" else Decimal("-1")
    difference = (exit_price - pos.entry) * sign * amount
    conversion = dec(
        fact["gain_to_home"] if difference >= 0 else fact["loss_to_home"],
        "exit conversion",
    )
    pos.gross += difference * conversion
    pos.remaining -= amount
    pos.last_exit = exit_price


def replay(payload, rules: InstrumentRules, evidence, *,
           assumptions: ReplayAssumptions):
    """Offline replay using real Strategy/TradePlanner/OrderPreflight contracts.

    A source is never promoted to verified by a supplied boolean. Prices and
    calendar inputs are external *assumptions* unless separately audited.
    If an open position remains at the end, it is UNRESOLVED (not forced TP).
    """
    if not isinstance(rules, InstrumentRules) or rules.kind != "CURRENCY":
        raise ReplayError("Only verified FX metadata is modelled")
    if not isinstance(assumptions, ReplayAssumptions):
        raise ReplayError("Explicit research assumptions required")
    bars = _bars(payload, rules)
    if not isinstance(evidence, dict) or set(evidence) != {
        iso_utc(b.time) for b in bars
    }:
        raise ReplayError("Complete, exact per-bar evidence required")
    facts = [_facts(evidence, iso_utc(b.time)) for b in bars]
    # A genuine account-currency conversion cannot be inferred from OHLC.
    # Factors are supplied for each bar; research outputs stay UNVERIFIED.
    from main import build_risk_policy
    planner = TradePlanner(build_risk_policy())
    strategy = Strategy()
    sessions = MarketSessions()
    blocked = Counter()
    seen_setups = set()
    trades = []
    equity = assumptions.starting_equity
    peak = equity
    streak = 0
    daily_loss = Decimal("0")
    day = None
    day_start = equity
    cooldown = {}
    open_pos = None
    queued = None
    evaluated = 0

    for i, bar in enumerate(bars):
        fact = facts[i]
        if day != bar.time.date():
            day = bar.time.date()
            day_start = equity
            daily_loss = Decimal("0")
        # Entry occurs only at the following bar OPEN. No retest-bar fill.
        if queued is not None and open_pos is None:
            signal, atr, decision_time = queued
            queued = None
            if (bar.time - decision_time > timedelta(
                    minutes=config.SIGNAL_CANDLE_MAX_AGE_MINUTES
                ) or bar.time < decision_time):
                blocked["SIGNAL_STALE_BEFORE_ENTRY"] += 1
            elif not sessions.instrument_tradeable(
                rules.instrument, now=bar.time,
                broker_tradeable=fact["broker_tradeable"],
                require_session=config.ENABLE_SESSIONS,
            ):
                blocked["ENTRY_SESSION_OR_BROKER_BLOCK"] += 1
            else:
                try:
                    market = _quote(bar, rules.instrument, rules, fact)
                    conv = QuoteConversions(
                        instrument=rules.instrument, bid=market.bid,
                        ask=market.ask, timestamp=market.timestamp,
                        quote_loss_to_home=dec(fact["loss_to_home"], "loss factor"),
                        base_value_to_home=dec(
                            fact["base_value_to_home"], "position conversion"
                        ),
                    )
                    account = AccountFunds(
                        account_id="research-account", currency=assumptions.account_currency,
                        balance=equity, nav=equity, margin_available=equity,
                        margin_used=Decimal("0"), broker_open_count=0,
                        broker_pending_count=0, transaction_id=str(i+1),
                    )
                    risk = LossState(
                        peak_nav=peak, consecutive_losses=streak, verified=True,
                        cooldown_until_by_instrument=cooldown,
                        day_start_nav=day_start, daily_realized_loss_home=daily_loss,
                    )
                    plan = planner.plan(
                        signal, atr, account, rules, conv,
                        ExposureBook(
                            account_id=account.account_id,
                            verified=True, uncertain_outcomes_accounted=True,
                        ), risk, now=bar.time,
                    )
                    sid = stable_signal_id(
                        account.account_id, rules.instrument, signal.direction,
                        decision_time - timedelta(minutes=15),
                        setup_id=signal.setup_id,
                    )
                    prepared = build_market_order(
                        plan, rules, conv, market,
                        attempt_id="algin-replay-" + str(i),
                        signal_id=sid,
                        slippage_pips=assumptions.entry_slippage_pips,
                        minimum_stop_pips=dec(
                            config.MIN_DISTANCE_PIPS, "minimum stop"
                        ),
                    )
                    # Conservatively assume the worst permitted opening
                    # priceBound is actually paid. No claim of actual fill.
                    actual_entry = prepared.bound
                    open_pos = _Position(
                        signal, decision_time, bar.time, plan, rules,
                        actual_entry, abs(prepared.units), abs(prepared.units),
                        plan.stop_loss, plan.stop_loss, plan.take_profit,
                        prepared.worst_loss_home,
                    )
                except (PlanRejected, PreflightRejected, ValueError) as exc:
                    blocked["PLANNER_OR_PREFLIGHT_BLOCK"] += 1

        if open_pos is not None:
            pos = open_pos
            side = bar.bid if pos.plan.direction == "LONG" else bar.ask
            sign = 1 if pos.plan.direction == "LONG" else -1
            # A gap past a prior STOP/TP is resolved before BE/partial edits.
            if sign == 1 and side["o"] <= pos.stop:
                decision = "SL"
            elif sign == -1 and side["o"] >= pos.stop:
                decision = "SL"
            else:
                decision = None
            if decision is None and pos.management_due and (
                assumptions.management == "BE_PARTIAL"
            ):
                mark = _record(pos, now=bar.time)
                bid, ask = bar.bid["o"], bar.ask["o"]
                # Both management actions are conditional at the NEXT open,
                # after gap check. No intrabar high is used to trigger them.
                part = mark.partial_close_units(
                    rules, bid=bid, ask=ask, trigger_r=Decimal("1"),
                    fraction=Decimal("0.5"),
                )
                if part is not None:
                    partial_exit = _exit_price(
                        pos, side["o"], "PARTIAL",
                        assumed_exit_slippage=assumptions.exit_slippage_pips,
                    )
                    _realize(pos, part, partial_exit, fact)
                    pos.partial_done = True
                be = mark.break_even_target(
                    bid=bid, ask=ask, trigger_r=Decimal("1"),
                )
                if be is not None:
                    pos.stop = be
                    pos.be_done = True
                pos.management_due = False
            if decision is None:
                decision = stop_first_bar(
                    pos.plan.direction, low=side["l"], high=side["h"],
                    stop=pos.stop, target=pos.target,
                )
            if decision in ("SL", "TP"):
                if decision == "SL":
                    # Gap through a stop worsens the fill; do not assume the
                    # requested stop level always fills through a gap.
                    raw = (
                        min(pos.stop, side["o"]) if sign == 1
                        else max(pos.stop, side["o"])
                    )
                else:
                    raw = pos.target
                exit_px = _exit_price(
                    pos, raw, decision,
                    assumed_exit_slippage=assumptions.exit_slippage_pips,
                )
                _realize(pos, pos.remaining, exit_px, fact)
            else:
                if assumptions.management == "BE_PARTIAL":
                    latest = _record(pos, now=bar.time)
                    if (latest.break_even_target(
                            bid=bar.bid["c"], ask=bar.ask["c"]
                        ) is not None or latest.partial_close_units(
                            rules, bid=bar.bid["c"], ask=bar.ask["c"]
                        ) is not None):
                        pos.management_due = True
                # Per-bar funding rate is EXPLICIT, not broker-proven swap.
                pos.financing += (
                    pos.remaining * assumptions.financing_home_per_unit_bar
                )
            if decision in ("SL", "TP"):
                commission = (
                    pos.units * Decimal("2")
                    * assumptions.commission_home_per_unit_side
                )
                costs = FillCosts(
                    spread=Decimal("0"), slippage=Decimal("0"),
                    commission=commission, financing=pos.financing,
                )
                # Bid/ask and adverse fill slippage are already embedded in
                # gross; do not subtract them for a second time.
                net = net_result(
                    gross_pl=pos.gross, costs=costs,
                    gross_includes=("spread", "slippage"),
                )
                result = ResearchTrade(
                    trade_id="research-" + str(len(trades)+1),
                    setup_id=pos.signal.setup_id or "LEGACY_CANDLE",
                    instrument=pos.plan.instrument,
                    direction=pos.plan.direction,
                    grade=pos.plan.grade, score=pos.signal.score,
                    signal_time=pos.signal_time,
                    opened_at=pos.opened_at,
                    closed_at=bar.time + timedelta(minutes=15),
                    entry=pos.entry, exit=pos.last_exit,
                    stop=pos.original_stop, target=pos.target,
                    initial_units=pos.units,
                    gross_home=pos.gross,
                    costs_home=costs.total(),
                    net_home=net, net_r=net / pos.initial_risk_home,
                    close_reason=decision,
                    partial_executed=pos.partial_done,
                    be_executed=pos.be_done,
                )
                trades.append(result)
                equity += net
                if equity <= 0:
                    blocked["NONPOSITIVE_RESEARCH_EQUITY"] += 1
                    break
                peak = max(peak, equity)
                if net < 0:
                    daily_loss += -net
                    streak += 1
                    cooldown[rules.instrument] = (
                        bar.time + timedelta(minutes=45)
                    )
                elif net > 0:
                    streak = 0
                open_pos = None

        if i < 49 or i == len(bars) - 1 or open_pos is not None:
            continue
        now = bar.time + timedelta(minutes=15)
        # Closed bars only. Same validator and indicators as main.py.
        window = [x.mid for x in bars[max(0, i-199):i+1]]
        try:
            window = validate_candles(window, now=now, min_count=50)
        except CandleDataError:
            blocked["CANDLE_DATA_UNVERIFIED"] += 1
            continue
        evaluated += 1
        if not sessions.instrument_tradeable(
            rules.instrument, now=now, broker_tradeable=fact["broker_tradeable"],
            require_session=config.ENABLE_SESSIONS,
        ):
            blocked["SESSION_OR_BROKER_BLOCK"] += 1
            continue
        closes = [c["close"] for c in window]
        fast = strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = strategy.atr_engine.atr(window, period=config.ATR_PERIOD)
        if not atr or atr <= 0:
            blocked["INDICATORS_UNAVAILABLE"] += 1
            continue
        zone_low = min(c["low"] for c in window[:-1][-10:])
        zone_high = max(c["high"] for c in window[:-1][-10:])
        found = []
        for direction in ("LONG", "SHORT"):
            signal = strategy.evaluate_signal(
                rules.instrument, direction, window, fast, slow,
                atr, zone_low, zone_high, now,
            )
            if signal is not None:
                found.append(signal)
        if not found:
            continue
        signal = max(found, key=lambda s: s.score)
        key = signal.setup_id or (
            rules.instrument + ":" + signal.direction + ":" + iso_utc(now)
        )
        if key in seen_setups:
            blocked["DUPLICATE_SETUP"] += 1
            continue
        seen_setups.add(key)
        # Calendar evidence is evaluated at historical decision time.
        # A true zero-event covered snapshot is distinct from missing data.
        if _OfflineCalendar(fact.get("calendar")).block_trading(
            rules.instrument, now
        ) is not None:
            blocked["NEWS_UNKNOWN_OR_LOCKOUT"] += 1
            continue
        queued = (signal, atr, now)

    return ReplayResult(
        tuple(trades), dict(sorted(blocked.items())),
        1 if open_pos is not None else 0,
        assumptions.source_kind, assumptions.management, evaluated,
    )
