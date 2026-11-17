"""Faza C / Category 12: strict offline manual-close verification fixtures."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import io
import json
import unittest

import config
from broker.practice_close_audit import (
    PracticeCloseAuditor, CloseAuditError, ACCOUNT, TRADE_CLIENT_ID, main,
)
from broker.practice_transport import BrokerReply, PRACTICE_API


def fill(tid, *, realized="0", financing="0", commission="0"):
    item = {
        "id": tid, "accountID": ACCOUNT, "type": "ORDER_FILL",
        "pl": realized, "financing": financing,
        "commission": commission,
        "guaranteedExecutionFee": "0",
    }
    return item


class FakePractice:
    def __init__(self):
        self.calls = []
        self.tx6 = fill("6")
        self.tx6["tradeOpened"] = {
            "tradeID": "6", "units": "20", "price": "1.10000",
        }
        self.tx10 = fill("10", realized="-0.0016")
        self.tx10["tradeReduced"] = {
            "tradeID": "6", "units": "-10",
            "realizedPL": "-0.0016", "financing": "0",
        }
        self.tx14 = fill("14", realized="0.0090")
        self.tx14["reason"] = "MARKET_ORDER_TRADE_CLOSE"
        self.tx14["tradesClosed"] = [{
            "tradeID": "6", "units": "-10",
            "realizedPL": "0.0090", "financing": "0",
        }]
        self.account = {
            "id": ACCOUNT, "currency": "USD",
            "balance": "10000.0074", "NAV": "10000.0074",
            "openTradeCount": 0, "pendingOrderCount": 0,
            "trades": [], "orders": [],
        }
        self.trade = {
            "id": "6", "instrument": "EUR_USD", "state": "CLOSED",
            "initialUnits": "20", "currentUnits": "0",
            "realizedPL": "0.0074", "financing": "0",
            "closeTime": "2026-11-17T14:30:00Z",
            "closingTransactionIDs": ["10", "14"],
            "clientExtensions": {"id": TRADE_CLIENT_ID, "tag": "algin"},
        }
        self.range = [
            {"id": "11", "accountID": ACCOUNT, "type": "MARKET_ORDER"},
            self.tx14,
        ]
        self.cursor = "14"
        self.fail = None

    def read(self, path, *, params=None):
        self.calls.append((path, params))
        if path == self.fail:
            return BrokerReply(503, {"errorMessage": "TOKEN SHOULD BE HIDDEN"})
        if path == "":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor, "account": self.account,
            })
        if path == "/trades/6":
            return BrokerReply(200, {
                "lastTransactionID": self.cursor, "trade": self.trade,
            })
        if path == "/transactions/sinceid":
            if params != {"id": "10"}:
                raise AssertionError("Wrong starting cursor")
            return BrokerReply(200, {
                "lastTransactionID": self.cursor,
                "transactions": self.range,
            })
        if path == "/transactions/6":
            return BrokerReply(200, {"transaction": self.tx6})
        if path == "/transactions/10":
            return BrokerReply(200, {"transaction": self.tx10})
        if path == "/transactions/14":
            return BrokerReply(200, {"transaction": self.tx14})
        raise AssertionError("Unexpected GET " + path)


class ManualCloseAuditTests(unittest.TestCase):
    def setUp(self):
        self.mock = FakePractice()
        for setting in (
            patch.object(config, "OANDA_URL", PRACTICE_API),
            patch.object(config, "ACCOUNT_ID", ACCOUNT),
            patch.object(config, "API_KEY", "test-only-secret"),
            patch.object(config, "ORDER_WRITES_ENABLED", False),
        ):
            setting.start()
            self.addCleanup(setting.stop)

    def verify(self):
        return PracticeCloseAuditor(transport=self.mock).run()

    def test_two_signed_closures_verify_trade_flat_and_trade_pl_sum(self):
        result = self.verify()
        self.assertEqual(result.source, "OFFLINE_INJECTED")
        self.assertEqual(result.trade_state, "CLOSED")
        self.assertEqual(result.remaining_units, "0")
        self.assertTrue(result.flat_pending_orders)
        self.assertEqual(result.final_fill_transaction, "14")
        self.assertEqual(result.broker_trade_realized_pl_usd, "0.0074")
        self.assertEqual(result.balance_delta_from_initial_reference_usd,
                         "0.0074")
        self.assertFalse(result.balance_delta_is_trade_attributed)
        self.assertTrue(result.verified_trade_realized_sum)
        self.assertEqual(result.broker_writes_attempted, 0)
        self.assertTrue(all(
            p in ("", "/trades/6", "/transactions/sinceid",
                  "/transactions/6", "/transactions/10",
                  "/transactions/14") for p, _ in self.mock.calls
        ))

    def test_any_open_trades_or_pending_orders_blocks_flat_claim(self):
        for name, change in (
            ("openTradeCount", 1),
            ("pendingOrderCount", 2),
            ("trades", [{"id": "6"}]),
            ("orders", [{"id": "9"}]),
        ):
            with self.subTest(name=name):
                tmp=deepcopy(self.mock.account)
                self.mock.account[name]=change
                with self.assertRaises(CloseAuditError):
                    self.verify()
                self.mock.account=tmp

    def test_trade_must_be_closed_not_close_when_tradeable(self):
        for state in ("OPEN", "CLOSE_WHEN_TRADEABLE", "FILLED"):
            with self.subTest(state=state):
                self.mock.trade["state"] = state
                with self.assertRaises(CloseAuditError):
                    self.verify()

    def test_wrong_ownership_or_currency_blocks(self):
        self.mock.trade["clientExtensions"]["id"]="foreign"
        with self.assertRaises(CloseAuditError):
            self.verify()
        self.mock.trade["clientExtensions"]["id"]=TRADE_CLIENT_ID
        self.mock.account["currency"]="EUR"
        with self.assertRaises(CloseAuditError):
            self.verify()

    def test_broker_close_must_have_matching_signed_units(self):
        self.mock.tx14["tradesClosed"][0]["units"] = "10"
        with self.assertRaises(CloseAuditError):
            self.verify()

    def test_trade_pl_must_reconcile_with_both_transaction_effects(self):
        self.mock.trade["realizedPL"] = "0.0090"
        with self.assertRaises(CloseAuditError):
            self.verify()

    def test_missing_or_nonfinite_money_never_becomes_zero(self):
        for value in ("NaN", "Infinity", None):
            with self.subTest(value=value):
                self.mock.trade["realizedPL"] = value
                with self.assertRaises(CloseAuditError):
                    self.verify()

    def test_cursor_gap_or_missing_terminal_fill_blocks(self):
        self.mock.range=[{"id": "11", "accountID": ACCOUNT,
                         "type": "MARKET_ORDER"}]
        with self.assertRaises(CloseAuditError):
            self.verify()

    def test_stale_cursor_does_not_approve_close(self):
        self.mock.cursor="10"
        with self.assertRaises(CloseAuditError):
            self.verify()

    def test_broker_error_never_leaks_credentials(self):
        self.mock.fail="/trades/6"
        with self.assertRaises(CloseAuditError) as error:
            self.verify()
        self.assertNotIn("TOKEN", str(error.exception))
        self.assertNotIn("test-only-secret", str(error.exception))

    def test_live_endpoint_gates_before_any_GET(self):
        with patch.object(config, "OANDA_URL", "https://api-fxtrade.oanda.com/v3"):
            with self.assertRaises(CloseAuditError):
                self.verify()
        self.assertEqual(self.mock.calls, [])

    def test_write_gate_must_remain_off(self):
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            with self.assertRaises(CloseAuditError):
                self.verify()
        self.assertEqual(self.mock.calls, [])

    def test_absent_commission_does_not_claim_complete_costs(self):
        del self.mock.tx10["commission"]
        result=self.verify()
        self.assertFalse(result.costs_verified_complete)
        self.assertIsNone(result.commission_total_usd)

    def test_coverage_of_external_non_trade_transaction_cannot_be_ignored(self):
        self.mock.range.insert(1,{
            "id": "12", "accountID": ACCOUNT, "type": "TRANSFER_FUNDS",
            "amount": "500",
        })
        result = self.verify()
        self.assertFalse(result.balance_delta_is_trade_attributed)

    def test_summary_hides_account_id_and_never_echoes_token(self):
        document = json.dumps(self.verify().public(), sort_keys=True)
        self.assertNotIn(ACCOUNT, document)
        self.assertNotIn("test-only-secret", document)
        self.assertIn('"account_suffix": "003"', document)


if __name__ == "__main__":
    unittest.main()
