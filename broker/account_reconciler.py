"""Read-only full account + since-ID transaction reconciliation for Category 4.

Never adopt a foreign trade. Never infer closure from a missing open trade.
The broker cursor is committed only after the full state passes validation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from broker.practice_transport import BrokerReply, PracticeTransport
from risk.trade_lifecycle import LifecycleError, TradeRecord
from risk.trade_plan import number
from storage.lifecycle_state import LifecycleStore, ProcessLease, StateError
from storage.intent_journal import IntentJournal, JournalError
from utils.time_tools import parse_utc


class ReconcileError(RuntimeError):
    """Never continue automated management from an inconsistent account."""


def _txid(x):
    return isinstance(x, str) and x.isascii() and x.isdecimal() and 1 <= len(x) <= 50


@dataclass(frozen=True)
class ReconcileReport:
    cursor: str
    owned_open_trades: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    new_closed_trade_ids: tuple[str, ...]
    entry_allowed: bool = False


class AccountReconciler:
    """Use as a held process context, including outside entry-session hours."""

    def __init__(self, account_id: str, *, transport=None, journal=None,
                 store=None, lease=None, directory=".local"):
        self.account_id = account_id
        self.transport = transport if transport is not None else PracticeTransport()
        self.journal = journal if journal is not None else IntentJournal(account_id)
        self.store = store if store is not None else LifecycleStore(
            account_id, directory=directory
        )
        self.lease = lease if lease is not None else ProcessLease(
            account_id, directory=directory
        )
        if self.journal.account_id != account_id or self.store.account_id != account_id:
            raise ReconcileError("Cross-account journal or lifecycle state")
        if self.store.environment != "practice":
            raise ReconcileError("Only OANDA Practice state permitted")
        self._active = False

    def __enter__(self):
        self.lease.acquire()
        self._active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self._active = False
        self.lease.release()
        return False

    def _read(self, path, params=None):
        try:
            reply = self.transport.read(path, params=params)
        except Exception as exc:
            raise ReconcileError("Broker GET unavailable; fail closed") from exc
        if (not isinstance(reply, BrokerReply) or reply.status_code != 200
            or not isinstance(reply.body, dict)):
            raise ReconcileError("Incomplete OANDA account/transaction response")
        return reply.body

    def _account(self):
        body = self._read("")
        try:
            raw = body["account"]
            cursor = body["lastTransactionID"]
            if (not isinstance(raw, dict) or raw.get("id") != self.account_id
                or not _txid(cursor) or raw.get("mt4AccountID") is not None):
                raise ReconcileError("Wrong account, cursor or MT4 account")
            trades, orders = raw["trades"], raw["orders"]
            if not isinstance(trades, list) or not isinstance(orders, list):
                raise ReconcileError("Missing full trade/order inventory")
            if (type(raw["openTradeCount"]) is not int
                or type(raw["pendingOrderCount"]) is not int
                or raw["openTradeCount"] != len(trades)
                or raw["pendingOrderCount"] != len(orders)):
                raise ReconcileError("Account inventory count mismatch")
            if not all(isinstance(t, dict) for t in trades + orders):
                raise ReconcileError("Malformed broker item")
            if len({t.get("id") for t in trades}) != len(trades):
                raise ReconcileError("Duplicate broker open trade IDs")
            if len({o.get("id") for o in orders}) != len(orders):
                raise ReconcileError("Duplicate broker pending order IDs")
            for order in orders:
                if not _txid(order.get("id")):
                    raise ReconcileError("Invalid pending order ID")
            for trade in trades:
                if not _txid(trade.get("id")) or trade.get("state", "OPEN") != "OPEN":
                    raise ReconcileError("Invalid open trade in broker snapshot")
            return cursor, {t["id"]: t for t in trades}, tuple(orders)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconcileError("Malformed OANDA full account snapshot") from exc

    @staticmethod
    def _known_trade(trade, journal):
        """Create initial state ONLY from complete durable order/fill evidence."""
        ext = trade.get("clientExtensions")
        if not isinstance(ext, dict) or ext.get("tag") != "algin":
            raise ReconcileError("Foreign/manual trade cannot be adopted")
        attempt_id = ext.get("id")
        candidates = [
            a for a in journal if a.get("attempt_id") == attempt_id
            and a.get("status") == "FILLED"
            and a.get("trade_id") == trade["id"]
            and a.get("protection_verified") is True
        ]
        if len(candidates) != 1:
            raise ReconcileError("No unique verified fill journal for open trade")
        entry = candidates[0]
        try:
            order = entry["request"]["order"]
            grade = entry["grade"]
            initial_units = number(entry["filled_units"], "filled units", zero_ok=False)
            actual_units = number(trade["currentUnits"], "trade current units", zero_ok=False)
            if (initial_units != actual_units or
                initial_units != number(trade["initialUnits"], "initial broker units", zero_ok=False)
                or number(entry["fill_price"], "fill price") != number(trade["price"], "entry price")):
                raise ReconcileError("Trade already reduced or fill changed without state")
            original_stop = number(order["stopLossOnFill"]["price"], "original stop", positive=True)
            actual_stop = number(trade["stopLossOrder"]["price"], "broker stop", positive=True)
            target = number(trade["takeProfitOrder"]["price"], "broker TP", positive=True)
            if original_stop != actual_stop or (
                target != number(order["takeProfitOnFill"]["price"], "intent TP", positive=True)
            ):
                raise ReconcileError("Trade protections moved without durable history")
            if (order["instrument"] != trade["instrument"]
                or grade not in ("A", "B")):
                raise ReconcileError("Incomplete original trade provenance")
            return TradeRecord(
                trade_id=trade["id"], attempt_id=attempt_id,
                instrument=trade["instrument"], direction="LONG" if initial_units > 0 else "SHORT",
                grade=grade, entry_price=entry["fill_price"],
                initial_stop=original_stop, take_profit=target,
                initial_units=initial_units, current_units=actual_units,
                current_stop=actual_stop, risk_pct=entry["risk_pct"],
                opened_at=parse_utc(trade["openTime"])
            )
        except (KeyError, TypeError, ValueError, LifecycleError) as exc:
            raise ReconcileError("Cannot recover complete trade lifetime from journal") from exc

    @staticmethod
    def _apply_transactions(state, transactions, target_cursor):
        """Consume only unprocessed typed transaction evidence, never duplicates."""
        old = int(state["cursor"])
        seen = set(state["processed_transactions"])
        new_closures = []
        previous_id = old
        for tx in transactions:
            if not isinstance(tx, dict) or not _txid(tx.get("id")):
                raise ReconcileError("Invalid broker transaction")
            tid = tx["id"]
            if tid in seen or int(tid) <= previous_id or int(tid) > int(target_cursor):
                raise ReconcileError("Duplicate/out-of-order transaction batch")
            previous_id = int(tid)
            if tx.get("accountID") != state["account_id"]:
                raise ReconcileError("Foreign account transaction")
            if tx.get("type") == "ORDER_FILL":
                reduced = tx.get("tradeReduced")
                closed = tx.get("tradesClosed", [])
                if reduced is not None and not isinstance(reduced, dict):
                    raise ReconcileError("Invalid reduced trade transaction")
                if not isinstance(closed, list):
                    raise ReconcileError("Invalid closed trade list")
                for item, is_closed in (
                    ([(reduced, False)] if reduced else [])
                    + [(x, True) for x in closed]
                ):
                    if not isinstance(item, dict) or not _txid(item.get("tradeID")):
                        raise ReconcileError("Malformed ORDER_FILL trade effect")
                    trade_id = item["tradeID"]
                    if trade_id not in state["trades"]:
                        # Manual/foreign trade closes are not accounted by Algin.
                        continue
                    trade = TradeRecord.from_dict(state["trades"][trade_id])
                    if trade.closed:
                        raise ReconcileError("Confirmed close on already-closed trade")
                    amount = number(item["units"], "closing units", zero_ok=False).copy_abs()
                    if amount > abs(trade.current_units):
                        raise ReconcileError("Close exceeds known remaining units")
                    sign = 1 if trade.direction == "LONG" else -1
                    new_remaining = sign * (abs(trade.current_units) - amount)
                    # Only an already-staged partial phase can be confirmed.
                    if not is_closed:
                        phases = [
                            p for p, d in trade.partial_phases.items()
                            if d["state"] in ("INTENT", "UNKNOWN")
                            and number(d["units"], "partial units", positive=True) == amount
                        ]
                        if len(phases) != 1 or new_remaining == 0:
                            raise ReconcileError("Unattributed or mismatched partial reduction")
                        trade = trade.with_partial_confirmed(
                            phases[0], amount, new_remaining
                        )
                    else:
                        if new_remaining != 0:
                            raise ReconcileError("Close transaction does not close all units")
                        trade = replace(
                            trade, current_units=Decimal("0"), closed=True,
                            closed_at=parse_utc(tx["time"])
                        )
                    realized = number(item["realizedPL"], "realized P/L")
                    trade = replace(
                        trade, net_pl_home=trade.net_pl_home + realized,
                        close_transaction_ids=trade.close_transaction_ids + (tid,)
                    )
                    if is_closed:
                        # One ORDER_FILL can close several trades. Count each
                        # trade once, and each enclosing transaction once.
                        if tid not in state["counted_closures"]:
                            state["counted_closures"].append(tid)
                        if trade_id in state["closed_results"]:
                            raise ReconcileError("Duplicate trade result counting")
                        trade = replace(trade, result_counted=True)
                        state["closed_results"][trade_id] = {
                            "transaction_id": tid, "pl": str(trade.net_pl_home),
                            "closed_at": trade.to_dict()["closed_at"],
                            "instrument": trade.instrument,
                        }
                        if trade.net_pl_home < 0:
                            from datetime import timedelta
                            from utils.time_tools import iso_utc
                            state["cooldowns"][trade.instrument] = iso_utc(
                                trade.closed_at + timedelta(minutes=30)
                            )
                        new_closures.append(trade_id)
                    state["trades"][trade_id] = trade.to_dict()
            elif tx.get("type") == "STOP_LOSS_ORDER":
                trade_id = tx.get("tradeID")
                if trade_id in state["trades"]:
                    trade = TradeRecord.from_dict(state["trades"][trade_id])
                    if trade.be_state in ("INTENT", "UNKNOWN") and (
                        number(tx["price"], "confirmed stop", positive=True)
                        == trade.entry_price
                    ):
                        trade = trade.with_stop_confirmed(tx["price"])
                        state["trades"][trade_id] = trade.to_dict()
                    else:
                        raise ReconcileError("Unattributed stop-loss amendment")
            seen.add(tid)
            state["processed_transactions"].append(tid)
        state["cursor"] = target_cursor
        return new_closures

    def run(self):
        if not self._active:
            raise ReconcileError("Process-wide management lease required")
        cursor, broker_trades, orders = self._account()
        attempts = self.journal.snapshot()
        if len({a["attempt_id"] for a in attempts}) != len(attempts):
            raise ReconcileError("Duplicate durable order intent")
        persisted = self.store.load()
        if persisted is None:
            # First bootstrap: a verified fill can be reconstructed from the
            # original intent, but manual/unmatched trades must remain blocked.
            verified = {}
            blocked = []
            for tid, raw in broker_trades.items():
                try:
                    record = self._known_trade(raw, attempts)
                    verified[tid] = record.to_dict()
                except (ReconcileError, LifecycleError):
                    blocked.append("Unattributed broker trade " + tid)
            persisted = self.store.initialize(cursor, verified)
            if blocked:
                return ReconcileReport(cursor, tuple(verified), tuple(blocked), ())
        if int(cursor) < int(persisted["cursor"]):
            raise ReconcileError("Broker cursor regressed")
        proposed = dict(persisted)
        proposed["trades"] = dict(persisted["trades"])
        proposed["processed_transactions"] = list(persisted["processed_transactions"])
        proposed["counted_closures"] = list(persisted["counted_closures"])
        proposed["closed_results"] = dict(persisted["closed_results"])
        proposed["cooldowns"] = dict(persisted["cooldowns"])
        new_closures = []
        if cursor != persisted["cursor"]:
            payload = self._read("/transactions/sinceid", {"id": persisted["cursor"]})
            txs = payload.get("transactions")
            if (payload.get("lastTransactionID") != cursor
                or not isinstance(txs, list) or not txs
                or txs[-1].get("id") != cursor):
                raise ReconcileError("Incomplete transaction range or concurrent broker changes")
            try:
                new_closures = self._apply_transactions(proposed, txs, cursor)
            except ReconcileError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # Malformed broker transactions never advance local cursor.
                raise ReconcileError("Malformed transaction history: state unchanged") from exc
        blockers = []
        journal_by_id = {a["attempt_id"]: a for a in attempts}
        for tid, raw in broker_trades.items():
            if tid not in proposed["trades"]:
                blockers.append("Unmanaged or foreign open trade " + tid)
                continue
            rec = TradeRecord.from_dict(proposed["trades"][tid])
            entry = journal_by_id.get(rec.attempt_id)
            ext = raw.get("clientExtensions")
            if (entry is None or entry.get("trade_id") != tid
                or entry.get("status") != "FILLED"
                or not isinstance(ext, dict)
                or ext.get("id") != rec.attempt_id
                or ext.get("tag") != "algin"
                or raw.get("instrument") != rec.instrument):
                blockers.append("Trade ownership mismatch " + tid)
                continue
            try:
                current = number(raw["currentUnits"], "open units", zero_ok=False)
                sl = number(raw["stopLossOrder"]["price"], "live SL", positive=True)
                tp = number(raw["takeProfitOrder"]["price"], "live TP", positive=True)
                if (current != rec.current_units or sl != rec.current_stop
                    or tp != rec.take_profit or rec.closed):
                    raise ReconcileError("Unexpected units/protection state")
            except (KeyError, TypeError, ValueError):
                blockers.append("Trade state/protection divergence " + tid)
        for tid, saved in proposed["trades"].items():
            rec = TradeRecord.from_dict(saved)
            if tid not in broker_trades and not rec.closed:
                blockers.append("Open trade missing without confirmed close " + tid)
        if orders:
            blockers.append("Unreconciled pending broker orders")
        if any(a["status"] in ("PREPARED", "IN_FLIGHT", "UNKNOWN") for a in attempts):
            blockers.append("Ambiguous broker submission in journal")
        if any(TradeRecord.from_dict(v).be_state in ("INTENT", "UNKNOWN")
               or any(x["state"] in ("INTENT", "UNKNOWN")
                      for x in TradeRecord.from_dict(v).partial_phases.values())
               for v in proposed["trades"].values()):
            blockers.append("Unconfirmed BE/partial management operation")
        if blockers:
            # A broker snapshot with unresolved inventory or intents must not
            # make the locally persisted cursor appear fully reconciled.
            # Retain the previous durable snapshot and replay this batch after
            # the discrepancy is investigated. Report no uncommitted closures.
            return ReconcileReport(
                persisted["cursor"], (), tuple(blockers), (),
                entry_allowed=False,
            )
        if cursor != persisted["cursor"]:
            # Persist the complete validated transaction state + cursor in
            # ONE checksummed atomic replacement; never cursor-only.
            self.store.mutate(
                lambda s: s.update(proposed),
                expected_cursor=persisted["cursor"],
            )
        return ReconcileReport(
            cursor, tuple(sorted(
                tid for tid in broker_trades if tid in proposed["trades"]
                and not any(tid in issue for issue in blockers)
            )), tuple(blockers), tuple(new_closures),
            entry_allowed=False,
        )
