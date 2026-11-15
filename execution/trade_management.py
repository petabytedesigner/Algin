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
