"""Category 05: deterministic strategy-to-order coordinator, OFFLINE ONLY.

The existing main.py remains OBSERVE_ONLY. A prepared TradePlan is advisory.
Only the exact in-memory SimulatedOrderTransport may reach OrderEngine.submit
through this module; neither a custom transport nor a PracticeTransport is
accepted. A fresh full risk snapshot is mandatory before the synthetic POST.

No real OANDA credentials, HTTP clients, network requests, or broker writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Callable

import config
from broker.account_reconciler import ReconcileReport
from broker.execution_result import ExecutionResult, Outcome
from broker.execution_validation import PreflightRejected
from broker.order_engine import OrderEngine
from broker.practice_transport import (
    BrokerCommunicationError, BrokerReply, BrokerWriteDisabled,
    _PRACTICE_WRITES_RELEASED,
)
from risk.reconciled_account import (
    RiskEvidenceError, RiskHistoryAnchor, build_risk_readiness,
)
from risk.trade_plan import (
    AccountFunds, InstrumentRules, PlanRejected, QuoteConversions,
    TradePlan, TradePlanner,
)
from storage.intent_journal import IntentJournal, JournalError
from strategy.strategy import StrategySignal
from utils.time_tools import iso_utc, require_utc


@dataclass(frozen=True)
class EntryGates:
    """Separate, independently verified preconditions (synthetic in tests)."""
    completed_candle_verified: bool = False
    session_allowed: bool = False
    news_clear: bool = False
    broker_tradeable: bool = False


@dataclass(frozen=True)
class AccountEvidence:
    """One reconciled read generation; caller must hold the account lease.

    A separate state read and account GET are not guaranteed atomic in live
    trading. The coordinator therefore never gives live-entry permission.
    """
    funds: AccountFunds
    state: dict
    journal_attempts: tuple[dict, ...]
    broker_trades: tuple[dict, ...]
    broker_orders: tuple[dict, ...]
    report: ReconcileReport
    history_anchor: RiskHistoryAnchor | None = None


@dataclass(frozen=True)
class CoordinatorRequest:
    signal: StrategySignal
    candle_time: datetime
    atr: Decimal
    rules: InstrumentRules
    quote: QuoteConversions
    evidence: AccountEvidence
    gates: EntryGates
    now: datetime
    structure_level: Decimal | None = None


@dataclass(frozen=True)
class CoordinationResult:
    status: str
    reason_code: str
    signal_id: str | None = None
    plan: TradePlan | None = None
    execution: ExecutionResult | None = None
    # This is intentionally NEVER a release of the production entry gate.
    entry_allowed: bool = False


def stable_signal_id(account_id: str, instrument: str, direction: str,
                     completed_candle: datetime) -> str:
    """One account/instrument/direction/M15 candle = one lifetime signal ID.

    Score, grade, wall-clock retry and process UUID are intentionally absent.
    Invalid identities are refused rather than silently normalized.
    """
    stamp = require_utc(completed_candle)
    if (not isinstance(account_id, str) or not account_id
        or not all(ch.isascii() and (ch.isalnum() or ch in "._-")
                   for ch in account_id)
        or not isinstance(instrument, str)
        or instrument.count("_") != 1
        or not isinstance(direction, str)
        or direction not in ("LONG", "SHORT")):
        raise ValueError("Unidentified account/signal direction/instrument")
    # Require the UTC *opening timestamp* of a 15-minute candle.
    # Otherwise an operator could submit the same bar under two slightly
    # different timestamps and defeat durable signal deduplication.
    if stamp.minute % 15 or stamp.second or stamp.microsecond:
        raise ValueError("M15 candle must start on a UTC quarter-hour")
    key = json.dumps(
        (account_id, instrument, direction, "M15", iso_utc(stamp)),
        separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "algin-signal-" + hashlib.sha256(key).hexdigest()[:40]


class SimulatedOrderTransport:
    """Sealed, in-memory fixture for the real OrderEngine contract.

    No HTTP client and no URL; the sole 'POST' records a synthetic order.
    This is deliberately not a PracticeTransport subclass.
    """
    def __init__(self, funds: AccountFunds, quote: QuoteConversions,
                 *, mode: str = "FILLED"):
        if (not isinstance(funds, AccountFunds)
            or not isinstance(quote, QuoteConversions)
            or mode not in ("FILLED", "REJECTED", "TIMEOUT")
            or funds.broker_open_count != 0 or funds.broker_pending_count != 0):
            raise ValueError("Simulation requires a flat, typed broker fixture")
        self.funds = funds
        self.quote = quote
        self.mode = mode
        self.post_count = 0
        self.calls = []
        self.last_payload = None
        self.trade = None
        self._account = {
            "lastTransactionID": funds.transaction_id,
            "account": {
                "id": funds.account_id, "currency": funds.currency,
                "balance": str(funds.balance), "NAV": str(funds.nav),
                "marginAvailable": str(funds.margin_available),
                "marginUsed": str(funds.margin_used),
                "openTradeCount": 0, "pendingOrderCount": 0,
                "trades": [], "orders": [],
            },
        }
        self._pricing = {"prices": [{
            "instrument": quote.instrument, "status": "tradeable",
            "bids": [{"price": str(quote.bid)}],
            "asks": [{"price": str(quote.ask)}],
            "time": iso_utc(quote.timestamp),
        }]}

    def matches(self, evidence: AccountEvidence, quote: QuoteConversions) -> bool:
        """Independent fixture check immediately before synthetic submission."""
        a = self._account
        raw = a["account"]
        f = evidence.funds
        return (
            f == self.funds and quote == self.quote
            and a["lastTransactionID"] == f.transaction_id
            and raw["id"] == f.account_id and raw["currency"] == f.currency
            and raw["balance"] == str(f.balance)
            and raw["NAV"] == str(f.nav)
            and raw["marginAvailable"] == str(f.margin_available)
            and raw["openTradeCount"] == 0 and raw["pendingOrderCount"] == 0
            and not raw["trades"] and not raw["orders"]
            and self.trade is None and self.post_count == 0
        )

    def read(self, path, *, params=None):
        self.calls.append(("GET", path))
        if path == "":
            return BrokerReply(200, self._account)
        if path == "/pricing":
            return BrokerReply(200, self._pricing)
        if path == "/trades/2201" and self.trade is not None:
            return BrokerReply(200, {"trade": self.trade})
        if path.startswith("/orders/@") or path.startswith("/transactions/"):
            return BrokerReply(404, {"errorMessage": "synthetic unknown"})
        return BrokerReply(404, {"errorMessage": "synthetic not found"})

    def submit_market_order(self, payload):
        self.post_count += 1
        self.calls.append(("POST_SIMULATED", "/orders"))
        self.last_payload = payload
        if self.post_count != 1:
            raise BrokerCommunicationError("Repeated synthetic POST blocked")
        if self.mode == "TIMEOUT":
            raise BrokerCommunicationError("Synthetic lost acknowledgment")
        if self.mode == "REJECTED":
            return BrokerReply(400, {
                "orderRejectTransaction": {
                    "id": "2200", "type": "MARKET_ORDER_REJECT"
                },
            })
        order = payload["order"]
        side_price = self.quote.ask if Decimal(order["units"]) > 0 else self.quote.bid
        self.trade = {
            "id": "2201", "state": "OPEN",
            "instrument": order["instrument"],
            "price": str(side_price), "currentUnits": order["units"],
            "clientExtensions": order["tradeClientExtensions"],
            "stopLossOrder": order["stopLossOnFill"],
            "takeProfitOrder": order["takeProfitOnFill"],
        }
        self._account["account"]["trades"] = [self.trade]
        self._account["account"]["openTradeCount"] = 1
        return BrokerReply(201, {
            "orderCreateTransaction": {"id": "2200"},
            "orderFillTransaction": {
                "id": "2201", "type": "ORDER_FILL",
                "orderID": "2200", "price": str(side_price),
                "units": order["units"],
                "tradeOpened": {"tradeID": "2201", "units": order["units"]},
            },
        })


class TradeCoordinator:
    """Strict offline path: filters -> reconciled risk -> plan -> fake OrderEngine."""

    def __init__(self, journal: IntentJournal, *, planner=None):
        if not isinstance(journal, IntentJournal):
            raise ValueError("Durable intent journal required")
        self.journal = journal
        self.planner = planner if planner is not None else TradePlanner()

    def preview(self, req: CoordinatorRequest) -> CoordinationResult:
        signal_id = None
        try:
            if (not isinstance(req, CoordinatorRequest)
                or not isinstance(req.signal, StrategySignal)
                or not isinstance(req.gates, EntryGates)
                or not isinstance(req.evidence, AccountEvidence)
                or not isinstance(req.rules, InstrumentRules)
                or not isinstance(req.quote, QuoteConversions)
                or not isinstance(req.evidence.report, ReconcileReport)
                or not isinstance(req.evidence.funds, AccountFunds)):
                return CoordinationResult("BLOCKED", "CONTEXT_INVALID")
            now = require_utc(req.now)
            candle = require_utc(req.candle_time)
            signal_time = require_utc(req.signal.timestamp)
            if (candle > now or signal_time < candle
                or signal_time > now + timedelta(seconds=2)
                or now - candle > timedelta(
                    minutes=config.SIGNAL_CANDLE_MAX_AGE_MINUTES
                )):
                return CoordinationResult("BLOCKED", "CANDLE_EXPIRED")
            if now - candle < timedelta(minutes=15):
                return CoordinationResult("BLOCKED", "CANDLE_NOT_COMPLETE")
            signal_id = stable_signal_id(
                req.evidence.funds.account_id, req.signal.instrument,
                req.signal.direction, candle,
            )
            gates = req.gates
            for name, verified in (
                ("CANDLE_UNVERIFIED", gates.completed_candle_verified),
                ("SESSION_UNVERIFIED", gates.session_allowed),
                ("NEWS_UNVERIFIED", gates.news_clear),
                ("TRADEABILITY_UNVERIFIED", gates.broker_tradeable),
            ):
                if type(verified) is not bool or verified is not True:
                    return CoordinationResult("BLOCKED", name, signal_id)
            if req.signal.grade not in ("A", "B"):
                return CoordinationResult("BLOCKED", "GRADE_NOT_TRADEABLE", signal_id)
            if self.journal.account_id != req.evidence.funds.account_id:
                return CoordinationResult("BLOCKED", "ACCOUNT_ID_MISMATCH", signal_id)
            # No implicit adoption of a stale journal across a scan/restart.
            current_attempts = self.journal.snapshot()
            if tuple(current_attempts) != tuple(req.evidence.journal_attempts):
                return CoordinationResult("BLOCKED", "JOURNAL_SNAPSHOT_CHANGED", signal_id)
            if any(a["signal_id"] == signal_id for a in current_attempts):
                return CoordinationResult("BLOCKED", "SIGNAL_ALREADY_RECORDED", signal_id)
            ev = req.evidence
            if (ev.report.blocked_reasons
                or ev.report.cursor != ev.funds.transaction_id
                or ev.report.entry_allowed is True):
                return CoordinationResult("BLOCKED", "ACCOUNT_RECONCILIATION_BLOCK", signal_id)
            ready = build_risk_readiness(
                ev.funds, ev.state, ev.journal_attempts,
                broker_trades=ev.broker_trades,
                broker_orders=ev.broker_orders,
                report=ev.report, now=now, anchor=ev.history_anchor,
            )
            if not ready.inputs_verified:
                return CoordinationResult("BLOCKED", "RISK_NOT_VERIFIED", signal_id)
            plan = self.planner.plan(
                req.signal, req.atr, ev.funds, req.rules, req.quote,
                ready.exposure, ready.losses,
                now=now, structure_level=req.structure_level,
            )
            if not plan.preview_only:
                return CoordinationResult("BLOCKED", "PLAN_NOT_PREVIEW", signal_id)
            return CoordinationResult("PREVIEW", "VERIFIED_SYNTHETIC_PLAN",
                                      signal_id, plan)
        except (JournalError, RiskEvidenceError, PlanRejected, ValueError,
                TypeError, KeyError, AttributeError) as exc:
            # Error text might contain sensitive broker details; only code.
            return CoordinationResult("BLOCKED", "INPUT_OR_JOURNAL_INVALID", signal_id)

    def simulate(
        self, initial: CoordinatorRequest, *,
        refresh: Callable[[], CoordinatorRequest],
        transport: SimulatedOrderTransport,
    ) -> CoordinationResult:
        """Exactly one synthetic OrderEngine submission after fresh verification.

        No arbitrarily injected write-capable transport is accepted. All
        exceptions *after* the submission boundary are indeterminate.
        """
        if (type(transport) is not SimulatedOrderTransport
            or not callable(refresh)
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False):
            return CoordinationResult("BLOCKED", "OFFLINE_GATE_REQUIRED")
        first = self.preview(initial)
        if first.status != "PREVIEW":
            return first
        try:
            latest = refresh()
        except Exception:
            return CoordinationResult("BLOCKED", "FRESH_RECONCILIATION_UNAVAILABLE",
                                      first.signal_id)
        updated = self.preview(latest)
        if updated.status != "PREVIEW":
            return updated
        if (updated.signal_id != first.signal_id or updated.plan != first.plan
            or latest.evidence.funds != initial.evidence.funds
            or latest.evidence.report.cursor != initial.evidence.report.cursor):
            return CoordinationResult("BLOCKED", "RISK_OR_SIGNAL_CHANGED",
                                      first.signal_id)
        if not transport.matches(latest.evidence, latest.quote):
            return CoordinationResult("BLOCKED", "SYNTHETIC_BROKER_STATE_CHANGED",
                                      first.signal_id)
        # Production OrderEngine retains its journal, quote, slippage, STOP/TP
        # and no-retry checks; its only transport here has no networking.
        engine = OrderEngine(
            latest.evidence.funds.account_id,
            transport=transport, journal=self.journal,
        )
        try:
            execution = engine.submit(
                updated.plan, latest.rules, latest.quote,
                signal_id=updated.signal_id, now=latest.now,
            )
        except (PreflightRejected, BrokerWriteDisabled, JournalError):
            # The same exception class may occur AFTER a simulated POST
            # (e.g. journal fsync fails while storing the fill). Never report
            # such a result as a safe preflight rejection.
            if transport.post_count:
                return CoordinationResult("UNKNOWN", "SYNTHETIC_OUTCOME_UNCERTAIN",
                                          first.signal_id, updated.plan)
            return CoordinationResult("BLOCKED", "ORDER_PREFLIGHT_OR_JOURNAL_BLOCK",
                                      first.signal_id, updated.plan)
        except Exception:
            # Conservative even if the exception happened before the call.
            return CoordinationResult("UNKNOWN", "SYNTHETIC_OUTCOME_UNCERTAIN",
                                      first.signal_id, updated.plan)
        if execution.outcome == Outcome.UNKNOWN:
            return CoordinationResult("UNKNOWN", "SYNTHETIC_OUTCOME_UNCERTAIN",
                                      first.signal_id, updated.plan, execution)
        if execution.outcome == Outcome.FILLED and execution.protection_verified:
            return CoordinationResult("SIMULATED", "SYNTHETIC_FILL_PROTECTED",
                                      first.signal_id, updated.plan, execution)
        return CoordinationResult(
            "BLOCKED", "SYNTHETIC_NOT_VERIFIED",
            first.signal_id, updated.plan, execution,
        )
