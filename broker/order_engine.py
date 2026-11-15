"""OANDA Category-3 order coordinator: exactly one attempted POST, never blind retry.

Broker writes remain hard-disabled by config in this category. Injectable fake
transport exercises the complete request/acknowledgement/reconciliation path.
No attempt is retried, even after 404 during read-only reconciliation.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import re
import uuid

import config
from broker.execution_result import ExecutionResult, Outcome, classify_response
from broker.execution_validation import (
    PreflightRejected, build_market_order, parse_market_quote,
)
from broker.practice_transport import (
    BrokerCommunicationError, BrokerReply, BrokerWriteDisabled, PracticeTransport,
)
from execution.safety_check import SafetyCheck
from risk.trade_plan import InstrumentRules, QuoteConversions, TradePlan, number
from storage.intent_journal import IntentJournal, JournalError, _identity
from utils.time_tools import require_utc
from telemetry.events import AuditEvents, event_id


def _strict_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,50}", value))


def _result_unknown(attempt_id, reason):
    return ExecutionResult(Outcome.UNKNOWN, attempt_id, reason=reason)


class OrderEngine:
    """P19–P30. Live entry is disabled pending separate operational release."""

    def __init__(self, account_id: str, *, transport=None, journal=None, audit=None):
        self.account_id = _identity(account_id, "account ID")
        self.transport = transport if transport is not None else PracticeTransport()
        synthetic_journal = journal is not None
        self.journal = journal if journal is not None else IntentJournal(account_id)
        self.audit = audit if audit is not None else AuditEvents(
            account_id, directory=(
                str(self.journal.path.parent / "audit")
                if synthetic_journal else None
            )
        )
        if self.journal.account_id != account_id:
            raise PreflightRejected("Journal belongs to another account")

    def _read(self, path, *, params=None) -> BrokerReply:
        try:
            answer = self.transport.read(path, params=params)
        except (BrokerCommunicationError, OSError, ValueError) as exc:
            raise PreflightRejected("Broker read unavailable") from exc
        if not isinstance(answer, BrokerReply) or answer.status_code != 200 or (
            not isinstance(answer.body, dict)
        ):
            raise PreflightRejected("Broker read returned uncertain HTTP/JSON")
        return answer

    def _account_preflight(self, plan: TradePlan):
        reply = self._read("")
        body = reply.body
        try:
            raw = body["account"]
            trades, orders = raw["trades"], raw["orders"]
            if (not isinstance(raw, dict) or raw.get("id") != self.account_id
                or not isinstance(trades, list) or not isinstance(orders, list)
                or not all(isinstance(t, dict) for t in trades + orders)
                or not isinstance(raw.get("openTradeCount"), int)
                or not isinstance(raw.get("pendingOrderCount"), int)
                or raw.get("openTradeCount") != len(trades)
                or raw.get("pendingOrderCount") != len(orders)
                or not _strict_id(body.get("lastTransactionID"))):
                raise ValueError("Missing/inconsistent broker account inventory")
        except (KeyError, ValueError, TypeError) as exc:
            raise PreflightRejected("Account snapshot incomplete") from exc
        # P27: do not apply an old monetary risk budget to an account whose
        # current currency/equity/margin has changed since planning.
        try:
            if raw.get("currency") != plan.account_currency:
                raise ValueError("Account currency changed")
            equity = min(
                number(raw["NAV"], "NAV", positive=True),
                number(raw["balance"], "balance", positive=True)
            )
            free_margin = number(raw["marginAvailable"], "available margin")
            if free_margin < 0:
                raise ValueError("Negative available margin")
            grade_pct = Decimal(str(
                config.RISK_GRADE_A_PCT if plan.grade == "A"
                else config.RISK_GRADE_B_PCT
            ))
            if number(plan.risk_budget_home, "risk budget", positive=True) > (
                equity * grade_pct / Decimal("100")
            ):
                raise ValueError("Stale account NAV/balance risk budget")
            if number(plan.estimated_margin_home, "planned margin", positive=True) > (
                free_margin * Decimal(str(config.MAX_MARGIN_FRACTION))
            ):
                raise ValueError("Account margin fell below planned reserve")
        except (KeyError, ValueError, TypeError) as exc:
            raise PreflightRejected("Account risk/margin snapshot no longer matches plan") from exc
        # OANDA explicitly prohibits clientExtensions on MT4-associated
        # accounts. This field is a deny signal when available; Category 4
        # must additionally verify account properties before real activation.
        if raw.get("mt4AccountID") is not None:
            raise PreflightRejected("MT4-associated account: client IDs unavailable")
        # SafetyCheck is the common authority for SL/TP and ownership issues.
        known = self.journal.snapshot()
        issues = SafetyCheck.audit_raw_trades(trades, known)
        if issues:
            raise PreflightRejected("Account has unprotected/manual/unmanaged trades")
        # Category 4 has not provided full risk/state reconciliation; therefore
        # ANY open/pending trade blocks further account mutations for now.
        if trades or orders:
            raise PreflightRejected("Open positions or pending orders require reconciliation")
        if any(x["status"] not in ("REJECTED", "CANCELLED") for x in known):
            raise PreflightRejected("Existing unresolved or filled execution journal")
        return body["lastTransactionID"]

    def submit(
        self, plan: TradePlan, rules: InstrumentRules,
        conversion: QuoteConversions, *, signal_id: str,
        now: datetime,
    ) -> ExecutionResult:
        """Stage intent DURABLY, transmit once, persist result, verify protection.

        In the shipping category-3 build the production transport blocks POST;
        only offline test transports can exercise this path.
        """
        _identity(signal_id, "signal ID")
        now = require_utc(now)
        if isinstance(self.transport, PracticeTransport):
            from broker.practice_transport import _PRACTICE_WRITES_RELEASED
            if not _PRACTICE_WRITES_RELEASED or config.ORDER_WRITES_ENABLED is not True:
                raise BrokerWriteDisabled(
                    "Category 3 is not authorized for Practice order writes"
                )
        if not isinstance(plan, TradePlan) or not isinstance(rules, InstrumentRules):
            raise PreflightRejected("Typed risk plan/instrument rules required")
        if not isinstance(conversion, QuoteConversions):
            raise PreflightRejected("Typed quote/home conversion context required")
        try:
            checkpoint = self._account_preflight(plan)
        except PreflightRejected:
            self.audit.emit(
                "RISK", eid=event_id("blocked-account-risk", signal_id),
                stage="RISK", outcome="BLOCKED",
                reason_code="ACCOUNT_RISK_OR_MARGIN_UNVERIFIED",
                signal_id=signal_id, instrument=plan.instrument
            )
            raise
        try:
            price_reply = self._read(
                "/pricing", params={"instruments": plan.instrument}
            )
            quote = parse_market_quote(
                price_reply.body, plan.instrument, rules, now=now,
                max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
                max_spread_pips=Decimal(str(config.MAX_SPREAD_PIPS)),
            )
        except PreflightRejected:
            self.audit.emit(
                "SIGNAL_BLOCK", eid=event_id("blocked-quote", signal_id),
                stage="PREFLIGHT", outcome="BLOCKED",
                reason_code="SPREAD_OR_QUOTE_UNVERIFIED",
                signal_id=signal_id, instrument=plan.instrument
            )
            raise
        attempt_id = "algin-" + uuid.uuid4().hex
        try:
            prepared = build_market_order(
                plan, rules, conversion, quote, attempt_id=attempt_id,
                signal_id=signal_id,
                slippage_pips=Decimal(str(config.SLIPPAGE_ALLOWED)),
                minimum_stop_pips=Decimal(str(config.MIN_DISTANCE_PIPS)),
            )
        except PreflightRejected:
            self.audit.emit(
                "RISK", eid=event_id("blocked-plan", signal_id),
                stage="RISK", outcome="BLOCKED",
                reason_code="FINAL_ORDER_RISK_REJECTED",
                signal_id=signal_id, instrument=plan.instrument
            )
            raise
        # The HTTP request is not permitted until this write is durable.
        try:
            self.journal.begin(
                signal_id=signal_id, attempt_id=attempt_id,
                request=prepared.payload,
                risk_pct=format(number(plan.reserved_risk_pct, "reserved risk", positive=True), "f"),
                margin_home=format(number(plan.estimated_margin_home, "margin", positive=True), "f"),
                grade=plan.grade,
            )
        except JournalError:
            self.audit.emit(
                "RISK", eid=event_id("blocked-journal", signal_id),
                stage="RISK", outcome="BLOCKED",
                reason_code="INTENT_JOURNAL_REJECTED",
                signal_id=signal_id, instrument=plan.instrument
            )
            raise
        # Both the intent AND the sanitized attempt event must be durable
        # BEFORE the single permitted (simulated) broker POST.
        self.audit.emit(
            "RISK", eid=event_id("risk", attempt_id), stage="RISK",
            outcome="OBSERVED", reason_code="PLAN_PREFLIGHT_VALIDATED",
            signal_id=signal_id, attempt_id=attempt_id,
            instrument=plan.instrument, direction=plan.direction,
            grade=plan.grade, risk_pct=plan.reserved_risk_pct
        )
        self.audit.emit(
            "ORDER_ATTEMPT", eid=event_id("prepare", attempt_id),
            stage="BROKER", outcome="PREPARED",
            signal_id=signal_id, attempt_id=attempt_id,
            instrument=plan.instrument, direction=plan.direction,
            grade=plan.grade, units=prepared.units,
            requested_price=prepared.bound, risk_pct=plan.reserved_risk_pct,
            spread_pips=prepared.spread_pips
        )
        self.journal.transition(attempt_id, "IN_FLIGHT", status_note=f"checkpoint={checkpoint}")
        # No loop. No requests adapter retries. No retry even if POST never
        # received a reply. Reconciliation is GET-only.
        try:
            reply = self.transport.submit_market_order(prepared.payload)
            if not isinstance(reply, BrokerReply):
                raise BrokerCommunicationError("Invalid transport response")
            result = classify_response(reply, attempt_id)
        except Exception:
            result = _result_unknown(attempt_id, "Mutation transport outcome unknown")
        self._persist_outcome(result)
        if result.outcome == Outcome.UNKNOWN:
            # One read-only lookup is useful, but lack of a matching order is
            # never proof of non-execution or authorization for another POST.
            return self.reconcile(attempt_id)
        if result.outcome == Outcome.FILLED:
            return self._postfill_protection(result, prepared.payload)
        return result

    def _persist_outcome(self, result: ExecutionResult):
        data = {
            "request_id": result.request_id,
            "order_id": result.order_id,
            "trade_id": result.trade_id,
            "status_note": result.reason,
            "protection_verified": result.protection_verified,
        }
        if result.filled_units is not None:
            data["filled_units"] = format(result.filled_units, "f")
        if result.fill_price is not None:
            data["fill_price"] = format(result.fill_price, "f")
        if result.trade_reduced is not None:
            data["trade_reduced"] = result.trade_reduced
        if result.trades_closed:
            data["trades_closed"] = list(result.trades_closed)
        record = self.journal.transition(result.attempt_id, result.outcome.value, **data)
        self.audit.emit(
            "ORDER_RESULT",
            eid=event_id("outcome", result.attempt_id, result.outcome.value),
            stage="BROKER", outcome=result.outcome.value,
            reason_code=("BROKER_RESULT_UNKNOWN" if result.outcome == Outcome.UNKNOWN
                         else "BROKER_TRANSACTION_CONFIRMED"),
            signal_id=record["signal_id"], attempt_id=result.attempt_id,
            order_id=result.order_id or "", trade_id=result.trade_id or "",
            instrument=record["request"]["order"]["instrument"],
            executed_price=result.fill_price, units=result.filled_units,
            grade=record.get("grade") or ""
        )

    def _postfill_protection(self, result: ExecutionResult, payload: dict) -> ExecutionResult:
        if result.trade_reduced is not None or result.trades_closed:
            return replace(
                result, reason="Unexpected reduction or close in OPEN_ONLY order: entry lock"
            )
        if result.filled_units is None or result.filled_units != number(
            payload["order"]["units"], "requested units", zero_ok=False
        ):
            return replace(
                result, reason="Filled quantity differs from FOK order: entry lock"
            )
        if result.trade_id is None:
            # A fill that reduced/closed positions is still a real execution,
            # but it is NOT an opened/protected Algin trade.
            return replace(
                result, reason="Broker fill changed existing positions: entry lock"
            )
        try:
            reply = self._read("/trades/" + result.trade_id)
            trade = reply.body["trade"]
            expected = payload["order"]
            ext = trade["clientExtensions"]
            if (not isinstance(trade, dict)
                or trade.get("id") != result.trade_id
                or trade.get("state") != "OPEN"
                or trade.get("instrument") != expected["instrument"]
                or not isinstance(ext, dict)
                or ext.get("id") != result.attempt_id or ext.get("tag") != "algin"):
                raise ValueError("Trade ownership or identity mismatch")
            actual_units = number(trade["currentUnits"], "trade units", zero_ok=False)
            if actual_units != result.filled_units:
                raise ValueError("Filled units and live trade units disagree")
            sl = number(trade["stopLossOrder"]["price"], "SL", positive=True)
            tp = number(trade["takeProfitOrder"]["price"], "TP", positive=True)
            if (sl != number(expected["stopLossOnFill"]["price"], "expected SL")
                or tp != number(expected["takeProfitOnFill"]["price"], "expected TP")):
                raise ValueError("Broker SL/TP not equal to requested protection")
        except (PreflightRejected, ValueError, TypeError, KeyError, AttributeError):
            return replace(
                result, reason="Fill confirmed but protection/ownership unverified"
            )
        # Confirm protection status durably; no autonomous repair or close.
        saved = self.journal.transition(
            result.attempt_id, "FILLED", protection_verified=True,
            status_note="Broker trade ownership and SL/TP confirmed",
        )
        self.audit.emit(
            "TRADE", eid=event_id("protected", result.attempt_id, result.trade_id),
            stage="RECONCILIATION", outcome="VERIFIED",
            reason_code="TRADE_OWNERSHIP_SL_TP_VERIFIED",
            signal_id=saved["signal_id"], attempt_id=result.attempt_id,
            order_id=result.order_id or "", trade_id=result.trade_id,
            instrument=saved["request"]["order"]["instrument"],
            executed_price=result.fill_price,
            units=result.filled_units, grade=saved.get("grade") or ""
        )
        return replace(
            result, protection_verified=True, reason="Fill and SL/TP confirmed"
        )

    def reconcile(self, attempt_id: str) -> ExecutionResult:
        """GET-only audit of a previously persisted attempt. NEVER resubmits."""
        record = self.journal.get(attempt_id)
        if record["status"] in ("REJECTED", "CANCELLED"):
            return ExecutionResult(
                Outcome(record["status"]), attempt_id,
                reason="Terminal local journal; no resend"
            )
        if record["status"] == "FILLED":
            # A protection problem may only be rechecked, not retried as an order.
            return ExecutionResult(
                Outcome.FILLED, attempt_id,
                trade_id=record.get("trade_id"),
                protection_verified=record.get("protection_verified", False),
                reason="Existing fill journal; no resend"
            )
        try:
            response = self._read("/orders/@" + attempt_id)
            order = response.body["order"]
            if (not isinstance(order, dict)
                or order.get("clientExtensions", {}).get("id") != attempt_id
                or not _strict_id(order.get("id"))
                or order.get("instrument") != record["request"]["order"]["instrument"]):
                raise ValueError("Broker order identity not verified")
            state = order["state"]
            if state == "FILLED":
                tx_id = order["fillingTransactionID"]
                expected_type = "ORDER_FILL"
            elif state == "CANCELLED":
                tx_id = order["cancellingTransactionID"]
                expected_type = "ORDER_CANCEL"
            else:
                raise ValueError("Order has no verified terminal outcome")
            if not _strict_id(tx_id):
                raise ValueError("Missing broker transaction identity")
            tx_reply = self._read("/transactions/" + tx_id)
            tx = tx_reply.body["transaction"]
            if (not isinstance(tx, dict) or tx.get("id") != tx_id
                or tx.get("type") != expected_type
                or tx.get("orderID") != order["id"]):
                raise ValueError("Broker transaction does not match order")
            wrapper = (
                {"orderFillTransaction": tx,
                 "orderCreateTransaction": {"id": order["id"]}}
                if state == "FILLED" else {"orderCancelTransaction": tx}
            )
            classified = classify_response(
                BrokerReply(201, wrapper, tx_reply.request_id), attempt_id
            )
            if classified.outcome not in (Outcome.FILLED, Outcome.CANCELLED):
                raise ValueError("Transaction cannot prove terminal order outcome")
            if record["status"] == "PREPARED":
                self.journal.transition(attempt_id, "UNKNOWN")
            self._persist_outcome(classified)
            if classified.outcome == Outcome.FILLED:
                return self._postfill_protection(classified, record["request"])
            return classified
        except (PreflightRejected, KeyError, ValueError, TypeError, AttributeError):
            # An order GET 404 or a malformed transaction proves NOTHING.
            if record["status"] in ("PREPARED", "IN_FLIGHT"):
                self.journal.transition(
                    attempt_id, "UNKNOWN",
                    status_note="Read-only reconciliation inconclusive"
                )
            return _result_unknown(attempt_id, "No conclusive broker confirmation")
