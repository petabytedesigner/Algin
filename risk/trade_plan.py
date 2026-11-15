"""Conservative risk-sizing and OANDA market-order payload construction."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from broker.oanda_client import BrokerError, number


@dataclass(frozen=True)
class TradePlan:
    instrument: str
    direction: str
    units: Decimal
    entry_hint: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    risk_pct: Decimal
    risk_home: Decimal
    margin_estimate: Decimal
    price_bound: Decimal

    def order(self, client_id: str) -> dict:
        if not client_id or len(client_id) > 128:
            raise BrokerError("Invalid client correlation ID")
        return {
            "type": "MARKET",
            "instrument": self.instrument,
            "units": format(self.units, "f"),
            "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY",
            "priceBound": format(self.price_bound, "f"),
            "stopLossOnFill": {"price": format(self.stop_loss, "f")},
            "takeProfitOnFill": {"price": format(self.take_profit, "f")},
            "clientExtensions": {"id": client_id, "tag": "algin"},
            "tradeClientExtensions": {"id": client_id, "tag": "algin"},
        }


def metadata(instruments_response: dict, instrument: str) -> dict:
    values = instruments_response.get("instruments")
    if not isinstance(values, list):
        raise BrokerError("Missing account instrument catalog")
    found = [v for v in values if isinstance(v, dict) and v.get("name") == instrument]
    if len(found) != 1:
        raise BrokerError("Instrument unavailable on this account")
    return found[0]


def home_conversion(pricing: dict, currency: str, home_currency: str) -> tuple[Decimal, Decimal]:
    if currency == home_currency:
        return Decimal("1"), Decimal("1")
    conversions = pricing.get("homeConversions")
    if not isinstance(conversions, list):
        raise BrokerError("Missing homeConversions; cannot calculate risk")
    found = [v for v in conversions if isinstance(v, dict) and v.get("currency") == currency]
    if len(found) != 1:
        raise BrokerError("Missing conversion for instrument quote currency")
    loss = number(found[0].get("accountLoss"))
    position = number(found[0].get("positionValue"))
    if loss <= 0 or position <= 0:
        raise BrokerError("Invalid home conversion factors")
    return loss, position


def make_plan(*, instrument: str, direction: str, quote: dict, spec: dict,
              nav: Any, margin_available: Any, risk_pct: Any, atr: Any,
              account_loss_factor: Any, position_value_factor: Any,
              min_distance_pips: Any = "3", reward_risk: Any = "2",
              max_spread_pips: Any = "2.5", slippage_pips: Any = "1") -> TradePlan:
    if direction not in ("LONG", "SHORT"):
        raise BrokerError("Unknown signal direction")
    if spec.get("name") != instrument or quote.get("instrument") != instrument:
        raise BrokerError("Instrument metadata/quote mismatch")
    qty_pct, equity = number(risk_pct), number(nav)
    margin, volatility = number(margin_available), number(atr)
    loss_factor, value_factor = number(account_loss_factor), number(position_value_factor)
    reward, spread_limit, slip = number(reward_risk), number(max_spread_pips), number(slippage_pips)
    if not 0 < qty_pct <= 1 or equity <= 0 or margin <= 0 or volatility <= 0:
        raise BrokerError("Invalid NAV, margin, ATR or risk budget")
    if min(loss_factor, value_factor, reward, spread_limit, slip) <= 0:
        raise BrokerError("Invalid conversion, RR or execution limit")
    try:
        precision = int(spec["displayPrecision"])
        unit_precision = int(spec["tradeUnitsPrecision"])
        pip_location = int(spec["pipLocation"])
    except (KeyError, ValueError, TypeError) as exc:
        raise BrokerError("Invalid instrument precision") from exc
    if not 0 <= precision <= 12 or not 0 <= unit_precision <= 8 or not -12 <= pip_location <= 4:
        raise BrokerError("Out of range instrument precision")
    pip = Decimal(10) ** pip_location
    step = Decimal(10) ** (-unit_precision)
    tick = Decimal(10) ** (-precision)
    entry = number(quote["ask"] if direction == "LONG" else quote["bid"])
    bid, ask = number(quote["bid"]), number(quote["ask"])
    if not 0 < bid < ask or entry <= 0:
        raise BrokerError("Invalid bid/ask")
    if (ask - bid) / pip > spread_limit:
        raise BrokerError("Spread over maximum")
    distance = max(volatility * Decimal("1.5"), number(min_distance_pips) * pip)
    if distance <= 0 or distance >= entry:
        raise BrokerError("Invalid stop distance")
    if direction == "LONG":
        stop = (entry - distance).quantize(tick, rounding=ROUND_HALF_UP)
        target = (entry + distance * reward).quantize(tick, rounding=ROUND_HALF_UP)
        bound = (entry + slip * pip).quantize(tick, rounding=ROUND_HALF_UP)
    else:
        stop = (entry + distance).quantize(tick, rounding=ROUND_HALF_UP)
        target = (entry - distance * reward).quantize(tick, rounding=ROUND_HALF_UP)
        bound = (entry - slip * pip).quantize(tick, rounding=ROUND_HALF_UP)
    if not (stop < entry < target if direction == "LONG" else target < entry < stop):
        raise BrokerError("Invalid directional stop/target")
    if bound <= 0 or (direction == 'LONG' and bound < entry) or (direction == 'SHORT' and bound > entry) or abs(stop-entry) < number(min_distance_pips) * pip:
        raise BrokerError("Rounded stop or priceBound invalid")
    budget = equity * qty_pct / Decimal(100)
    risk_per_unit_home = max(abs(entry - stop), abs(bound - stop)) * loss_factor
    if risk_per_unit_home <= 0:
        raise BrokerError("Invalid risk per unit")
    unsigned = (budget / risk_per_unit_home).quantize(step, rounding=ROUND_DOWN)
    try:
        min_units = number(spec["minimumTradeSize"])
        max_units = number(spec["maximumOrderUnits"])
        margin_rate = number(spec["marginRate"])
    except KeyError as exc:
        raise BrokerError("Missing instrument limits") from exc
    if not (0 < min_units <= unsigned <= max_units) or not (0 < margin_rate <= 1):
        raise BrokerError("Units violate instrument minimum/maximum or margin rate")
    # Position value (quote currency) converted to account currency; estimate only.
    margin_estimate = unsigned * max(entry, bound) * value_factor * margin_rate
    if margin_estimate > margin * Decimal("0.90"):
        raise BrokerError("Conservative margin reserve exceeded")
    risk_real = unsigned * risk_per_unit_home
    if risk_real > budget:
        raise BrokerError("Rounded quantity exceeds risk budget")
    units = unsigned if direction == "LONG" else -unsigned
    return TradePlan(instrument, direction, units, entry, stop, target,
                     qty_pct, risk_real, margin_estimate, bound)
