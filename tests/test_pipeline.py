"""End-to-end offline pipeline fixtures for one LONG and one SHORT."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import config
from broker.oanda_client import OutcomeUnknown
from runtime.engine import TradingEngine
from storage.state_store import StateStore


SPEC = {"name": "EUR_USD", "pipLocation": -4, "displayPrecision": 5,
        "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
        "maximumOrderUnits": "100000", "marginRate": "0.02"}
BID, ASK = Decimal("1.10000"), Decimal("1.10020")


class Events:
    def __init__(self):
        self.events = []

    def emit(self, event, **kwargs):
        self.events.append((event, kwargs))


class PracticeFixture:
    def __init__(self):
        self.writes = []
        self.remote = {}
        self.foreign_trade = None

    def account(self):
        return {"account": {"id": "demo", "NAV": "10000",
                "marginAvailable": "8000", "currency": "USD"},
                "lastTransactionID": "8"}

    def instruments(self):
        return {"instruments": [SPEC]}

    def open_trades(self):
        results = list(self.remote.values())
        if self.foreign_trade:
            results.append(self.foreign_trade)
        return {"trades": results}

    def pending_orders(self):
        return {"orders": []}

    def pricing(self, instrument):
        return {"prices": [{
            "instrument": instrument, "time": datetime.now(timezone.utc).isoformat(),
            "tradeable": True, "bids": [{"price": str(BID)}],
            "asks": [{"price": str(ASK)}]}], "homeConversions": []}

    def create_order(self, order):
        self.writes.append(order)
        units = Decimal(order["units"])
        entry = ASK if units > 0 else BID
        self.remote["42"] = {
            "id": "42", "state": "OPEN", "instrument": order["instrument"],
            "price": str(entry), "initialUnits": str(units),
            "currentUnits": str(units),
            "stopLossOrder": dict(order["stopLossOnFill"]),
            "takeProfitOrder": dict(order["takeProfitOnFill"]),
            "clientExtensions": dict(order["tradeClientExtensions"]),
            "openTime": datetime.now(timezone.utc).isoformat()
        }
        return {"orderFillTransaction": {"id": "11", "tradeOpened": {
            "tradeID": "42", "price": str(entry), "units": str(units)}}}

    def trade(self, trade_id):
        return {"trade": self.remote[str(trade_id)]}


class PipelineTests(unittest.TestCase):
    def _engine(self, temp, client, direction="LONG", enabled=True):
        engine = TradingEngine(client, StateStore("demo", temp), Events(),
                               submit_enabled=enabled)
        # All substitutes are local deterministic data; broker calls are fixtures.
        candle_id = (datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
        engine.data.get_candles = lambda _inst: [{"time": candle_id}]
        engine.data.get_quote_and_conversions = lambda inst: (
            {"instrument": inst, "bid": BID, "ask": ASK},
            {"homeConversions": []})
        engine._setup = lambda *_: (SimpleNamespace(
            direction=direction, grade="B", score=75.0), 0.001)
        return engine

    def test_positive_long_short_then_no_duplicate_after_restart(self):
        with patch.object(config, "INSTRUMENTS", ("EUR_USD",)), \
             patch.object(config, "ENABLE_SESSIONS", False), \
             patch.object(config, "NEWS_MODE", "DISABLED_ACKNOWLEDGED"):
            for direction in ("LONG", "SHORT"):
                with self.subTest(direction=direction), tempfile.TemporaryDirectory() as temp:
                    client = PracticeFixture()
                    engine = self._engine(temp, client, direction)
                    engine.cycle()
                    self.assertEqual(len(client.writes), 1)
                    order = client.writes[0]
                    self.assertEqual((Decimal(order["units"]) > 0), direction == "LONG")
                    self.assertEqual(len(engine.store.load()["trades"]), 1)
                    restarted = self._engine(temp, client, direction)
                    restarted.cycle()
                    self.assertEqual(len(client.writes), 1)

    def test_manual_trade_blocks_new_entry(self):
        with patch.object(config, "INSTRUMENTS", ("EUR_USD",)), \
             patch.object(config, "ENABLE_SESSIONS", False), \
             patch.object(config, "NEWS_MODE", "DISABLED_ACKNOWLEDGED"), \
             tempfile.TemporaryDirectory() as temp:
            client = PracticeFixture()
            client.foreign_trade = {"id": "999", "state": "OPEN",
                                    "instrument": "EUR_USD", "currentUnits": "12"}
            engine = self._engine(temp, client)
            engine.cycle()
            self.assertEqual(client.writes, [])
            self.assertTrue(any(e == "ENTRY_GATED" and "Foreign" in d.get("reason", "")
                                for e, d in engine.events.events))

    def test_lost_write_response_can_reconcile_without_resend(self):
        class LostResponseFixture(PracticeFixture):
            def create_order(self, order):
                super().create_order(order)
                raise OutcomeUnknown("response lost after broker accepted")
        with patch.object(config, "INSTRUMENTS", ("EUR_USD",)), \
             patch.object(config, "ENABLE_SESSIONS", False), \
             patch.object(config, "NEWS_MODE", "DISABLED_ACKNOWLEDGED"), \
             tempfile.TemporaryDirectory() as temp:
            client = LostResponseFixture()
            first = self._engine(temp, client)
            first.cycle()
            self.assertEqual(len(client.writes), 1)
            self.assertIn("UNKNOWN", [x["status"] for x in first.store.load()["intents"].values()])
            second = self._engine(temp, client)
            second.cycle()
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(len(second.store.load()["trades"]), 1)
            self.assertTrue(any(x["status"] == "CONFIRMED" for x in second.store.load()["intents"].values()))


if __name__ == "__main__":
    unittest.main()
