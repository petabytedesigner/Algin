"""OANDA Practice account verification, strictly GET-only.

This module does not read .env files or expose credentials. Production probes
take credentials ONLY from environment variables resolved by config.py.
No POST/PUT/PATCH/DELETE path, journal mutation, order release or retry here.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
import json
import os
import sys

import config
from broker.account_instruments import (
    InstrumentCatalogError, parse_account_instruments,
)
from broker.execution_validation import PreflightRejected, parse_market_quote
from broker.oanda_v20_contract import (
    OandaContractError, account_id, transaction_id, validate_sinceid_batch,
)
from broker.practice_transport import (
    BrokerCommunicationError, BrokerReply, PracticeTransport, PRACTICE_API,
    _PRACTICE_WRITES_RELEASED,
)
from risk.trade_plan import number
from utils.time_tools import utc_now


class PracticeProbeError(RuntimeError):
    """The Practice account identity or read evidence is not verified."""


@dataclass(frozen=True)
class PracticeProbeReport:
    environment: str
    source: str
    account_suffix: str
    currency: str
    account_read_verified: bool
    summary_read_verified: bool
    instrument_catalog_verified: bool
    instruments_count: int
    eur_usd_quote_status: str
    transaction_history_status: str
    account_balance_reference_10000: bool
    account_nav_positive: bool
    zero_open_trades: bool
    zero_pending_orders: bool
    mt4_associated: bool
    broker_writes_enabled: bool = False

    def public(self):
        """Safe CI summary: no token, account ID, balances or trade IDs."""
        return asdict(self)


class PracticeReadOnlyProbe:
    def __init__(self, *, expected_account_id, transport=None):
        if not account_id(expected_account_id):
            raise PracticeProbeError("Expected Practice account ID invalid")
        self.expected_account_id = expected_account_id
        # An injected transport is never labeled a verified external source.
        self.source = "OANDA_PRACTICE_NETWORK" if transport is None else "OFFLINE_INJECTED"
        self.transport = PracticeTransport() if transport is None else transport

    def _get(self, path, params=None):
        try:
            reply = self.transport.read(path, params=params)
        except Exception as exc:
            # Never propagate exception content or request URL/token to CI.
            raise PracticeProbeError("Practice GET communication failed") from None
        if (not isinstance(reply, BrokerReply)
            or reply.status_code != 200
            or not isinstance(reply.body, dict)):
            raise PracticeProbeError("Practice GET HTTP/JSON not authoritative")
        return reply.body

    def run(self):
        if (config.OANDA_URL != PRACTICE_API
            or config.ORDER_WRITES_ENABLED is not False
            or _PRACTICE_WRITES_RELEASED is not False
            or config.ACCOUNT_ID != self.expected_account_id
            or not config.API_KEY):
            raise PracticeProbeError("Practice endpoint/credentials/write gates unverified")

        # Full account, rather than summary, is needed for exact inventory.
        a = self._get("")
        info = a.get("account")
        if (not isinstance(info, dict)
            or info.get("id") != self.expected_account_id
            or info.get("currency") != "USD"
            or not transaction_id(a.get("lastTransactionID"))):
            raise PracticeProbeError("Expected USD Practice account identity mismatch")
        trades, orders = info.get("trades"), info.get("orders")
        if (not isinstance(trades, list) or not isinstance(orders, list)
            or not all(isinstance(x, dict) for x in trades + orders)
            or type(info.get("openTradeCount")) is not int
            or info["openTradeCount"] != len(trades)
            or type(info.get("pendingOrderCount")) is not int
            or info["pendingOrderCount"] != len(orders)):
            raise PracticeProbeError("Broker trade/order inventory incomplete")
        try:
            balance = number(info["balance"], "balance", positive=True)
            nav = number(info["NAV"], "NAV", positive=True)
            margin = number(info["marginAvailable"], "free margin")
            if margin < 0:
                raise ValueError("Negative margin")
        except (KeyError, ValueError, TypeError) as exc:
            raise PracticeProbeError("Broker balance/NAV/margin unverified") from None

        summary = self._get("/summary")
        brief = summary.get("account")
        if (not isinstance(brief, dict)
            or brief.get("id") != self.expected_account_id
            or brief.get("currency") != "USD"
            or not transaction_id(summary.get("lastTransactionID"))):
            raise PracticeProbeError("Account summary disagrees with full account")

        all_rules = self._get("/instruments")
        try:
            rules = parse_account_instruments(all_rules)
        except (InstrumentCatalogError, ValueError, TypeError) as exc:
            raise PracticeProbeError("Account-scoped instrument catalog unverified") from None

        quote_status = "INSTRUMENT_NOT_AVAILABLE"
        if "EUR_USD" in rules:
            try:
                raw_quote = self._get("/pricing", {"instruments": "EUR_USD"})
                parse_market_quote(
                    raw_quote, "EUR_USD", rules["EUR_USD"],
                    now=utc_now(),
                    max_age_seconds=config.MAX_QUOTE_AGE_SECONDS,
                    max_spread_pips=Decimal(str(config.MAX_SPREAD_PIPS)),
                )
                quote_status = "QUOTE_VERIFIED"
            except (PracticeProbeError, PreflightRejected, ValueError):
                quote_status = "QUOTE_UNAVAILABLE_OR_UNVERIFIED"

        # Check transaction cursor continuity without importing the remote
        # inventory into persistent state. Concurrent changes block writes.
        tx_snapshot = self._get(
            "/transactions/sinceid",
            {"id": a["lastTransactionID"]},
        )
        latest = tx_snapshot.get("lastTransactionID")
        earlier = a["lastTransactionID"]
        if not transaction_id(latest) or int(latest) < int(earlier):
            raise PracticeProbeError("Broker transaction cursor regressed")
        if latest == earlier:
            if tx_snapshot.get("transactions") != []:
                raise PracticeProbeError("Unexpected transactions at unchanged cursor")
            tx_status = "UNCHANGED_CURSOR"
        else:
            try:
                validate_sinceid_batch(
                    tx_snapshot, account=self.expected_account_id,
                    previous_cursor=earlier, target_cursor=latest,
                )
            except OandaContractError:
                raise PracticeProbeError("Concurrent transaction range unverified") from None
            tx_status = "CONCURRENT_CHANGES_NO_WRITE"

        # Even a fully verified GET does NOT grant execution authorization.
        return PracticeProbeReport(
            environment="PRACTICE", source=self.source,
            account_suffix=self.expected_account_id[-3:],
            currency="USD",
            account_read_verified=True, summary_read_verified=True,
            instrument_catalog_verified=True,
            instruments_count=len(rules),
            eur_usd_quote_status=quote_status,
            transaction_history_status=tx_status,
            account_balance_reference_10000=balance == Decimal("10000"),
            account_nav_positive=nav > 0,
            zero_open_trades=len(trades) == 0,
            zero_pending_orders=len(orders) == 0,
            mt4_associated=info.get("mt4AccountID") is not None,
        )


def main():
    """Safe runner for a vetted Actions job or an explicit local CLI session."""
    # The target is the account supplied by the user, NOT an API response.
    expected = os.environ.get("OANDA_PRACTICE_EXPECTED_ACCOUNT_ID", "")
    try:
        report = PracticeReadOnlyProbe(expected_account_id=expected).run()
    except PracticeProbeError as exc:
        # These are exclusively our own static diagnostics.
        print(json.dumps({
            "status": "PRACTICE_READ_ONLY_FAILED",
            "reason": str(exc),
            "broker_mutations": 0,
        }, sort_keys=True))
        return 2
    except Exception:
        # Unexpected errors (including a broker library regression) must
        # NEVER print raw HTTP exceptions, response bodies, URLs or tokens.
        print(json.dumps({
            "status": "PRACTICE_READ_ONLY_FAILED",
            "reason": "UNEXPECTED_PROBE_ERROR",
            "broker_mutations": 0,
        }, sort_keys=True))
        return 3
    print(json.dumps({
        "status": "PRACTICE_READ_ONLY_VERIFIED",
        "report": report.public(),
        "broker_mutations": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
