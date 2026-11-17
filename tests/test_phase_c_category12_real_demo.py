"""Category 12 controlled demo broker integration: synthetic offline tests."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

import config
from broker.practice_transport import BrokerReply, PRACTICE_API
from broker.practice_demo_adapter import (
    DemoAdapter, DemoBlocked, ACCOUNT, ATTEMPT
)
from broker.practice_one_shot_runner import preflight, exercise, ARM
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.trade = None
        self.order_exists = False
        self.fail_write = False
        self.bad_partial = False
        self.bad_owner = False
        self.bad_account = False
        self.prices = {
            "prices": [{
                "instrument": "EUR_USD", "tradeable": True,
                "time": iso_utc(NOW), "bids": [{"price": "1.10000"}],
                "asks": [{"price": "1.10020"}],
            }]
        }
        self.transactions = {}

    def read(self, path, *, params=None):
        self.calls.append(("GET", path))
        if path == "/orders/@" + ATTEMPT:
            if self.order_exists:
                return BrokerReply(200, {"order": {"id": "100"}})
            return BrokerReply(404, {
                "errorCode": "NO_SUCH_ORDER",
                "errorMessage": "The order ID specified does not exist",
            })
        if path == "":
            trades = [self.trade] if self.trade is not None else []
            orders = []
            return BrokerReply(200, {
                "lastTransactionID": "100",
                "account": {
                    "id": "not-the-account" if self.bad_account else ACCOUNT,
                    "currency": "USD", "NAV": "10000",
                    "balance": "10000", "marginAvailable": "10000",
                    "openTradeCount": len(trades), "pendingOrderCount": 0,
                    "trades": trades, "orders": orders,
                },
            })
        if path == "/instruments":
            return BrokerReply(200, {
                "lastTransactionID": "100",
                "instruments": [{
                    "name": "EUR_USD", "type": "CURRENCY",
                    "pipLocation": -4, "displayPrecision": 5,
                    "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                    "maximumOrderUnits": "1000000",
                    "maximumPositionSize": "0", "marginRate": "0.02",
                }],
            })
        if path == "/pricing":
            return BrokerReply(200, self.prices)
        if path.startswith("/transactions/"):
            tid = path.rsplit("/", 1)[1]
            return BrokerReply(200, {
                "transaction": self.transactions[tid],
            })
        if path.startswith("/trades/"):
            return BrokerReply(200, {"trade": self.trade})
        raise AssertionError("Unknown fake broker GET")

    def mount(self, *args):
        pass

    def request(self, method, url, *, headers, json, timeout, allow_redirects):
        self.calls.append((method, url.rsplit("/accounts/"+ACCOUNT, 1)[-1]))
        if self.fail_write:
            raise TimeoutError("Synthetic ambiguous outcome")
        if method == "POST":
            self.order_exists = True
            body = json["order"]
            self.trade = {
                "id": "1001", "instrument": "EUR_USD", "state": "OPEN",
                "currentUnits": "20", "price": "1.10020",
                "stopLossOrder": {"price": body["stopLossOnFill"]["price"]},
                "takeProfitOrder": {"price": body["takeProfitOnFill"]["price"]},
                "clientExtensions": {
                    "id": "other" if self.bad_owner else ATTEMPT,
                },
            }
            fill = {
                "id": "101", "accountID": ACCOUNT, "type": "ORDER_FILL",
                "units": "20", "orderID": "100",
                "tradeOpened": {
                    "tradeID": "1001", "units": "20", "price": "1.10020",
                },
            }
            self.transactions["101"] = {
                "id": "101", "accountID": ACCOUNT, "type": "ORDER_FILL",
            }
            result = {
                "orderCreateTransaction": {"id": "100"},
                "orderFillTransaction": fill,
            }
            status = 201
        elif method == "PUT" and url.endswith("/close"):
            if json["units"] == "10":
                fill = {
                    "id": "102", "type": "ORDER_FILL",
                    "tradeReduced": {
                        "tradeID": "1001",
                        "units": "-9" if self.bad_partial else "-10",
                    },
                }
                self.trade["currentUnits"] = "10"
                status = 200
                result = {"orderFillTransaction": fill}
                self.transactions["102"] = {
                    "id": "102", "accountID": ACCOUNT, "type": "ORDER_FILL",
                }
            else:
                fill = {"id": "103", "type": "ORDER_FILL",
                        "tradesClosed": [{
                            "tradeID": "1001", "units": "-10",
                        }]}
                self.trade = None
                self.transactions["103"] = {
                    "id": "103", "accountID": ACCOUNT, "type": "ORDER_FILL",
                }
                result = {"orderFillTransaction": fill}
                status = 200
        else:
            raise AssertionError("Unexpected synthetic mutation")
        return FakeResponse(status, result)


class FakeResponse:
    headers = {}
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
    def json(self):
        return self.body


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeBroker()
        self.path = Path(self.tmp.name) / "evidence.jsonl"
        self.adapter = DemoAdapter(
            session=self.fake, reader=self.fake, evidence=self.path
        )
        for cm in (
            patch.object(config, "API_KEY", "synthetic-private-value"),
            patch.object(config, "ACCOUNT_ID", ACCOUNT),
            patch.object(config, "OANDA_URL", PRACTICE_API),
            patch.object(config, "ORDER_WRITES_ENABLED", False),
            patch("broker.practice_one_shot_runner.utc_now", return_value=NOW),
            patch.dict("os.environ", {
                "ALGIN_PRACTICE_ONE_SHOT_CONFIRM": ARM,
                "GITHUB_RUN_ATTEMPT": "1",
            }),
        ):
            cm.start()
            self.addCleanup(cm.stop)

    def test_one_time_open_partial_full_close_real_protocol_shape(self):
        self.assertEqual(exercise(self.adapter, verify_network=False),
                         "CONFIRMED")
        self.assertEqual(
            [verb for verb, _ in self.fake.calls if verb != "GET"],
            ["POST", "PUT", "PUT"],
        )
        self.assertIsNone(self.fake.trade)
        self.assertEqual(self.adapter.writes, 3)
        statuses = [
            json.loads(x)["status"]
            for x in self.path.read_text().splitlines()
        ]
        self.assertIn("PRACTICE_OPEN_PROTECTED_CONFIRMED", statuses)
        self.assertIn("PRACTICE_PARTIAL_CLOSE_CONFIRMED", statuses)
        self.assertIn("PRACTICE_FULL_CYCLE_CONFIRMED", statuses)
        self.assertIn("BREAK_EVEN_SKIPPED_PRICE_CONDITION", statuses)

    def test_rerun_prohibited_by_own_evidence_and_broker_order_id(self):
        exercise(self.adapter, verify_network=False)
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.path.unlink()
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 3)

    def test_bad_account_blocks_before_any_write(self):
        self.fake.bad_account = True
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 0)

    def test_unfavorable_or_crossed_quotes_block(self):
        self.fake.prices["prices"][0]["asks"][0]["price"] = "1.09990"
        with self.assertRaises(Exception):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 0)

    def test_unknown_timeout_causes_no_retry_and_no_close(self):
        self.fake.fail_write = True
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 1)
        self.assertEqual(
            [verb for verb, _ in self.fake.calls if verb != "GET"], ["POST"],
        )

    def test_partial_mismatch_prevents_any_full_close(self):
        self.fake.bad_partial = True
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 2)
        self.assertIsNotNone(self.fake.trade)
        self.assertEqual(self.fake.trade["currentUnits"], "10")

    def test_invalid_owner_keeps_trade_protected_without_next_write(self):
        self.fake.bad_owner = True
        with self.assertRaises(DemoBlocked):
            exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 1)

    def test_incorrect_environment_and_write_release_fail_before_network(self):
        with patch.object(config, "OANDA_URL", "https://api-fxtrade.oanda.com/v3"):
            with self.assertRaises(DemoBlocked):
                exercise(self.adapter, verify_network=False)
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            with self.assertRaises(DemoBlocked):
                exercise(self.adapter, verify_network=False)
        self.assertEqual(self.fake.calls, [])

    def test_not_armed_or_second_attempt_blocked(self):
        for field, value in (
            ("ALGIN_PRACTICE_ONE_SHOT_CONFIRM", "NO"),
            ("GITHUB_RUN_ATTEMPT", "2"),
        ):
            with self.subTest(field=field), patch.dict(
                "os.environ", {field: value}
            ):
                with self.assertRaises(DemoBlocked):
                    exercise(self.adapter, verify_network=False)
        self.assertEqual(self.adapter.writes, 0)

    def test_other_mutation_methods_rejected_before_write(self):
        with self.assertRaises(DemoBlocked):
            self.adapter.send(
                "DELETE", "/orders/1", {}, operation="FORBIDDEN"
            )
        self.assertEqual(self.adapter.writes, 0)

    def test_hard_risk_cap_and_order_bound_are_below_one_dollar(self):
        data, rules, quote, sl, tp, bound = preflight(
            self.adapter, verify_network=False
        )
        self.assertEqual(data["order"]["units"], "20")
        self.assertEqual(data["order"]["type"], "MARKET")
        self.assertEqual(data["order"]["timeInForce"], "FOK")
        self.assertEqual(data["order"]["positionFill"], "OPEN_ONLY")
        self.assertEqual((bound-sl)*Decimal("20") <= Decimal(".25"), True)
        self.assertLess(sl, quote.bid)
        self.assertGreater(tp, quote.ask)


if __name__ == "__main__":
    unittest.main()
