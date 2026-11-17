"""Read-only OANDA Practice reconciliation after manually closing Trade 6.

No broker mutations; account identity and transaction provenance are checked
against authoritative GET responses. Reports do not echo API credentials,
raw response bodies, or the full account ID. Zero-valued/absent financial
components remain explicitly distinct.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
import json
import os
import sys

import config
from broker.oanda_v20_contract import transaction_id, validate_sinceid_batch, OandaContractError
from broker.practice_transport import PracticeTransport, PRACTICE_API, _PRACTICE_WRITES_RELEASED, BrokerReply
from risk.trade_plan import number, PlanRejected
from utils.time_tools import parse_utc


ACCOUNT = "101-001-37674492-003"
TRADE = "6"
OPEN_TX = "6"
PARTIAL_TX = "10"
TRADE_CLIENT_ID = "algin-c12-20261117-once"
STARTING_BALANCE_USD = Decimal("10000")


class CloseAuditError(RuntimeError):
    """A Practice close or financial component lacks sufficient broker proof."""


def _positive_id(value):
    return transaction_id(value) and int(value) > 0


def _checked_decimal(value, label):
    try:
        return number(value, label)
    except (PlanRejected, ValueError, TypeError):
        raise CloseAuditError("Nonfinite or missing broker financial value") from None


@dataclass(frozen=True)
class PracticeCloseAuditReport:
    source: str
    environment: str
    account_suffix: str
    instrument: str
    trade_id: str
    trade_state: str
    initial_units: str
    remaining_units: str
    partial_units: str
    final_close_units: str
    partial_fill_transaction: str
    final_fill_transaction: str
    final_close_reason: str
    final_close_time: str
    flat_open_trades: bool
    flat_pending_orders: bool
    broker_trade_realized_pl_usd: str
    broker_trade_financing_usd: str | None
    partial_realized_pl_usd: str | None
    final_realized_pl_usd: str | None
    verified_trade_realized_sum: bool
    broker_account_balance_usd: str
    broker_account_nav_usd: str
    balance_delta_from_initial_reference_usd: str
    balance_delta_is_trade_attributed: bool
    commission_total_usd: str | None
    financing_total_from_fills_usd: str | None
    costs_verified_complete: bool
    transaction_cursor: str
    broker_writes_attempted: int = 0

    def public(self):
        return asdict(self)


class PracticeCloseAuditor:
    def __init__(self, *, transport=None):
        self.transport = transport if transport is not None else PracticeTransport()
        self.source = ("OANDA_PRACTICE_NETWORK" if transport is None
                       else "OFFLINE_INJECTED")

    def get(self, path, *, params=None):
        if (config.OANDA_URL != PRACTICE_API
            or config.ACCOUNT_ID != ACCOUNT
            or not config.API_KEY
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False):
            raise CloseAuditError("Practice-only account and write locks unverified")
        try:
            result = self.transport.read(path, params=params)
        except Exception:
            raise CloseAuditError("Broker GET failed; no outcome assumed") from None
        if (not isinstance(result, BrokerReply) or result.status_code != 200
            or not isinstance(result.body, dict)):
            raise CloseAuditError("Broker GET status/body not authoritative")
        return result.body

    def _one_tx(self, tid):
        document = self.get("/transactions/" + tid)
        tx = document.get("transaction")
        if (not isinstance(tx, dict) or tx.get("id") != tid
            or tx.get("accountID") != ACCOUNT
            or tx.get("type") != "ORDER_FILL"):
            raise CloseAuditError("Referenced ORDER_FILL not verified by broker")
        return tx

    def run(self):
        snapshot = self.get("")
        a = snapshot.get("account")
        cursor = snapshot.get("lastTransactionID")
        if (not isinstance(a, dict) or a.get("id") != ACCOUNT
            or a.get("currency") != "USD"
            or not _positive_id(cursor)
            or a.get("openTradeCount") != 0
            or a.get("pendingOrderCount") != 0
            or a.get("trades") != []
            or a.get("orders") != []):
            raise CloseAuditError("Account not demonstrably flat in Practice")
        balance = _checked_decimal(a.get("balance"), "balance")
        nav = _checked_decimal(a.get("NAV"), "NAV")
        if balance <= 0 or nav <= 0:
            raise CloseAuditError("Account balance or NAV invalid")

        detail = self.get("/trades/" + TRADE)
        trade = detail.get("trade")
        if (not isinstance(trade, dict)
            or trade.get("id") != TRADE
            or trade.get("instrument") != "EUR_USD"
            or trade.get("state") != "CLOSED"
            or (trade.get("clientExtensions") or {}).get("id") != TRADE_CLIENT_ID
            or _checked_decimal(trade.get("initialUnits"), "initial units") != 20
            or _checked_decimal(trade.get("currentUnits"), "remaining units") != 0
            or not isinstance(trade.get("closingTransactionIDs"), list)
            or PARTIAL_TX not in trade["closingTransactionIDs"]):
            raise CloseAuditError("Owned trade not confirmed fully CLOSED")
        try:
            close_time = parse_utc(trade["closeTime"])
        except (KeyError, ValueError, TypeError):
            raise CloseAuditError("Trade terminal close timestamp missing") from None
        closing_ids = trade["closingTransactionIDs"]
        if (len(closing_ids) != 2
            or len(set(closing_ids)) != 2
            or not all(_positive_id(x) for x in closing_ids)
            or int(PARTIAL_TX) >= int(max(closing_ids, key=int))):
            raise CloseAuditError("Unexpected trade closing transaction lineage")
        final_id = next(x for x in closing_ids if x != PARTIAL_TX)

        since = self.get("/transactions/sinceid", params={"id": PARTIAL_TX})
        try:
            new = validate_sinceid_batch(
                since, account=ACCOUNT, previous_cursor=PARTIAL_TX,
                target_cursor=cursor,
            )
        except OandaContractError:
            raise CloseAuditError("Broker manual-close cursor history incomplete") from None
        matching = [
            item for item in new
            if item.get("type") == "ORDER_FILL"
            and item.get("id") == final_id
            and any(
                isinstance(c, dict) and c.get("tradeID") == TRADE
                for c in item.get("tradesClosed", [])
            )
        ]
        if len(matching) != 1:
            raise CloseAuditError("No broker-backed manual full-close fill")

        opener = self._one_tx(OPEN_TX)
        partial = self._one_tx(PARTIAL_TX)
        final = self._one_tx(final_id)
        opened = opener.get("tradeOpened")
        reduced = partial.get("tradeReduced")
        closes = final.get("tradesClosed")
        if (not isinstance(opened, dict) or opened.get("tradeID") != TRADE
            or _checked_decimal(opened.get("units"), "opened units") != 20
            or not isinstance(reduced, dict) or reduced.get("tradeID") != TRADE
            or _checked_decimal(reduced.get("units"), "reduced units") != -10
            or not isinstance(closes, list) or len(closes) != 1
            or not isinstance(closes[0], dict)
            or closes[0].get("tradeID") != TRADE
            or _checked_decimal(closes[0].get("units"), "closed units") != -10):
            raise CloseAuditError("Open, partial and terminal units do not reconcile")
        if final.get("id") != matching[0].get("id"):
            raise CloseAuditError("Terminal full-close transaction changed")
        try:
            broker_pl = _checked_decimal(trade["realizedPL"], "trade realizedPL")
            p_pl = _checked_decimal(reduced["realizedPL"], "partial realizedPL")
            f_pl = _checked_decimal(closes[0]["realizedPL"], "closed realizedPL")
        except (KeyError, ValueError, TypeError):
            raise CloseAuditError("Trade P/L needs explicit broker trade components") from None
        pl_equal = broker_pl == p_pl + f_pl
        if not pl_equal:
            raise CloseAuditError("Trade realizedPL differs from transaction sum")

        funding = (
            str(_checked_decimal(trade["financing"], "trade financing"))
            if "financing" in trade else None
        )
        # Missing financial fields are UNKNOWN, NOT zero. Account balance
        # delta is a descriptive reference and may include other account
        # activity; don't mislabel it as attributable per-trade net.
        commissions = [
            _checked_decimal(tx["commission"], "commission")
            for tx in (opener, partial, final) if "commission" in tx
        ]
        comm_total = (
            str(sum(commissions, Decimal("0")))
            if len(commissions) == 3 else None
        )
        funding_fills = [
            _checked_decimal(tx["financing"], "fill financing")
            for tx in (opener, partial, final) if "financing" in tx
        ]
        funding_total = (
            str(sum(funding_fills, Decimal("0")))
            if len(funding_fills) == 3 else None
        )
        # Fees, daily financing and other account activity require
        # complete accounting before declaring a certified NET trade P/L.
        reported_complete = (
            comm_total is not None and funding_total is not None
            and all("guaranteedExecutionFee" in tx
                    for tx in (opener, partial, final))
        )
        return PracticeCloseAuditReport(
            source=self.source, environment="PRACTICE",
            account_suffix=ACCOUNT[-3:], instrument="EUR_USD",
            trade_id=TRADE, trade_state="CLOSED",
            initial_units="20", remaining_units="0",
            partial_units="10", final_close_units="10",
            partial_fill_transaction=PARTIAL_TX,
            final_fill_transaction=final_id,
            final_close_reason=str(final.get("reason", "NOT_REPORTED")),
            final_close_time=close_time.isoformat(),
            flat_open_trades=True, flat_pending_orders=True,
            broker_trade_realized_pl_usd=str(broker_pl),
            broker_trade_financing_usd=funding,
            partial_realized_pl_usd=str(p_pl),
            final_realized_pl_usd=str(f_pl),
            verified_trade_realized_sum=True,
            broker_account_balance_usd=str(balance),
            broker_account_nav_usd=str(nav),
            balance_delta_from_initial_reference_usd=str(
                balance - STARTING_BALANCE_USD
            ),
            balance_delta_is_trade_attributed=False,
            commission_total_usd=comm_total,
            financing_total_from_fills_usd=funding_total,
            costs_verified_complete=reported_complete,
            transaction_cursor=cursor,
        )


def main():
    try:
        report = PracticeCloseAuditor().run()
    except CloseAuditError as exc:
        print(json.dumps({
            "status": "C12_MANUAL_CLOSE_UNVERIFIED",
            "reason": str(exc), "broker_writes": 0,
        }, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({
            "status": "C12_MANUAL_CLOSE_UNVERIFIED",
            "reason": "UNEXPECTED_ERROR", "broker_writes": 0,
        }, sort_keys=True))
        return 3
    print(json.dumps({
        "status": "C12_MANUAL_CLOSE_VERIFIED",
        "report": report.public(), "broker_writes": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
