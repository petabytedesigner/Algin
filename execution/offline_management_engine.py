"""Offline-only OANDA v20 BE/partial/full-close command and replay harness.

No HTTP, token, PracticeTransport or real broker mutations are possible here.
An action is reserved in checksummed lifecycle state BEFORE one synthetic
operation. Completion requires a subsequent GET-only AccountReconciler.run()
with immutable OANDA-shaped transaction IDs; HTTP 200 is not proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import copy
import re

import config
from broker.practice_transport import (
    BrokerReply, _PRACTICE_WRITES_RELEASED,
)
from broker.account_reconciler import AccountReconciler, ReconcileError
from execution.trade_management import TradeManagement
from risk.trade_lifecycle import TradeRecord, LifecycleError
from risk.trade_plan import InstrumentRules, number
from storage.lifecycle_state import StateError
from utils.time_tools import iso_utc, utc_now


class ManagementContractError(RuntimeError):
    """Unproven/ambiguous lifecycle operation; do not resend."""


_TRADE_ID = re.compile(r"^[0-9]{1,50}$")


@dataclass(frozen=True)
class ManagementCommand:
    operation: str
    trade_id: str
    method: str
    path: str
    body: dict
    phase: str | None = None


@dataclass(frozen=True)
class ManagementResult:
    status: str
    trade_id: str
    operation: str
    reason: str
    confirmed_transaction_id: str | None = None
    simulated_write_count: int = 0
    broker_write_attempted: bool = False


def build_command(record: TradeRecord, operation: str, *,
                  phase: str | None = None, close_intent=None):
    """Generate exact documented v20 PUT route from persisted intent."""
    if not isinstance(record, TradeRecord) or record.closed:
        raise ManagementContractError("Open trade record required")
    tid = record.trade_id
    if not _TRADE_ID.fullmatch(tid):
        raise ManagementContractError("Unusable trade specifier")
    if operation == "BE":
        if record.be_state != "INTENT":
            raise ManagementContractError("No newly staged BE intent")
        return ManagementCommand(
            "BE", tid, "PUT", f"/trades/{tid}/orders",
            {"stopLoss": {"price": str(record.entry_price)}},
        )
    if operation == "PARTIAL":
        value = record.partial_phases.get(phase)
        if (not isinstance(phase, str) or not isinstance(value, dict)
            or value["state"] != "INTENT"):
            raise ManagementContractError("No newly staged partial intent")
        units = number(value["units"], "intended partial units", positive=True)
        if units >= abs(record.current_units):
            raise ManagementContractError("Partial amount would close all units")
        return ManagementCommand(
            "PARTIAL", tid, "PUT", f"/trades/{tid}/close",
            {"units": str(units)}, phase=phase,
        )
    if operation == "CLOSE":
        if (not isinstance(close_intent, dict)
            or close_intent.get("status") != "INTENT"
            or close_intent.get("trade_id") != tid
            or close_intent.get("attempt_id") != record.attempt_id
            or number(close_intent.get("units"), "intended close", positive=True)
                != abs(record.current_units)):
            raise ManagementContractError("Full close not durably reserved")
        return ManagementCommand(
            "CLOSE", tid, "PUT", f"/trades/{tid}/close", {"units": "ALL"},
        )
    raise ManagementContractError("Unsupported management operation")


class SyntheticLifecycleBroker:
    """Sealed, in-memory account/transaction fixture. No networking code."""

    def __init__(self, *, account_id="acct-1", cursor="100",
                 effect_mode="NORMAL"):
        if effect_mode not in ("NORMAL", "TIMEOUT_BEFORE_EFFECT", "LOST_ACK"):
            raise ValueError("Unknown synthetic outcome")
        self.account_id = account_id
        self.cursor = cursor
        self.trades = []
        self.orders = []
        self.transactions = []
        self.effect_mode = effect_mode
        self.simulated_writes = 0
        self.calls = []

    def read(self, path, *, params=None):
        self.calls.append(("GET", path))
        if path == "":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "account": {
                    "id": self.account_id,
                    "openTradeCount": len(self.trades),
                    "pendingOrderCount": len(self.orders),
                    "trades": copy.deepcopy(self.trades),
                    "orders": copy.deepcopy(self.orders),
                },
            })
        if path == "/transactions/sinceid":
            since = params.get("id") if isinstance(params, dict) else None
            if not isinstance(since, str) or not since.isdecimal():
                return BrokerReply(400, {"errorMessage": "bad sinceid"})
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "transactions": [
                    copy.deepcopy(t) for t in self.transactions
                    if int(t["id"]) > int(since)
                ],
            })
        raise ManagementContractError("Unrecognized synthetic GET")

    def _next_id(self):
        self.cursor = str(int(self.cursor) + 1)
        return self.cursor

    def submit_synthetic(self, command: ManagementCommand):
        """Exactly one virtual PUT; never sends HTTP."""
        if (type(command) is not ManagementCommand
            or command.method != "PUT"
            or not _TRADE_ID.fullmatch(command.trade_id)
            or command.path not in (
                f"/trades/{command.trade_id}/orders",
                f"/trades/{command.trade_id}/close",
            )):
            raise ManagementContractError("Invalid synthetic command")
        self.simulated_writes += 1
        self.calls.append(("PUT_SIMULATED", command.path))
        if self.effect_mode == "TIMEOUT_BEFORE_EFFECT":
            raise TimeoutError("Synthetic request outcome unknown")
        candidates = [t for t in self.trades if t.get("id") == command.trade_id]
        if len(candidates) != 1:
            raise ManagementContractError("Unmatched synthetic open trade")
        trade = candidates[0]
        if command.operation == "BE":
            if (command.path != f"/trades/{command.trade_id}/orders"
                or set(command.body) != {"stopLoss"}
                or not isinstance(command.body["stopLoss"], dict)
                or set(command.body["stopLoss"]) != {"price"}):
                raise ManagementContractError("Malformed stop-loss replacement")
            target = number(command.body["stopLoss"]["price"], "BE price", positive=True)
            if target != number(trade["price"], "trade entry", positive=True):
                raise ManagementContractError("BE differs from entry price")
            t_id = self._next_id()
            self.transactions.append({
                "id": t_id, "accountID": self.account_id,
                "type": "STOP_LOSS_ORDER", "reason": "REPLACEMENT",
                "tradeID": command.trade_id, "price": str(target),
            })
            trade["stopLossOrder"]["price"] = str(target)
            reply = {"stopLossOrderTransaction": self.transactions[-1],
                     "lastTransactionID": self.cursor}
        elif command.operation in ("PARTIAL", "CLOSE"):
            if (command.path != f"/trades/{command.trade_id}/close"
                or set(command.body) != {"units"}):
                raise ManagementContractError("Malformed close payload")
            current = number(trade["currentUnits"], "current trade units",
                             zero_ok=False)
            amount = (
                abs(current) if command.body["units"] == "ALL"
                else number(command.body["units"], "close units", positive=True)
            )
            if (amount <= 0 or amount > abs(current)
                or (command.operation == "PARTIAL"
                    and amount >= abs(current))
                or (command.operation == "CLOSE"
                    and amount != abs(current))):
                raise ManagementContractError("Synthetic reduction units invalid")
            create = self._next_id()
            market = {
                "id": create, "accountID": self.account_id,
                "type": "MARKET_ORDER", "reason": "TRADE_CLOSE",
                "tradeClose": {"tradeID": command.trade_id,
                               "units": command.body["units"]},
            }
            self.transactions.append(market)
            fill_id = self._next_id()
            effect = {
                "tradeID": command.trade_id, "units": str(amount),
                "realizedPL": "0", "financing": "0",
                "guaranteedExecutionFee": "0", "halfSpreadCost": "0",
                "price": str(trade["price"]),
            }
            fill = {
                "id": fill_id, "accountID": self.account_id,
                "type": "ORDER_FILL", "orderID": create,
                "reason": "MARKET_ORDER_TRADE_CLOSE",
                "time": iso_utc(utc_now()),
                "pl": "0", "financing": "0", "commission": "0",
                "guaranteedExecutionFee": "0", "halfSpreadCost": "0",
                "units": str(amount), "fullVWAP": str(trade["price"]),
            }
            if amount == abs(current):
                fill["tradesClosed"] = [effect]
                self.trades.remove(trade)
            else:
                fill["tradeReduced"] = effect
                sign = 1 if current > 0 else -1
                trade["currentUnits"] = str(sign * (abs(current) - amount))
            self.transactions.append(fill)
            reply = {"orderCreateTransaction": market,
                     "orderFillTransaction": fill,
                     "lastTransactionID": self.cursor}
        else:
            raise ManagementContractError("Unknown synthetic action")
        if self.effect_mode == "LOST_ACK":
            raise TimeoutError("Synthetic acknowledgment lost after effect")
        return BrokerReply(200, reply)


class OfflineManagementEngine:
    """One staged synthetic operation and GET-only verification under lease."""

    def __init__(self, reconciler: AccountReconciler):
        if not isinstance(reconciler, AccountReconciler):
            raise ManagementContractError("Reconciler required")
        self.reconciler = reconciler
        self.manager = TradeManagement(reconciler)

    def _dispatch(self, command, transport):
        if (not self.reconciler._active
            or type(transport) is not SyntheticLifecycleBroker
            or self.reconciler.transport is not transport
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False):
            raise ManagementContractError("Offline lease/transport/write gate required")
        saved = self.reconciler.store.load()
        record = TradeRecord.from_dict(saved["trades"][command.trade_id])
        staged = build_command(
            record, command.operation, phase=command.phase,
            close_intent=saved.get("close_intents", {}).get(command.trade_id),
        )
        if staged != command:
            raise ManagementContractError("Staged action changed before dispatch")
        # Independent synthetic account GET and trade provenance check.
        cursor, live_trades, orders = self.reconciler._account()
        live = live_trades.get(command.trade_id)
        if (cursor != saved["cursor"] or orders
            or not isinstance(live, dict)
            or live.get("instrument") != record.instrument
            or live.get("clientExtensions") != {
                "id": record.attempt_id, "tag": "algin"
            }
            or number(live.get("currentUnits"), "current units", zero_ok=False)
                != record.current_units
            or number(live.get("stopLossOrder", {}).get("price"), "SL", positive=True)
                != record.current_stop
            or number(live.get("takeProfitOrder", {}).get("price"), "TP", positive=True)
                != record.take_profit):
            raise ManagementContractError("Broker changed since durable stage")
        return transport.submit_synthetic(command)

    def _result(self, operation, trade_id, transport, status, reason,
                tx_id=None):
        return ManagementResult(
            status, trade_id, operation, reason,
            confirmed_transaction_id=tx_id,
            simulated_write_count=transport.simulated_writes,
        )

    def _operate(self, command, transport):
        operation, tid = command.operation, command.trade_id
        if (type(transport) is not SyntheticLifecycleBroker
            or self.reconciler.transport is not transport
            or not self.reconciler._active
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False):
            return ManagementResult(
                "BLOCKED", tid, operation, "OFFLINE_GATE_REQUIRED"
            )
        ambiguous = False
        try:
            reply = self._dispatch(command, transport)
            # A 200 response is not proof: only the sinceid transaction +
            # fully reconciled broker snapshot may confirm the operation.
            if reply.status_code != 200 or not isinstance(reply.body, dict):
                ambiguous = True
        except Exception:
            ambiguous = True
        if ambiguous:
            try:
                self.manager.mark_unknown(
                    tid, operation=operation, phase=command.phase
                )
            except Exception:
                # A durable INTENT also blocks retries after an interrupted
                # UNKNOWN transition. Do not delete/recreate the reservation.
                pass
        try:
            report = self.reconciler.run()
            if report.blocked_reasons:
                return self._result(
                    operation, tid, transport, "UNKNOWN", "RECONCILIATION_BLOCKED"
                )
            state = self.reconciler.store.load()
            record = TradeRecord.from_dict(state["trades"][tid])
            if operation == "BE":
                done = record.be_state == "CONFIRMED"
                confirmed_tx = record.be_transaction_id
            elif operation == "PARTIAL":
                part = record.partial_phases[command.phase]
                done = part["state"] == "CONFIRMED"
                confirmed_tx = part.get("transaction_id")
            else:
                close = state.get("close_intents", {}).get(tid, {})
                if close.get("status") == "SUPERSEDED":
                    return self._result(
                        operation, tid, transport, "SUPERSEDED",
                        "BROKER_CLOSE_SUPERSEDED_ACTION",
                        close["confirmed_transaction_id"],
                    )
                done = (close.get("status") == "CONFIRMED"
                        and record.closed and record.result_counted)
                confirmed_tx = close.get("confirmed_transaction_id")
            if not done or confirmed_tx is None:
                return self._result(
                    operation, tid, transport, "UNKNOWN", "NO_VERIFIED_BROKER_EFFECT"
                )
            tx = confirmed_tx
            return self._result(
                operation, tid, transport, "CONFIRMED",
                "GET_ONLY_TRANSACTION_REPLAY_PROVED", tx
            )
        except Exception:
            return self._result(
                operation, tid, transport, "UNKNOWN", "BROKER_REPLAY_INCONCLUSIVE"
            )

    def _ready_to_stage(self, transport):
        # Reject untrusted/real transport BEFORE touching durable state.
        return (
            self.reconciler._active
            and type(transport) is SyntheticLifecycleBroker
            and self.reconciler.transport is transport
            and config.ORDER_WRITES_ENABLED is False
            and _PRACTICE_WRITES_RELEASED is False
        )

    def stage_and_simulate_be(self, tid, rules: InstrumentRules, *, bid, ask,
                              transport):
        if not self._ready_to_stage(transport):
            return ManagementResult("BLOCKED", tid, "BE", "OFFLINE_GATE_REQUIRED")
        self.manager.stage_break_even(tid, rules, bid=bid, ask=ask)
        saved = self.reconciler.store.load()
        cmd = build_command(
            TradeRecord.from_dict(saved["trades"][tid]), "BE"
        )
        return self._operate(cmd, transport)

    def stage_and_simulate_partial(
        self, tid, rules: InstrumentRules, *, bid, ask, transport,
        phase="phase1", fraction=Decimal("0.5")
    ):
        if not self._ready_to_stage(transport):
            return ManagementResult("BLOCKED", tid, "PARTIAL", "OFFLINE_GATE_REQUIRED")
        self.manager.stage_partial(
            tid, rules, bid=bid, ask=ask,
            phase=phase, fraction=fraction,
        )
        saved = self.reconciler.store.load()
        cmd = build_command(
            TradeRecord.from_dict(saved["trades"][tid]),
            "PARTIAL", phase=phase,
        )
        return self._operate(cmd, transport)

    def stage_and_simulate_close(self, tid, *, transport, source="SYSTEM"):
        if not self._ready_to_stage(transport):
            return ManagementResult("BLOCKED", tid, "CLOSE", "OFFLINE_GATE_REQUIRED")
        self.manager.stage_close(tid, source=source)
        saved = self.reconciler.store.load()
        cmd = build_command(
            TradeRecord.from_dict(saved["trades"][tid]), "CLOSE",
            close_intent=saved["close_intents"][tid],
        )
        return self._operate(cmd, transport)
