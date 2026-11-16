"""Read-only, provenance-checked risk inputs for a reconciled OANDA account.

This module never authorizes a broker write. An account snapshot, lifecycle
state, journal and ledger must agree on account/cursor/trade identities before
ExposureBook can be marked verified. LossState is NOT verified unless a
separately established daily/historical risk anchor is supplied and all
relevant financial history is complete. A fresh account NAV is not a
historical day-start NAV or a historical high-water mark.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from broker.financial_ledger import FinancialEvidenceError, summary, trade_summary
from risk.trade_lifecycle import TradeRecord
from risk.trade_plan import (
    AccountFunds, ExposureBook, ExposureReservation, LossState,
    PlanRejected, number,
)
from utils.time_tools import parse_utc, require_utc


class RiskEvidenceError(ValueError):
    """Risk state or its provenance cannot be verified."""


@dataclass(frozen=True)
class RiskHistoryAnchor:
    """Externally verified anchor; must never be inferred from today's GET.

    A future monitoring/bootstrap workflow must persist/attest these fields.
    This object alone is NOT a broker authorization or a proof of historic NAV.
    """
    account_id: str
    cursor: str
    day: str
    day_start_cursor: str
    day_start_nav: Decimal
    peak_nav: Decimal
    starting_loss_streak: int
    history_complete: bool

    def __post_init__(self):
        if not isinstance(self.account_id, str) or not self.account_id:
            raise RiskEvidenceError("Risk anchor account missing")
        if not all(
            isinstance(x, str) and x.isascii() and x.isdecimal()
            for x in (self.cursor, self.day_start_cursor)
        ):
            raise RiskEvidenceError("Risk anchor cursor invalid")
        if not isinstance(self.day, str):
            raise RiskEvidenceError("Risk anchor day invalid")
        try:
            from datetime import date
            if date.fromisoformat(self.day).isoformat() != self.day:
                raise ValueError
        except ValueError as exc:
            raise RiskEvidenceError("Invalid anchor UTC day") from exc
        number(self.day_start_nav, "day start NAV", positive=True)
        number(self.peak_nav, "verified peak NAV", positive=True)
        if (type(self.starting_loss_streak) is not int
            or self.starting_loss_streak < 0
            or type(self.history_complete) is not bool):
            raise RiskEvidenceError("Unverified loss-streak anchor")


@dataclass(frozen=True)
class RiskReadiness:
    exposure: ExposureBook
    losses: LossState
    inputs_verified: bool
    entry_allowed: bool
    blocked_reasons: tuple[str, ...]


def _attempt_reservation(entry, *, status):
    try:
        order = entry["request"]["order"]
        instrument = order["instrument"]
        units = number(order["units"], "reserved order units", zero_ok=False)
        pct = number(entry["risk_pct"], "reserved risk", positive=True)
        grade = entry["grade"]
        margin = number(entry["margin_home"], "reserved margin", positive=True)
        if not isinstance(instrument, str) or instrument.count("_") != 1:
            raise RiskEvidenceError("Invalid journal instrument")
        return ExposureReservation(
            reservation_id=entry["attempt_id"], instrument=instrument,
            direction="LONG" if units > 0 else "SHORT",
            grade=grade, risk_pct=pct, status=status,
            reserved_margin_home=margin,
        )
    except (KeyError, TypeError, PlanRejected) as exc:
        raise RiskEvidenceError("Invalid journal risk reservation") from exc


def _snapshot_exposure(account, state, attempts, broker_trades, broker_orders):
    reasons = []
    reservations = []
    if not isinstance(account, AccountFunds) or not isinstance(state, dict):
        raise RiskEvidenceError("Missing account or lifecycle snapshot")
    if (state.get("account_id") != account.account_id
        or state.get("environment") != "practice"
        or state.get("cursor") != account.transaction_id):
        raise RiskEvidenceError("Account/lifecycle cursor mismatch")
    if not isinstance(attempts, (tuple, list)):
        raise RiskEvidenceError("Journal snapshot missing")
    if not isinstance(broker_trades, (tuple, list)) or not isinstance(broker_orders, (tuple, list)):
        raise RiskEvidenceError("Broker inventory missing")
    if (account.broker_open_count != len(broker_trades)
        or account.broker_pending_count != len(broker_orders)):
        raise RiskEvidenceError("Broker inventory count disagrees with account")
    if not isinstance(state.get("trades"), dict) or not isinstance(
        state.get("closed_results"), dict
    ):
        raise RiskEvidenceError("Malformed lifecycle trade ledger")

    active = {}
    for raw in broker_trades:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise RiskEvidenceError("Invalid broker open trade")
        if raw["id"] in active:
            raise RiskEvidenceError("Duplicate broker open trade")
        active[raw["id"]] = raw
    indexed = {}
    for entry in attempts:
        if not isinstance(entry, dict):
            raise RiskEvidenceError("Malformed journal attempt")
        aid = entry.get("attempt_id")
        if not isinstance(aid, str) or not aid or aid in indexed:
            raise RiskEvidenceError("Duplicate/unidentified journal attempt")
        indexed[aid] = entry

    reserved_trade_ids = set()
    for entry in attempts:
        status = entry.get("status")
        if status in ("REJECTED", "CANCELLED"):
            continue
        if status in ("PREPARED", "IN_FLIGHT", "UNKNOWN"):
            reservations.append(_attempt_reservation(entry, status=(
                "PENDING" if status == "PREPARED" else "OUTCOME_UNKNOWN"
            )))
            reasons.append("UNRESOLVED_ORDER_INTENT")
            continue
        if status != "FILLED":
            raise RiskEvidenceError("Unknown journal state")
        trade_id = entry.get("trade_id")
        if trade_id in active:
            raw = active[trade_id]
            record = TradeRecord.from_dict(state["trades"].get(trade_id))
            ext = raw.get("clientExtensions")
            if (record.closed or record.trade_id != trade_id
                or record.attempt_id != entry["attempt_id"]
                or entry.get("protection_verified") is not True
                or not isinstance(ext, dict)
                or ext.get("id") != record.attempt_id
                or ext.get("tag") != "algin"
                or raw.get("instrument") != record.instrument
                or number(entry["risk_pct"], "journal risk", positive=True)
                != record.risk_pct
                or entry.get("grade") != record.grade
                or entry.get("retired") is True):
                raise RiskEvidenceError("Open trade/journal risk mismatch")
            if trade_id in reserved_trade_ids:
                raise RiskEvidenceError("Duplicate open trade reservation")
            reserved_trade_ids.add(trade_id)
            reservations.append(ExposureReservation(
                reservation_id=entry["attempt_id"],
                instrument=record.instrument, direction=record.direction,
                grade=record.grade, risk_pct=record.risk_pct,
                status="OPEN",
            ))
        else:
            saved = state["trades"].get(trade_id)
            outcome = state["closed_results"].get(trade_id)
            if saved is None or outcome is None:
                raise RiskEvidenceError("FILLED trade lacks confirmed closure")
            rec = TradeRecord.from_dict(saved)
            if (not rec.closed or not rec.result_counted
                or rec.attempt_id != entry["attempt_id"]
                or outcome["transaction_id"] not in rec.close_transaction_ids
                or outcome["transaction_id"] not in state["processed_transactions"]
                or entry.get("retired") is not True
                or entry.get("closure_transaction_id") != outcome["transaction_id"]):
                raise RiskEvidenceError("Closure not durably retired")
    for trade_id, raw in active.items():
        if trade_id not in reserved_trade_ids:
            reasons.append("FOREIGN_OR_UNRESERVED_BROKER_TRADE")
    if broker_orders:
        # Existing reconciliation cannot yet attribute all pending orders
        # to unique, proved reservations; no optimistic risk allocation.
        reasons.append("UNRECONCILED_PENDING_BROKER_ORDER")
    for trade_id, saved in state["trades"].items():
        record = TradeRecord.from_dict(saved)
        if not record.closed and trade_id not in active:
            reasons.append("MISSING_OPEN_TRADE")
        if record.closed and trade_id in active:
            reasons.append("CLOSED_TRADE_STILL_OPEN")
        if not record.closed and (record.be_state in ("INTENT", "UNKNOWN") or any(
            x["state"] in ("INTENT", "UNKNOWN")
            for x in record.partial_phases.values()
        )):
            reasons.append("UNRESOLVED_MANAGEMENT_OPERATION")
    verified = not reasons
    return ExposureBook(
        account.account_id, tuple(reservations),
        verified=verified, uncertain_outcomes_accounted=verified
    ), reasons


def _losses_from_anchor(account, state, anchor, *, now):
    """Never infer streak, peak or day-start NAV from a single account GET."""
    fallback = LossState(peak_nav=account.nav, consecutive_losses=0, verified=False)
    if anchor is None:
        return fallback, ("RISK_HISTORY_ANCHOR_MISSING",)
    if not isinstance(anchor, RiskHistoryAnchor):
        return fallback, ("RISK_HISTORY_ANCHOR_INVALID",)
    if (not anchor.history_complete or anchor.account_id != account.account_id
        or anchor.cursor != state["cursor"]
        or anchor.day != now.date().isoformat()):
        return fallback, ("RISK_HISTORY_NOT_CURRENT_OR_COMPLETE",)
    if "financial_ledger" not in state:
        return fallback, ("NET_FINANCIAL_LEDGER_MISSING",)
    ledger = state["financial_ledger"]
    if (int(anchor.day_start_cursor) < int(ledger["baseline_cursor"])
        or int(anchor.day_start_cursor) > int(state["cursor"])
        or number(anchor.peak_nav, "peak NAV", positive=True) < account.nav):
        return fallback, ("DAY_OR_PEAK_ANCHOR_UNVERIFIABLE",)
    result = summary(ledger)
    if not result["complete_since_cursor"]:
        return fallback, ("FINANCIAL_HISTORY_INCOMPLETE",)
    loss_home = Decimal("0")
    for tid, event in ledger["events"].items():
        if int(tid) > int(anchor.day_start_cursor):
            value = event["account_net"]
            if value is not None:
                pl = number(value, "verified financial transaction")
                if pl < 0:
                    loss_home -= pl
    closes = []
    for trade_id, closed in state["closed_results"].items():
        # The starting streak is attested at the day-start cursor.
        # Replaying earlier closes would count an old loss twice.
        if int(closed["transaction_id"]) <= int(anchor.day_start_cursor):
            continue
        detail = trade_summary(ledger, trade_id)
        if not detail["complete"] or detail["net_pl_home"] is None:
            return fallback, ("CLOSED_TRADE_NET_NOT_VERIFIED",)
        closes.append((
            parse_utc(closed["closed_at"]),
            int(closed["transaction_id"]),
            trade_id,
            number(detail["net_pl_home"], "verified trade net")
        ))
    closes.sort()
    streak = anchor.starting_loss_streak
    cooldown = {
        key: parse_utc(value) for key, value in state.get("cooldowns", {}).items()
    }
    for closed_at, _, trade_id, net in closes:
        if net < 0:
            streak += 1
            symbol = state["closed_results"][trade_id]["instrument"]
            until = closed_at + timedelta(minutes=30)
            previous = cooldown.get(symbol)
            if previous is None or until > previous:
                cooldown[symbol] = until
        else:
            streak = 0
    return LossState(
        peak_nav=anchor.peak_nav,
        consecutive_losses=streak, verified=True,
        cooldown_until_by_instrument=cooldown,
        day_start_nav=anchor.day_start_nav,
        daily_realized_loss_home=loss_home,
    ), ()


def build_risk_readiness(
    account: AccountFunds, state: dict, attempts, *,
    broker_trades, broker_orders, report, now: datetime, anchor=None
) -> RiskReadiness:
    """Read-only verified risk inputs, never permission to transmit an order.

    report is the completed AccountReconciler.run() result for this account.
    Any prior reconciliation blocker or cursor mismatch disables readiness.
    The production ReconcileReport.entry_allowed remains independently False.
    """
    now = require_utc(now)
    reasons = []
    if report is None or not hasattr(report, "blocked_reasons"):
        raise RiskEvidenceError("Completed reconciliation report required")
    if report.blocked_reasons or report.cursor != account.transaction_id:
        reasons.append("BROKER_RECONCILIATION_NOT_COMPLETE")
    if set(report.owned_open_trades) != {
        trade.get("id") for trade in broker_trades if isinstance(trade, dict)
    }:
        reasons.append("OWNED_BROKER_TRADE_REPORT_MISMATCH")
    exposure, exposure_reasons = _snapshot_exposure(
        account, state, attempts, broker_trades, broker_orders
    )
    reasons.extend(exposure_reasons)
    losses, loss_reasons = _losses_from_anchor(
        account, state, anchor, now=now
    )
    reasons.extend(loss_reasons)
    inputs_verified = not reasons and exposure.verified and losses.verified
    return RiskReadiness(
        exposure=exposure, losses=losses,
        inputs_verified=inputs_verified,
        # Pipeline execution permission belongs to later categories.
        entry_allowed=False,
        blocked_reasons=tuple(sorted(set(reasons))),
    )
