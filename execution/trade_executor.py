"""Broker mutation coordinator. Intent journal written before EVERY submit/modify/close."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import re

from broker.oanda_client import BrokerError, BrokerRejected, OutcomeUnknown, number, trade_fill
from storage.state_store import utc_now


class TradeExecutor:
    def __init__(self, client, store, events, submission_enabled=False):
        self.client = client
        self.store = store
        self.events = events
        self.submission_enabled = submission_enabled

    @staticmethod
    def _safe_id(signal_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", signal_id):
            raise BrokerError("Unsafe signal identifier")
        return "algin-" + hashlib.sha256(signal_id.encode()).hexdigest()[:24]

    def _record(self, state, key, status, **other):
        record = state["intents"].setdefault(key, {})
        record.update(other)
        record["status"] = status
        record["updated_at"] = utc_now()
        self.store.save(state)

    def submit(self, state, plan, signal_id, grade):
        if not self.submission_enabled:
            self.events.emit("SUBMIT_DISABLED", instrument=plan.instrument,
                             direction=plan.direction, signal_id=signal_id, status="BLOCKED")
            return {"status": "BLOCKED"}
        intent_id = self._safe_id(signal_id)
        if intent_id in state["intents"]:
            # Prevent duplicates from restart, loop, and lost HTTP responses.
            return {"status": "BLOCKED", "reason": "Intent already recorded"}
        self._record(state, intent_id, "SUBMITTING", kind="OPEN", instrument=plan.instrument,
                     direction=plan.direction, signal_id=signal_id, grade=grade,
                     units=str(plan.units), entry_hint=str(plan.entry_hint),
                     stop_loss=str(plan.stop_loss), take_profit=str(plan.take_profit),
                     risk_pct=str(plan.risk_pct), risk_home=str(plan.risk_home))
        payload = plan.order(intent_id)
        try:
            response = self.client.create_order(payload)
            outcome = trade_fill(response)
        except BrokerRejected:
            self._record(state, intent_id, "REJECTED")
            self.events.emit("ORDER_REJECTED", instrument=plan.instrument, intent_id=intent_id, status="REJECTED")
            return {"status": "REJECTED"}
        except (OutcomeUnknown, BrokerError):
            self._record(state, intent_id, "UNKNOWN")
            self.events.emit("ORDER_UNKNOWN", instrument=plan.instrument, intent_id=intent_id, status="UNKNOWN")
            return {"status": "UNKNOWN"}
        if outcome["status"] != "OPENED":
            self._record(state, intent_id, outcome["status"])
            return outcome
        trade_id = outcome["trade_id"]
        # The fill is real. A failed read/verification MUST NOT lose it.
        self._record(state, intent_id, "FILLED_UNVERIFIED", trade_id=trade_id)
        state["trades"][trade_id] = {
            "instrument": plan.instrument, "direction": plan.direction, "grade": grade,
            "intent_id": intent_id, "entry": str(outcome["entry"] or plan.entry_hint),
            "initial_units": str(outcome["units"] or plan.units),
            "initial_stop": str(plan.stop_loss), "stop": str(plan.stop_loss),
            "take_profit": str(plan.take_profit), "be_done": False, "partial_done": False,
            "risk_home": str(plan.risk_home), "risk_pct": str(plan.risk_pct),
            "opened_at": utc_now(),
        }
        self.store.save(state)
        try:
            observed = self.client.trade(trade_id).get("trade")
            if not isinstance(observed, dict) or not observed.get("stopLossOrder") or not observed.get("takeProfitOrder"):
                raise BrokerError("Missing protective orders on broker trade")
            if observed.get("state") != "OPEN":
                raise BrokerError("Trade not open after submission")
        except BrokerError:
            self._record(state, intent_id, "UNPROTECTED_OR_UNVERIFIED")
            self.events.emit("PROTECTION_UNKNOWN", instrument=plan.instrument, intent_id=intent_id, trade_id=trade_id)
            return {"status": "UNPROTECTED_OR_UNVERIFIED", "trade_id": trade_id}
        self._record(state, intent_id, "CONFIRMED")
        self.events.emit("TRADE_OPENED", instrument=plan.instrument, direction=plan.direction, grade=grade,
                         intent_id=intent_id, trade_id=trade_id, units=str(plan.units),
                         stop_loss=str(plan.stop_loss), take_profit=str(plan.take_profit), status="CONFIRMED")
        return {"status": "CONFIRMED", "trade_id": trade_id}

    def move_stop(self, state, trade_id, new_stop):
        if not self.submission_enabled:
            return "BLOCKED"
        trade = state["trades"][trade_id]
        key = trade["intent_id"] + "-be"
        if key in state["intents"]:
            return "BLOCKED"
        self._record(state, key, "SUBMITTING", kind="BE", trade_id=trade_id,
                     expected_stop=str(new_stop))
        try:
            self.client.set_dependent_orders(trade_id, stop_loss=new_stop)
        except BrokerRejected:
            self._record(state, key, "REJECTED")
            return "REJECTED"
        except (OutcomeUnknown, BrokerError):
            self._record(state, key, "UNKNOWN")
            return "UNKNOWN"
        self._record(state, key, "UNVERIFIED")
        try:
            observed = self.client.trade(trade_id)["trade"]
            actual = number(observed["stopLossOrder"]["price"])
            if actual != number(new_stop):
                return "UNVERIFIED"
        except (BrokerError, KeyError, TypeError):
            return "UNVERIFIED"
        trade["stop"] = str(new_stop)
        trade["be_done"] = True
        self.store.save(state)
        self._record(state, key, "CONFIRMED")
        self.events.emit("BREAK_EVEN", instrument=trade["instrument"], trade_id=trade_id, status="CONFIRMED")
        return "CONFIRMED"

    def partial_close(self, state, trade_id, units, previous_units):
        if not self.submission_enabled:
            return "BLOCKED"
        trade = state["trades"][trade_id]
        key = trade["intent_id"] + "-partial"
        if key in state["intents"]:
            return "BLOCKED"
        requested = number(units)
        prev = number(previous_units)
        if requested <= 0 or requested >= abs(prev):
            return "INVALID"
        self._record(state, key, "SUBMITTING", kind="PARTIAL", trade_id=trade_id,
                     requested_units=str(requested), original_units=str(prev))
        try:
            self.client.close_trade(trade_id, str(requested))
        except BrokerRejected:
            self._record(state, key, "REJECTED")
            return "REJECTED"
        except (OutcomeUnknown, BrokerError):
            self._record(state, key, "UNKNOWN")
            return "UNKNOWN"
        self._record(state, key, "UNVERIFIED")
        try:
            observed = self.client.trade(trade_id)["trade"]
            remaining = number(observed["currentUnits"])
            if abs(remaining) != abs(prev) - requested:
                return "UNVERIFIED"
        except (BrokerError, KeyError, TypeError):
            return "UNVERIFIED"
        trade["partial_done"] = True
        trade["current_units"] = str(remaining)
        self.store.save(state)
        self._record(state, key, "CONFIRMED")
        self.events.emit("PARTIAL_CLOSE", instrument=trade["instrument"], trade_id=trade_id,
                         units=str(requested), status="CONFIRMED")
        return "CONFIRMED"
