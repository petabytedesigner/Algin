"""One-cycle pipeline: broker truth -> recovery -> manage own trades -> entry gates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
from zoneinfo import ZoneInfo

import config
from broker.oanda_client import BrokerError, number, quote_from_pricing, timestamp
from data.data_handler import DataHandler
from execution.trade_executor import TradeExecutor
from risk.risk_engine import (Grade, OpenPositionRisk, RiskConfig,
                              RiskEngine, TradeCandidate)
from risk.trade_plan import make_plan, metadata, home_conversion
from strategy.strategy import Strategy


def session_open(now: datetime) -> bool:
    """Entry sessions use DST-aware local clocks; open-trade management is independent."""
    if now.weekday() >= 5:
        return False
    london = now.astimezone(ZoneInfo("Europe/London"))
    new_york = now.astimezone(ZoneInfo("America/New_York"))
    return (8 <= london.hour < 17) or (8 <= new_york.hour < 17)


class TradingEngine:
    def __init__(self, client, store, events, submit_enabled: bool = False):
        self.client, self.store, self.events = client, store, events
        self.data = DataHandler(client)
        self.strategy = Strategy()
        self.executor = TradeExecutor(client, store, events, submission_enabled=submit_enabled)
        self.submit_enabled = submit_enabled
        self.risk = RiskEngine(RiskConfig(
            max_total_risk_pct=config.MAX_TOTAL_RISK_PCT,
            risk_pct_grade_a=config.GRADE_A_RISK_PCT,
            risk_pct_grade_b=config.GRADE_B_RISK_PCT,
            max_open_trades_grade_a=config.MAX_TRADES_GRADE_A,
            max_open_trades_grade_b=config.MAX_TRADES_GRADE_B,
            cooldown_minutes_after_loss=config.COOLDOWN_MINUTES,
            allow_multiple_trades_same_instrument=False,
            allow_hedging_same_instrument=False))
        self.state = self.store.load()

    def _snapshot(self):
        response = self.client.account()
        account = response["account"]
        if account.get("id") != self.store.account_id:
            raise BrokerError("Account identity mismatch")
        if not account.get("currency") or number(account.get("NAV")) <= 0 or number(account.get("marginAvailable")) < 0:
            raise BrokerError("Invalid account NAV, margin or currency")
        open_trades = self.client.open_trades().get("trades")
        pending = self.client.pending_orders().get("orders")
        if not isinstance(open_trades, list) or not isinstance(pending, list):
            raise BrokerError("Open trade/order snapshot missing")
        for t in open_trades:
            if not isinstance(t, dict) or not t.get("id") or t.get("state") != "OPEN":
                raise BrokerError("Malformed open trade snapshot")
        self.state["last_transaction_id"] = response.get("lastTransactionID")
        self.store.save(self.state)
        return account, {str(t["id"]): t for t in open_trades}, pending

    def _adopt_intent(self, intent_id, intent, observed):
        tid = str(observed["id"])
        self.state["trades"][tid] = {
            "status": "OPEN", "instrument": intent["instrument"],
            "direction": intent["direction"], "grade": intent["grade"],
            "intent_id": intent_id, "entry": str(observed["price"]),
            "initial_units": str(observed["initialUnits"]),
            "current_units": str(observed["currentUnits"]),
            "initial_stop": intent["stop_loss"], "stop": intent["stop_loss"],
            "take_profit": intent["take_profit"], "risk_pct": intent["risk_pct"],
            "risk_home": intent["risk_home"], "be_done": False,
            "partial_done": False, "opened_at": observed.get("openTime", ""),
        }
        intent["trade_id"] = tid
        intent["status"] = "CONFIRMED" if observed.get("stopLossOrder") and observed.get("takeProfitOrder") else "UNPROTECTED_OR_UNVERIFIED"
        self.store.save(self.state)

    def _reconcile(self, broker_trades):
        # Recover a write that was received by OANDA but whose HTTP answer was lost.
        for intent_id, intent in list(self.state["intents"].items()):
            if intent.get("kind") != "OPEN" or intent.get("status") not in ("SUBMITTING", "UNKNOWN", "FILLED_UNVERIFIED", "UNPROTECTED_OR_UNVERIFIED"):
                continue
            matches = [t for t in broker_trades.values() if (t.get("clientExtensions") or {}).get("id") == intent_id]
            if len(matches) == 1:
                if matches[0].get("instrument") != intent.get("instrument"):
                    raise BrokerError("Client ID matches wrong instrument")
                self._adopt_intent(intent_id, intent, matches[0])
            # No match does NOT prove a request failed: leave UNKNOWN.

        for tid, local in self.state["trades"].items():
            observed = broker_trades.get(tid)
            if observed:
                if observed.get("instrument") != local.get("instrument"):
                    raise BrokerError("Tracked trade instrument mismatch")
                local["status"] = "OPEN"
                local["current_units"] = str(observed.get("currentUnits"))
                if not observed.get("stopLossOrder") or not observed.get("takeProfitOrder"):
                    local["protection"] = "MISSING"
                else:
                    local["protection"] = "CONFIRMED"
                    local["stop"] = str(observed["stopLossOrder"].get("price", local["stop"]))
            elif local.get("status") != "CLOSED":
                # A missing trade needs explicit CLOSED confirmation; never assume it closed.
                detail = self.client.trade(tid).get("trade")
                if not isinstance(detail, dict) or detail.get("state") != "CLOSED":
                    raise BrokerError("Tracked trade missing, closed state not confirmed")
                local["status"] = "CLOSED"
                local["closed_at"] = detail.get("closeTime", "")
                local["realized_pl"] = str(detail.get("realizedPL", ""))
                if tid not in self.state["events"]:
                    pl = number(detail.get("realizedPL"))
                    if pl < 0:
                        self.state["cooldown"][local["instrument"]] = datetime.now(timezone.utc).isoformat()
                    self.state["events"][tid] = "CLOSED"
                    self.events.emit("TRADE_CLOSED", instrument=local["instrument"], trade_id=tid, status="CLOSED")
        self._reconcile_actions(broker_trades)
        self.store.save(self.state)

    def _reconcile_actions(self, broker_trades):
        for key, intent in self.state["intents"].items():
            if intent.get("kind") not in ("BE", "PARTIAL") or intent.get("status") not in ("SUBMITTING", "UNKNOWN", "UNVERIFIED"):
                continue
            tid = intent.get("trade_id")
            trade = broker_trades.get(tid)
            if not trade or tid not in self.state["trades"]:
                continue
            local = self.state["trades"][tid]
            if intent["kind"] == "BE":
                stop = (trade.get("stopLossOrder") or {}).get("price")
                if stop is not None and number(stop) == number(intent["expected_stop"]):
                    local["be_done"] = True
                    local["stop"] = str(stop)
                    intent["status"] = "CONFIRMED"
            elif intent["kind"] == "PARTIAL":
                expected = abs(number(intent["original_units"])) - number(intent["requested_units"])
                if abs(number(trade["currentUnits"])) == expected:
                    local["partial_done"] = True
                    intent["status"] = "CONFIRMED"

    def _unresolved(self):
        bad = {"SUBMITTING", "UNKNOWN", "UNVERIFIED", "FILLED_UNVERIFIED", "UNPROTECTED_OR_UNVERIFIED"}
        return [key for key, intent in self.state["intents"].items() if intent.get("status") in bad]

    def _manage(self, broker_trades, instrument_specs):
        for tid, observed in broker_trades.items():
            local = self.state["trades"].get(tid)
            if not local or local.get("status") != "OPEN" or local.get("protection") != "CONFIRMED":
                continue
            if local.get("intent_id") not in self.state["intents"]:
                continue
            if not local.get("initial_stop") or not local.get("entry"):
                continue
            quote = quote_from_pricing(self.client.pricing(local["instrument"]),
                                       local["instrument"], datetime.now(timezone.utc))
            entry, init_stop = number(local["entry"]), number(local["initial_stop"])
            side = Decimal("1") if local["direction"] == "LONG" else Decimal("-1")
            delta = abs(entry - init_stop)
            if delta <= 0:
                continue
            exit_price = quote["bid"] if side > 0 else quote["ask"]
            multiples = side * (exit_price - entry) / delta
            current_stop = number((observed["stopLossOrder"])["price"])
            if multiples >= Decimal("1") and not local.get("be_done"):
                better_or_equal = (current_stop >= entry if side > 0 else current_stop <= entry)
                if better_or_equal:
                    local["be_done"] = True
                    self.store.save(self.state)
                else:
                    outcome = self.executor.move_stop(self.state, tid, entry)
                    if outcome != 'CONFIRMED':
                        continue
            if multiples >= Decimal("2") and not local.get("partial_done"):
                spec = metadata(instrument_specs, local["instrument"])
                step = Decimal(10) ** (-int(spec["tradeUnitsPrecision"]))
                current = number(observed["currentUnits"])
                partial = (abs(current) / Decimal("2")).quantize(step, rounding=ROUND_DOWN)
                minimum = number(spec["minimumTradeSize"])
                if partial >= minimum and abs(current) - partial >= minimum:
                    self.executor.partial_close(self.state, tid, partial, current)

    def _entries_allowed(self, now, broker_trades, pending):
        if self._unresolved():
            return False, "Unresolved broker mutation"
        if pending:
            return False, "Pending account orders"
        if any(tid not in self.state["trades"] for tid in broker_trades):
            return False, "Foreign/manual trades present"
        if any(t.get("protection") != "CONFIRMED" for t in self.state["trades"].values() if t.get("status") == "OPEN"):
            return False, "Unprotected trade"
        if config.ENABLE_SESSIONS and not session_open(now):
            return False, "Outside entry session"
        if config.NEWS_MODE != "DISABLED_ACKNOWLEDGED":
            return False, "News feed not verified"
        return True, ""

    def _in_cooldown(self, inst, now):
        val = self.state["cooldown"].get(inst)
        return bool(val and now < timestamp(val) + timedelta(minutes=config.COOLDOWN_MINUTES))

    def _setup(self, inst, candles, now):
        # Historical structure precedes the breakout candle; retest candle is last.
        history, breakout, retest = candles[-22:-2], candles[-2], candles[-1]
        if len(history) < 20:
            return None
        resistance = max(c["high"] for c in history)
        support = min(c["low"] for c in history)
        closes = [c["close"] for c in candles]
        fast = self.strategy.ema_engine.ema(closes, config.EMA_FAST)
        slow = self.strategy.ema_engine.ema(closes, config.EMA_SLOW)
        atr = self.strategy.atr_engine.atr(candles, config.ATR_PERIOD)
        if atr <= 0:
            return None
        possibilities = []
        for direction, lo, hi in (
            ("LONG", resistance - atr * .5, resistance),
            ("SHORT", support, support + atr * .5),
        ):
            if direction == "LONG":
                if not (breakout["close"] > resistance and breakout["close"] > breakout["open"] and
                        retest["close"] > retest["open"] and fast > slow):
                    continue
            else:
                if not (breakout["close"] < support and breakout["close"] < breakout["open"] and
                        retest["close"] < retest["open"] and fast < slow):
                    continue
            signal = self.strategy.evaluate_signal(inst, direction, candles, fast, slow,
                                                  atr, lo, hi, now)
            if signal and signal.grade in ("A", "B"):
                possibilities.append((signal, atr))
        return max(possibilities, key=lambda x: x[0].score) if possibilities else None

    def _new_entries(self, account, broker_trades, instrument_specs, now):
        positions = []
        for tid, local in self.state["trades"].items():
            if local.get("status") == "OPEN":
                positions.append(OpenPositionRisk(
                    trade_id=tid, instrument=local["instrument"], direction=local["direction"],
                    grade=Grade(local["grade"]), risk_pct=float(local["risk_pct"]),
                    opened_at=timestamp(local["opened_at"]) if local.get("opened_at") else now))
        for inst in config.INSTRUMENTS:
            if self._in_cooldown(inst, now):
                self.events.emit("ENTRY_SKIPPED", instrument=inst, reason="COOLDOWN")
                continue
            try:
                candles = self.data.get_candles(inst)
                candle_id = candles[-1]["time"]
                if self.state["last_candles"].get(inst) == candle_id:
                    continue
                setup = self._setup(inst, candles, now)
                self.state["last_candles"][inst] = candle_id
                self.store.save(self.state)
                if not setup:
                    self.events.emit("ENTRY_SKIPPED", instrument=inst, reason="NO_SIGNAL")
                    continue
                signal, atr = setup
                risk_pct = config.GRADE_A_RISK_PCT if signal.grade == "A" else config.GRADE_B_RISK_PCT
                decision = self.risk.evaluate_candidate(
                    TradeCandidate(inst, signal.direction, Grade(signal.grade),
                                   signal.score, risk_pct, now), positions)
                if not decision.accept:
                    self.events.emit("ENTRY_SKIPPED", instrument=inst, reason=decision.code.value)
                    continue
                quote, pricing = self.data.get_quote_and_conversions(inst)
                spec = metadata(instrument_specs, inst)
                loss, value = home_conversion(pricing, inst.split("_")[-1], account["currency"])
                plan = make_plan(instrument=inst, direction=signal.direction, quote=quote,
                                 spec=spec, nav=account["NAV"],
                                 margin_available=account["marginAvailable"],
                                 risk_pct=risk_pct, atr=atr, account_loss_factor=loss,
                                 position_value_factor=value, min_distance_pips=config.MIN_DISTANCE_PIPS,
                                 reward_risk=config.REWARD_RISK,
                                 max_spread_pips=config.MAX_SPREAD_PIPS,
                                 slippage_pips=config.SLIPPAGE_ALLOWED)
                signal_id = hashlib.sha256(
                    (inst + "|" + candle_id + "|" + signal.direction).encode()).hexdigest()
                self.events.emit("SIGNAL", instrument=inst, direction=signal.direction,
                                 grade=signal.grade, score=str(signal.score), signal_id=signal_id)
                outcome = self.executor.submit(self.state, plan, signal_id, signal.grade)
                if outcome["status"] not in ("CONFIRMED", "BLOCKED", "REJECTED"):
                    self.events.emit('ENTRY_GATED', reason='Unresolved broker submit', status='BLOCKED')
                    return
                # Broker snapshot and NAV must be refreshed before another new order.
                if outcome["status"] == "CONFIRMED":
                    return
            except BrokerError as exc:
                self.events.emit("ENTRY_SKIPPED", instrument=inst, reason=exc.__class__.__name__)
                if self._unresolved():
                    return
                continue

    def cycle(self):
        now = datetime.now(timezone.utc)
        account, trades, pending = self._snapshot()
        self._reconcile(trades)
        instrument_specs = self.client.instruments()
        # Ownership and position management are evaluated BEFORE entry-session gates.
        self._manage(trades, instrument_specs)
        allowed, reason = self._entries_allowed(now, trades, pending)
        if not allowed:
            self.events.emit("ENTRY_GATED", reason=reason, status="BLOCKED")
            return
        self._new_entries(account, trades, instrument_specs, now)
