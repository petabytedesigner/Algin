"""Final Category-3 quote/spread/slippage and order-request validation.

No network, no account mutation, no implicit direction or unit corrections.
All money/price calculations use Decimal, not float or guessed precision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from typing import Any

from risk.trade_plan import InstrumentRules, QuoteConversions, TradePlan, number
from broker.oanda_v20_contract import price_tradeable
from utils.time_tools import parse_utc, require_utc


class PreflightRejected(ValueError):
    """Input cannot safely produce a broker mutation request."""


@dataclass(frozen=True)
class MarketQuote:
    instrument: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    spread_pips: Decimal


@dataclass(frozen=True)
class PreparedOrder:
    payload: dict
    units: Decimal
    bound: Decimal
    entry: Decimal
    worst_loss_home: Decimal
    spread_pips: Decimal


def _valid_status(price: dict) -> bool:
    # Current v20 uses Price.tradeable; Price.status is deprecated.
    return price_tradeable(price)


def parse_market_quote(
    data: dict, instrument: str, rules: InstrumentRules, *, now: datetime,
    max_age_seconds: int, max_spread_pips: Decimal,
) -> MarketQuote:
    now = require_utc(now)
    if (not isinstance(data, dict) or not isinstance(instrument, str)
        or rules.instrument != instrument):
        raise PreflightRejected("Instrument/price mismatch")
    try:
        prices = data["prices"]
        if not isinstance(prices, list):
            raise ValueError("Missing price list")
        matches = [
            x for x in prices if isinstance(x, dict) and x.get("instrument") == instrument
        ]
        if len(matches) != 1 or not _valid_status(matches[0]):
            raise ValueError("Untradeable, missing or duplicated instrument price")
        quote = matches[0]
        bids, asks = quote["bids"], quote["asks"]
        if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
            raise ValueError("Empty bid/ask book")
        bid = number(bids[0]["price"], "bid", positive=True)
        ask = number(asks[0]["price"], "ask", positive=True)
        if bid >= ask:
            raise ValueError("Crossed/locked order book")
        stamp = parse_utc(quote["time"])
        if (not isinstance(max_age_seconds, int) or max_age_seconds <= 0
            or stamp > now + timedelta(seconds=2)
            or (now - stamp).total_seconds() > max_age_seconds):
            raise ValueError("Stale or future-dated quote")
        max_spread = number(max_spread_pips, "max_spread_pips", positive=True)
        spread = (ask - bid) / rules.pip_size
        if spread > max_spread:
            raise ValueError("Broker spread exceeds policy")
        # Reject prices whose precision is not a valid broker price step.
        for price in (bid, ask):
            if price != price.quantize(rules.price_step):
                raise ValueError("Unquantized market price")
        return MarketQuote(instrument, bid, ask, stamp, spread)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
        raise PreflightRejected(f"Invalid broker quote: {exc}") from exc


def build_market_order(
    plan: TradePlan, rules: InstrumentRules, conversion: QuoteConversions,
    market: MarketQuote, *, attempt_id: str, signal_id: str,
    slippage_pips: Decimal, minimum_stop_pips: Decimal,
) -> PreparedOrder:
    """Tie the risk plan to *this exact quote*, quantize and cap worst loss."""
    if not all(isinstance(x, cls) for x, cls in (
        (plan, TradePlan), (rules, InstrumentRules),
        (conversion, QuoteConversions), (market, MarketQuote)
    )):
        raise PreflightRejected("Missing typed trade/risk context")
    if (not plan.preview_only or plan.instrument != rules.instrument
        or plan.instrument != conversion.instrument
        or plan.instrument != market.instrument):
        raise PreflightRejected("Plan/instrument provenance mismatch")
    if (plan.direction not in ("LONG", "SHORT")
        or plan.grade not in ("A", "B")):
        raise PreflightRejected("Unsupported direction or grade")
    if conversion.timestamp != market.timestamp or plan.quote_timestamp != market.timestamp:
        raise PreflightRejected("Risk conversion does not match the fresh quote")
    if conversion.bid != market.bid or conversion.ask != market.ask:
        raise PreflightRejected("Broker quote changed since risk calculation")
    direction = 1 if plan.direction == "LONG" else -1
    entry = market.ask if direction == 1 else market.bid
    if plan.entry != entry:
        raise PreflightRejected("Plan entry is not current broker side price")
    signed_units = number(plan.units, "units", zero_ok=False)
    if direction * signed_units <= 0:
        raise PreflightRejected("Signed units disagree with LONG/SHORT")
    unsigned = abs(signed_units)
    if (unsigned != unsigned.quantize(rules.unit_step)
        or unsigned < rules.minimum_trade_size
        or unsigned > rules.maximum_order_units
        or (rules.maximum_position_size > 0 and unsigned > rules.maximum_position_size)):
        raise PreflightRejected("Invalid size/precision/broker unit limits")
    stop = number(plan.stop_loss, "SL", positive=True)
    target = number(plan.take_profit, "TP", positive=True)
    if any(x != x.quantize(rules.price_step) for x in (stop, target)):
        raise PreflightRejected("Stop/target precision mismatch")
    minimum = number(minimum_stop_pips, "minimum stop pips") * rules.pip_size
    if minimum <= 0:
        raise PreflightRejected("Invalid stop distance")
    tolerance = number(slippage_pips, "slippage pips")
    if tolerance < 0:
        raise PreflightRejected("Negative slippage tolerance")
    if direction == 1:
        if not stop < market.bid - minimum or not target > market.ask + minimum:
            raise PreflightRejected("LONG SL/TP invalid against executable quote")
        bound = (market.ask + tolerance * rules.pip_size).quantize(
            rules.price_step, rounding=ROUND_CEILING
        )
        if target <= bound or stop >= bound:
            raise PreflightRejected("LONG stop/target conflicts with priceBound")
    else:
        if not stop > market.ask + minimum or not target < market.bid - minimum:
            raise PreflightRejected("SHORT SL/TP invalid against executable quote")
        bound = (market.bid - tolerance * rules.pip_size).quantize(
            rules.price_step, rounding=ROUND_FLOOR
        )
        if bound <= 0 or stop <= bound or target >= bound:
            raise PreflightRejected("SHORT stop/target conflicts with priceBound")
    if (abs(plan.entry - stop) < minimum
        or number(plan.reward_to_risk, "planned R", positive=True) < Decimal("1")):
        raise PreflightRejected("Invalid planned risk/reward")
    risk_budget = number(plan.risk_budget_home, "risk budget", positive=True)
    quote_loss = number(conversion.quote_loss_to_home, "loss conversion", positive=True)
    worst_per_unit = abs(bound - stop) * quote_loss
    if worst_per_unit <= 0:
        raise PreflightRejected("No defensible worst-price loss estimate")
    # Never silently INCREASE units; shrink only if the slippage-bound exposure
    # would otherwise exceed the original, independently risk-approved budget.
    safe_units = min(unsigned, risk_budget / worst_per_unit)
    safe_units = safe_units.quantize(rules.unit_step, rounding=ROUND_DOWN)
    if safe_units < rules.minimum_trade_size or safe_units <= 0:
        raise PreflightRejected("Slippage-aware size below broker minimum")
    if safe_units > unsigned:
        raise PreflightRejected("Unit size inadvertently increased")
    signed = safe_units if direction == 1 else -safe_units
    worst_loss = safe_units * worst_per_unit
    if worst_loss > risk_budget:
        raise PreflightRejected("Post-quantization worst-price risk cap exceeded")
    # No netting or unintended reductions to manually held positions.
    order = {
        "instrument": plan.instrument, "units": format(signed, "f"),
        "type": "MARKET", "timeInForce": "FOK",
        "positionFill": "OPEN_ONLY",
        "priceBound": format(bound, "f"),
        "stopLossOnFill": {"price": format(stop, "f")},
        "takeProfitOnFill": {"price": format(target, "f")},
        "clientExtensions": {"id": attempt_id, "tag": "algin", "comment": signal_id},
        "tradeClientExtensions": {"id": attempt_id, "tag": "algin", "comment": signal_id},
    }
    return PreparedOrder(
        {"order": order}, signed, bound, entry, worst_loss, market.spread_pips
    )
