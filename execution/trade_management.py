"""Category-4 read-only management decisions and durable action reservations.

Preview BE/partial outside entry hours. Stage an action exactly once before
a future (currently disabled) broker mutation. Confirmation comes ONLY from
read-only broker transactions and the reconciler, never the submit response.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from broker.account_reconciler import AccountReconciler, ReconcileError
from risk.trade_lifecycle import LifecycleError, TradeRecord
from risk.trade_plan import InstrumentRules
from storage.lifecycle_state import StateError
from utils.time_tools import iso_utc, utc_now


@dataclass(frozen=True)
class ManagementAdvice:
    trade_id: str
    current_r: Decimal
    be_target: Decimal | None
    partial_units: Decimal | None
    partial_phase: str
    preview_only: bool = True


class TradeManagement:
    """Runs under an already-held process-wide AccountReconciler lease."""

    def __init__(self, reconciler: AccountReconciler):
        self.reconciler = reconciler

    def _trusted_trade(self, trade_id: str):
        report = self.reconciler.run()
        if report.blocked_reasons:
            raise ReconcileError("Management blocked by unresolved broker/account state")
        if trade_id not in report.owned_open_trades:
            raise ReconcileError("Trade not confirmed as safe and owned")
        state = self.reconciler.store.load()
        if not state or trade_id not in state["trades"]:
            raise ReconcileError("Lifecycle trade missing")
        return TradeRecord.from_dict(state["trades"][trade_id])

    def preview(self, trade_id, rules: InstrumentRules, *, bid, ask,
                phase="phase1", trigger_r=Decimal("1"),
                fraction=Decimal("0.5")) -> ManagementAdvice:
        record = self._trusted_trade(trade_id)
        if rules.instrument != record.instrument:
            raise LifecycleError("Instrument metadata mismatch")
        return ManagementAdvice(
            trade_id=trade_id,
            current_r=record.favorable_r(bid=bid, ask=ask),
            be_target=record.break_even_target(
                bid=bid, ask=ask, trigger_r=trigger_r
            ),
            partial_units=record.partial_close_units(
                rules, bid=bid, ask=ask, phase=phase,
                trigger_r=trigger_r, fraction=fraction
            ),
            partial_phase=phase,
        )

    def stage_break_even(self, trade_id, rules, *, bid, ask, trigger_r=Decimal("1")):
        """Persist one intent; DOES NOT contact OANDA or modify broker SL."""
        advice = self.preview(
            trade_id, rules, bid=bid, ask=ask, trigger_r=trigger_r
        )
        if advice.be_target is None:
            raise LifecycleError("BE unavailable, redundant or already staged")
        def mutate(state):
            trade = TradeRecord.from_dict(state["trades"][trade_id])
            if trade.be_state != "NONE":
                raise LifecycleError("Duplicate BE action")
            if trade.break_even_target(
                bid=bid, ask=ask, trigger_r=trigger_r
            ) != advice.be_target:
                raise LifecycleError("BE condition changed")
            state["trades"][trade_id] = replace(
                trade, be_state="INTENT"
            ).to_dict()
        self.reconciler.store.mutate(mutate)
        return {"trade_id": trade_id, "stopLoss": {"price": str(advice.be_target)},
                "status": "INTENT", "broker_write_attempted": False}

    def stage_partial(self, trade_id, rules, *, bid, ask, phase="phase1",
                      trigger_r=Decimal("1"), fraction=Decimal("0.5")):
        """Reserve a one-time phase. Short trade close units are POSITIVE."""
        advice = self.preview(
            trade_id, rules, bid=bid, ask=ask, phase=phase,
            trigger_r=trigger_r, fraction=fraction
        )
        if advice.partial_units is None:
            raise LifecycleError("Partial stage unavailable or already used")
        if not isinstance(phase, str) or not phase or len(phase) > 32:
            raise LifecycleError("Invalid partial phase")
        def mutate(state):
            trade = TradeRecord.from_dict(state["trades"][trade_id])
            if phase in trade.partial_phases:
                raise LifecycleError("Partial action already recorded")
            amount = trade.partial_close_units(
                rules, bid=bid, ask=ask, phase=phase,
                trigger_r=trigger_r, fraction=fraction
            )
            if amount != advice.partial_units:
                raise LifecycleError("Partial amount changed during stage")
            phases = dict(trade.partial_phases)
            phases[phase] = {"state": "INTENT", "units": str(amount)}
            state["trades"][trade_id] = replace(
                trade, partial_phases=phases
            ).to_dict()
        self.reconciler.store.mutate(mutate)
        return {"trade_id": trade_id, "units": str(advice.partial_units),
                "phase": phase, "status": "INTENT",
                "broker_write_attempted": False}


    def stage_close(self, trade_id, *, source="SYSTEM"):
        """Durably reserve one full trade-close command, without broker I/O.

        Only a clean, owned trade under the account lease can be staged.
        Source is an operator's intent label, never a claim about execution
        origin. SL/TP can still close the trade independently at the broker.
        """
        if source not in ("SYSTEM", "MANUAL"):
            raise LifecycleError("Invalid close source")
        record = self._trusted_trade(trade_id)
        if (record.closed or record.be_state in ("INTENT", "UNKNOWN")
            or any(x["state"] in ("INTENT", "UNKNOWN")
                   for x in record.partial_phases.values())):
            raise LifecycleError("Trade or management operation unresolved")
        cursor = self.reconciler.store.load()["cursor"]

        def mutate(state):
            current = TradeRecord.from_dict(state["trades"].get(trade_id))
            if current != record or state["cursor"] != cursor:
                raise LifecycleError("Trade changed before durable close reservation")
            intents = state.setdefault("close_intents", {})
            if trade_id in intents:
                raise LifecycleError("Close already staged; no duplicate action")
            intents[trade_id] = {
                "trade_id": trade_id, "attempt_id": current.attempt_id,
                "status": "INTENT", "source": source,
                "units": str(abs(current.current_units)),
                "cursor_before": cursor,
                "created_at": iso_utc(utc_now()),
                "confirmed_transaction_id": None,
            }
        self.reconciler.store.mutate(mutate, expected_cursor=cursor)
        return {
            "trade_id": trade_id, "source": source,
            "units": "ALL", "status": "INTENT",
            "broker_write_attempted": False,
        }

    def mark_unknown(self, trade_id, *, operation, phase=None):
        """Persist ambiguous BE/partial outcome without automatic re-submission.

        Called only after an ambiguous mutation transport result in a future,
        separately activated broker-write implementation.
        """
        if not self.reconciler._active:
            raise ReconcileError("Process-wide account lease required")
        if operation not in ("BE", "PARTIAL", "CLOSE"):
            raise LifecycleError("Unknown management operation")
        def mutate(state):
            if trade_id not in state["trades"]:
                raise LifecycleError("Unknown trade")
            if operation == "CLOSE":
                entry = state.get("close_intents", {}).get(trade_id)
                if not isinstance(entry, dict) or entry["status"] != "INTENT":
                    raise LifecycleError("No outstanding close intent")
                state["close_intents"][trade_id] = dict(
                    entry, status="UNKNOWN"
                )
                return
            trade = TradeRecord.from_dict(state["trades"][trade_id])
            if operation == "BE":
                if trade.be_state != "INTENT":
                    raise LifecycleError("No outstanding BE intent")
                trade = replace(trade, be_state="UNKNOWN")
            else:
                if phase not in trade.partial_phases or (
                    trade.partial_phases[phase]["state"] != "INTENT"
                ):
                    raise LifecycleError("No outstanding partial intent")
                new_phases = dict(trade.partial_phases)
                new_phases[phase] = dict(new_phases[phase], state="UNKNOWN")
                trade = replace(trade, partial_phases=new_phases)
            state["trades"][trade_id] = trade.to_dict()
        return self.reconciler.store.mutate(mutate)



class ManagementLoop:
    """Read-only recovery/monitoring always runs irrespective of entry session."""

    def __init__(self, reconciler: AccountReconciler):
        self.reconciler = reconciler

    def run_once(self):
        # No market session check and no broker POST/PUT. This method is called
        # before any new-entry strategy/session decision in a future live loop.
        with self.reconciler:
            report = self.reconciler.run()
            return report
