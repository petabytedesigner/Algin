import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import config
from risk.risk_engine import (Grade, OpenPositionRisk, RiskConfig,
                              RiskDecisionCode, RiskEngine, TradeCandidate)
from runtime.engine import TradingEngine
from storage.state_store import StateStore


class Events:
    def emit(self, *args, **kwargs):
        pass


class FakeClient:
    def __init__(self):
        self.nav = "10000"
    def account(self):
        return {"account": {"id": "demo", "NAV": self.nav,
                "marginAvailable": "5000", "currency": "USD"},
                "lastTransactionID": "2"}
    def open_trades(self):
        return {"trades": []}
    def pending_orders(self):
        return {"orders": []}


class RiskLimitTests(unittest.TestCase):
    def test_shared_currency_concentration_denies_correlated_exposure(self):
        now = datetime.now(timezone.utc)
        engine = RiskEngine(RiskConfig(
            max_total_risk_pct=5, risk_pct_grade_a=.5, risk_pct_grade_b=.5,
            max_shared_currency_risk_pct=1.0))
        existing = [OpenPositionRisk("1", "EUR_USD", "LONG", Grade.B, .8, now)]
        decision = engine.evaluate_candidate(
            TradeCandidate("GBP_USD", "LONG", Grade.B, 75, .4, now), existing)
        self.assertEqual(decision.code, RiskDecisionCode.REJECT_CURRENCY_CONCENTRATION)
        unrelated = engine.evaluate_candidate(
            TradeCandidate("AUD_NZD", "LONG", Grade.B, 75, .4, now), existing)
        self.assertTrue(unrelated.accept)

    def test_daily_nav_loss_stops_entries_without_suppressing_snapshot(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(config, "ENABLE_SESSIONS", False), \
             patch.object(config, "NEWS_MODE", "DISABLED_ACKNOWLEDGED"):
            client = FakeClient()
            engine = TradingEngine(client, StateStore("demo", temp), Events())
            engine._snapshot()
            allowed, _ = engine._entries_allowed(datetime.now(timezone.utc), {}, [])
            self.assertTrue(allowed)
            client.nav = "9700"
            engine._snapshot()
            allowed, reason = engine._entries_allowed(datetime.now(timezone.utc), {}, [])
            self.assertFalse(allowed)
            self.assertIn("Daily", reason)


if __name__ == "__main__":
    unittest.main()
