"""Broker-free regression tests: no tokens, no network, no real orders."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from broker.oanda_client import (BrokerError, OutcomeUnknown, OandaClient,
                                  quote_from_pricing, trade_fill)
from data.data_handler import DataHandler
from execution.trade_executor import TradeExecutor
from risk.trade_plan import make_plan, home_conversion
from storage.state_store import StateError, StateStore
from utils.event_log import EventLogger


SPEC = {"name": "EUR_USD", "pipLocation": -4, "displayPrecision": 5,
        "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
        "maximumOrderUnits": "100000", "marginRate": "0.02"}
QUOTE = {"instrument": "EUR_USD", "bid": Decimal("1.10000"),
         "ask": Decimal("1.10020")}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, *, fails=False):
        self.fails = fails
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.fails:
            raise requests.Timeout("response lost")
        if method == "POST":
            return FakeResponse(201, {
                "orderFillTransaction": {"id": "11", "tradeOpened": {
                    "tradeID": "12", "price": "1.10020", "units": "100"}}})
        return FakeResponse(200, {"stopLossOrderTransaction": {"id": "50"}})


class FakeEvents:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


class FakeClient:
    def __init__(self):
        self.calls = 0

    def create_order(self, _):
        self.calls += 1
        raise OutcomeUnknown("simulated lost response")


def plan_for(direction="LONG", **kwargs):
    return make_plan(instrument="EUR_USD", direction=direction, quote=QUOTE,
                     spec=SPEC, nav=kwargs.get("nav", "10000"),
                     margin_available="8000", risk_pct=".5",
                     atr=".001", account_loss_factor="1",
                     position_value_factor="1", min_distance_pips="3",
                     max_spread_pips="2.5", slippage_pips="1")


class BrokerTests(unittest.TestCase):
    def test_trade_id_from_trade_open_only(self):
        result = trade_fill({"orderFillTransaction": {
            "id": "99", "tradeOpened": {"tradeID": "100", "units": "5",
                                        "price": "1.12345"}}})
        self.assertEqual(result["trade_id"], "100")
        with self.assertRaises(OutcomeUnknown):
            trade_fill({"orderCreateTransaction": {"id": "99"}})

    def test_no_mutating_retry_on_lost_reply(self):
        session = FakeSession(fails=True)
        client = OandaClient("TEST_ONLY", "demo", session)
        with self.assertRaises(OutcomeUnknown):
            client.create_order({"instrument": "EUR_USD"})
        self.assertEqual(len(session.calls), 1)

    def test_put_omits_take_profit_and_partial_positive(self):
        session = FakeSession()
        client = OandaClient("TEST_ONLY", "demo", session)
        client.set_dependent_orders("2", stop_loss="1.10000")
        self.assertEqual(session.calls[-1][2]["json"], {"stopLoss": {"price": "1.10000"}})
        client.close_trade("2", "12")
        self.assertEqual(session.calls[-1][2]["json"], {"units": "12"})
        with self.assertRaises(BrokerError):
            client.close_trade("2", "-12")

    def test_quote_stale_and_tradeable(self):
        now = datetime.now(timezone.utc)
        raw = {"prices": [{"instrument": "EUR_USD", "tradeable": True,
                           "time": now.isoformat(),
                           "bids": [{"price": "1.1000"}],
                           "asks": [{"price": "1.1002"}]}]}
        self.assertEqual(quote_from_pricing(raw, "EUR_USD", now)["bid"], Decimal("1.1000"))
        raw["prices"][0]["tradeable"] = False
        with self.assertRaises(BrokerError):
            quote_from_pricing(raw, "EUR_USD", now)
        raw["prices"][0]["tradeable"] = True
        raw["prices"][0]["time"] = (now - timedelta(minutes=5)).isoformat()
        with self.assertRaises(BrokerError):
            quote_from_pricing(raw, "EUR_USD", now)


class SizingTests(unittest.TestCase):
    def test_long_short_signed_units_and_risk_cap(self):
        long = plan_for()
        self.assertGreater(long.units, 0)
        self.assertLessEqual(long.risk_home, Decimal("50"))
        self.assertLess(long.stop_loss, long.entry_hint)
        self.assertGreater(long.take_profit, long.entry_hint)
        short = plan_for("SHORT")
        self.assertLess(short.units, 0)
        self.assertGreater(short.stop_loss, short.entry_hint)
        self.assertLess(short.take_profit, short.entry_hint)

    def test_worst_case_pricebound_risk_capped(self):
        plan = plan_for()
        worst_case_loss = abs(plan.price_bound - plan.stop_loss) * abs(plan.units)
        self.assertLessEqual(worst_case_loss, Decimal("50"))
        self.assertEqual(plan.risk_home, worst_case_loss)

    def test_nonfinite_and_below_minimum_rejected(self):
        with self.assertRaises(BrokerError):
            plan_for(nav="NaN")
        with self.assertRaises(BrokerError):
            plan_for(nav="0")
        spec = {**SPEC, "minimumTradeSize": "100000"}
        with self.assertRaises(BrokerError):
            make_plan(instrument="EUR_USD", direction="LONG", quote=QUOTE,
                      spec=spec, nav="10000", margin_available="8000", risk_pct=".5",
                      atr=".001", account_loss_factor="1",
                      position_value_factor="1")

    def test_spread_and_conversion_fail_closed(self):
        with self.assertRaises(BrokerError):
            home_conversion({}, "CAD", "USD")
        loss, position = home_conversion({"homeConversions": [
            {"currency": "CAD", "accountLoss": "0.77", "positionValue": "0.76"}]},
            "CAD", "USD")
        self.assertEqual(loss, Decimal(".77"))
        self.assertEqual(position, Decimal(".76"))
        with self.assertRaises(BrokerError):
            make_plan(instrument="EUR_USD", direction="LONG",
                      quote={**QUOTE, "ask": Decimal("1.1010")}, spec=SPEC,
                      nav="10000", margin_available="8000", risk_pct=".5",
                      atr=".001", account_loss_factor="1", position_value_factor="1")


class JournalTests(unittest.TestCase):
    def test_corrupt_state_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore("demo", temp)
            store.save(store.initial())
            self.assertEqual(store.load()["account_id"], "demo")
            Path(temp, "state.json").write_text("NOT JSON")
            with self.assertRaises(StateError):
                store.load()

    def test_instance_lock_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore("demo", temp)
            with store.single_instance():
                with self.assertRaises(StateError):
                    with store.single_instance():
                        pass

    def test_unknown_submit_is_journaled_and_never_resent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore("demo", temp)
            state = store.initial()
            client = FakeClient()
            service = TradeExecutor(client, store, FakeEvents(), submission_enabled=True)
            first = service.submit(state, plan_for(), "abcdef123456", "A")
            second = service.submit(state, plan_for(), "abcdef123456", "A")
            self.assertEqual(first["status"], "UNKNOWN")
            self.assertEqual(second["status"], "BLOCKED")
            self.assertEqual(client.calls, 1)
            self.assertIn("UNKNOWN", [v["status"] for v in store.load()["intents"].values()])

    def test_read_only_mode_does_not_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore("demo", temp)
            client = FakeClient()
            service = TradeExecutor(client, store, FakeEvents(), submission_enabled=False)
            result = service.submit(store.initial(), plan_for(), "abcdef123456", "A")
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(client.calls, 0)



class FillVerificationTests(unittest.TestCase):
    def _client(self, plan, *, stop=None, missing_tp=False):
        class Client:
            writes = 0
            def create_order(self, payload):
                self.writes += 1
                return {"orderFillTransaction": {
                    "id": "1", "tradeOpened": {"tradeID": "123",
                                              "price": str(plan.entry_hint), "units": str(plan.units)}}}
            def trade(self, _trade_id):
                value = {"id": "123", "state": "OPEN", "instrument": plan.instrument,
                         "price": str(plan.entry_hint), "initialUnits": str(plan.units),
                         "currentUnits": str(plan.units),
                         "stopLossOrder": {"price": str(stop if stop is not None else plan.stop_loss)},
                         "clientExtensions": {"id": TradeExecutor._safe_id("abcdef123456")}}
                if not missing_tp:
                    value["takeProfitOrder"] = {"price": str(plan.take_profit)}
                return {"trade": value}
        return Client()

    def test_post_fill_account_readback_confirms(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore("demo", temp)
            state = store.initial()
            plan = plan_for()
            client = self._client(plan)
            result = TradeExecutor(client, store, FakeEvents(), True).submit(state, plan, "abcdef123456", "A")
            self.assertEqual(result["status"], "CONFIRMED")
            self.assertEqual(client.writes, 1)
            self.assertEqual(state["trades"]["123"]["entry"], str(plan.entry_hint))

    def test_missing_tp_or_mismatched_stop_is_unverified(self):
        for altered in ("missing", "wrong_stop"):
            with self.subTest(altered=altered), tempfile.TemporaryDirectory() as temp:
                store = StateStore("demo", temp)
                state = store.initial()
                plan = plan_for()
                client = self._client(plan,
                                      stop="0.1" if altered == "wrong_stop" else None,
                                      missing_tp=altered == "missing")
                result = TradeExecutor(client, store, FakeEvents(), True).submit(
                    state, plan, "abcdef123456", "A")
                self.assertEqual(result["status"], "UNPROTECTED_OR_UNVERIFIED")
                self.assertNotIn("CONFIRMED", [it["status"] for it in store.load()["intents"].values()])


class StructureTests(unittest.TestCase):
    def test_confirmed_choch_is_directional(self):
        from strategy.structure_engine import StructureEngine, SwingPoint
        swings = [SwingPoint(1, 120, True), SwingPoint(2, 100, False),
                  SwingPoint(4, 110, True), SwingPoint(5, 90, False)]
        e = StructureEngine()
        bos, choch, direction = e._detect_breaks([{"close": 125}], swings)
        self.assertEqual((bos, choch, direction), (False, True, "UP"))
        bos, choch, direction = e._detect_breaks([{"close": 80}], swings)
        self.assertEqual((bos, choch, direction), (True, False, "DOWN"))


class CandleTests(unittest.TestCase):
    def test_incomplete_last_candle_excluded(self):
        class Client:
            def candles(self, instrument, granularity, count):
                base = datetime(2026, 1, 1, tzinfo=timezone.utc)
                candles = []
                for i in range(61):
                    candles.append({"complete": i < 60,
                        "time": (base + timedelta(minutes=15 * i)).isoformat(),
                        "mid": {"o": "1.0", "h": "1.1", "l": "0.9", "c": "1.0"}})
                return {"instrument": instrument, "granularity": granularity, "candles": candles}
        candles = DataHandler(Client()).get_candles("EUR_USD")
        self.assertEqual(len(candles), 60)
        self.assertEqual(candles[-1]["close"], 1.0)


if __name__ == "__main__":
    unittest.main()
