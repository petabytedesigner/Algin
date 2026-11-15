"""Category 1 baseline tests: no broker mutation and stable public interfaces."""
from __future__ import annotations

import importlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import main
from execution.order_validation import OrderValidation
from execution.partial_close_engine import PartialCloseEngine
from execution.trade_executor import BrokerDataError, OrderRequest, TradeExecutor
from risk.cooldown_engine import CooldownEngine
from risk.risk_engine import Grade, RiskConfig, RiskEngine, TradeCandidate
from utils.common import safe_request
from utils.time_tools import TimeTools, iso_utc, parse_utc


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class Category1Tests(unittest.TestCase):
    def test_canonical_imports_and_no_old_filenames(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "main_v10.py").exists())
        self.assertFalse((root / "config_v10.py").exists())
        for path in (
            "config", "main", "data.data_handler", "data.news_filter",
            "data.market_sessions", "execution.trade_executor",
            "execution.oanda_patch", "execution.partial_close_engine",
            "execution.safety_check", "risk.risk_engine",
            "risk.cooldown_engine", "risk.scoring_engine",
            "strategy.strategy", "utils.common", "utils.time_tools",
        ):
            importlib.import_module(path)
        self.assertEqual(main.main(["--check"]), 0)
        self.assertFalse(config.ORDER_WRITES_ENABLED)

    def test_risk_candidate_not_nonexistent_signal_method(self):
        risk = main.build_risk_engine()
        at = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
        signal = SimpleNamespace(instrument="EUR_USD", direction="LONG",
                                 grade="A", score=85)
        decision = main.evaluate_signal_risk(risk, signal, at)
        self.assertTrue(decision.accept)
        with self.assertRaises(ValueError):
            main.evaluate_signal_risk(risk, signal, at.replace(tzinfo=None))

    def test_iso_roundtrip_no_double_zone_suffix(self):
        local = datetime(2026, 11, 15, 10, 20,
                         tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(iso_utc(local), "2026-11-15T09:20:00Z")
        self.assertEqual(parse_utc("2026-11-15T09:20:00Z"),
                         datetime(2026, 11, 15, 9, 20, tzinfo=timezone.utc))
        self.assertTrue(TimeTools().now_iso().endswith("Z"))
        self.assertIsNone(TimeTools().parse_iso("2026-11-15T09:20:00+00:00Z"))
        with self.assertRaises(ValueError):
            parse_utc("2026-11-15T09:20:00")
        self.assertEqual(parse_utc("2026-11-15T09:20:00",
                                   legacy_naive_utc=True).tzinfo, timezone.utc)

    def test_cooldown_timestamps_are_aware(self):
        now = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
        engine = CooldownEngine()
        engine.register_loss("EUR_USD", now)
        self.assertTrue(engine.is_in_cooldown("EUR_USD",
                                             now + timedelta(minutes=20)))
        self.assertFalse(engine.is_in_cooldown("EUR_USD",
                                              now + timedelta(minutes=30)))
        with self.assertRaises(ValueError):
            engine.is_in_cooldown("EUR_USD", now.replace(tzinfo=None))
        risk = RiskEngine(RiskConfig(cooldown_minutes_after_loss=30))
        risk.register_loss("EUR_USD", now)
        candidate = TradeCandidate("EUR_USD", "LONG", Grade.A, 80, 1,
                                   now + timedelta(minutes=20))
        self.assertFalse(risk.evaluate_candidate(candidate, []).accept)

    def test_direction_validation_long_and_short(self):
        v = OrderValidation()
        self.assertTrue(v.validate("EUR_USD", "LONG", 10, 1.08, 1.12, 1.10, .001)["is_valid"])
        self.assertTrue(v.validate("EUR_USD", "SHORT", -10, 1.12, 1.08, 1.10, .001)["is_valid"])
        self.assertFalse(v.validate("EUR_USD", "SHORT", 10, 1.12, 1.08, 1.10, .001)["is_valid"])
        self.assertFalse(v.validate("EUR_USD", "LONG", 10, float("nan"), 1.12, 1.10, .001)["is_valid"])
        self.assertFalse(v.validate("EUR_USD", "HOLD", 10, 1.08, 1.12, 1.10, .001)["is_valid"])

    def test_old_http_adapter_cannot_submit_or_retry(self):
        with patch("requests.post") as post, patch("requests.put") as put:
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                with self.assertRaises(RuntimeError):
                    safe_request(verb, "https://example.invalid", retries=3)
            post.assert_not_called()
            put.assert_not_called()

    def test_trade_executor_normalization_and_bad_snapshot(self):
        service = TradeExecutor()
        payload = {"trades": [{"id": "101", "instrument": "EUR_USD",
                               "currentUnits": "-2.5", "price": "1.1000",
                               "stopLossOrder": {"price": "1.1200"},
                               "takeProfitOrder": {"price": "1.0800"}}]}
        with patch("execution.trade_executor.safe_request", return_value=FakeResponse(payload)):
            trades = service.get_open_trades()
        self.assertEqual(trades[0]["direction"], "SHORT")
        self.assertEqual(trades[0]["sl"], 1.12)
        with patch("execution.trade_executor.safe_request", return_value=FakeResponse({"trades": {}})):
            with self.assertRaises(BrokerDataError):
                service.get_open_trades()

    def test_quote_normalized_only_if_tradeable(self):
        service = TradeExecutor()
        quote = {"prices": [{"instrument": "EUR_USD", "tradeable": True,
                             "bids": [{"price": "1.1000"}],
                             "asks": [{"price": "1.1002"}]}]}
        with patch("execution.trade_executor.safe_request", return_value=FakeResponse(quote)):
            self.assertEqual(service.get_price("EUR_USD")["ask"], 1.1002)
        quote["prices"][0]["tradeable"] = False
        with patch("execution.trade_executor.safe_request", return_value=FakeResponse(quote)):
            with self.assertRaises(BrokerDataError):
                service.get_price("EUR_USD")

    def test_executor_and_legacy_partial_cannot_mutate(self):
        service = TradeExecutor()
        order = OrderRequest("EUR_USD", "BUY", 10, 1.08, 1.12)
        with patch("execution.trade_executor.safe_request") as network:
            with self.assertRaises(RuntimeError):
                service.open_market_order(order)
            with self.assertRaises(RuntimeError):
                service.modify_sl_tp("101", sl_price=1.1)
            with self.assertRaises(RuntimeError):
                service.close_trade("101", units=2)
            with self.assertRaises(RuntimeError):
                PartialCloseEngine(service).check_and_execute()
            network.assert_not_called()

    def test_scan_blocks_unverified_broker_snapshot(self):
        with patch.object(config, "API_KEY", "fake"), patch.object(config, "ACCOUNT_ID", "fake"), \
             patch.object(TradeExecutor, "get_open_trades", side_effect=BrokerDataError("no data")):
            self.assertEqual(main.scan_once(), 3)

    def test_scan_stops_at_existing_open_exposure(self):
        with patch.object(config, "API_KEY", "fake"), patch.object(config, "ACCOUNT_ID", "fake"), \
             patch.object(TradeExecutor, "get_open_trades", return_value=[{"id": "1"}]), \
             patch("main.DataHandler") as data:
            self.assertEqual(main.scan_once(), 0)
            data.assert_not_called()

    def test_standard_indicator_class_name(self):
        from utils.indicators import Indicators
        indicator = Indicators()
        self.assertTrue(callable(indicator.ema))
        self.assertFalse(hasattr(importlib.import_module("utils.indicators"),
                                 "IndicatorsV10"))



    def test_reject_invalid_broker_trade_identifiers_and_state(self):
        invalid = [
            {"id": None, "instrument": "EUR_USD", "currentUnits": "1", "price": "1.1"},
            {"id": "7", "instrument": "EUR_USD", "currentUnits": "0", "price": "1.1"},
            {"id": "7", "instrument": "EUR_USD", "currentUnits": "1", "price": "nan"},
            {"id": "7", "instrument": "EUR_USD", "currentUnits": "1", "price": "1.1",
             "state": "CLOSED"},
            {"id": "7", "instrument": "EUR_USD", "currentUnits": "1", "price": "1.1",
             "stopLossOrder": {"id": "sl"}},
        ]
        for trade in invalid:
            with self.subTest(trade=trade):
                with patch("execution.trade_executor.safe_request",
                           return_value=FakeResponse({"trades": [trade]})):
                    with self.assertRaises(BrokerDataError):
                        TradeExecutor().get_open_trades()

    def test_reject_duplicate_or_missing_broker_trade_snapshot(self):
        trade = {"id": "7", "instrument": "EUR_USD", "currentUnits": "1",
                 "price": "1.1000"}
        invalid = (None, {"trades": {}}, {"trades": [trade, trade]},
                   {"trades": [None]}, {"trades": {"id": "7"}})
        for data in invalid:
            with self.subTest(data=data):
                with patch("execution.trade_executor.safe_request",
                           return_value=FakeResponse(data)):
                    with self.assertRaises(BrokerDataError):
                        TradeExecutor().get_open_trades()

    def test_safety_check_uses_verified_normalized_broker_snapshot(self):
        from execution.safety_check import SafetyCheck
        snapshot = [
            {"id": "12", "instrument": "EUR_USD", "sl": None, "tp": 1.2},
            {"id": "13", "instrument": "GBP_USD", "sl": 1.1, "tp": 1.3}
        ]
        with patch.object(TradeExecutor, "get_open_trades", return_value=snapshot):
            result = SafetyCheck().check_all()
        self.assertEqual(result["12"]["issues"], ["Mungon SL"])
        self.assertEqual(result["13"]["issues"], [])
        with patch.object(TradeExecutor, "get_open_trades",
                          side_effect=BrokerDataError("unavailable")):
            result = SafetyCheck().check_all()
        self.assertIn("__broker_error__", result)
        with patch.object(TradeExecutor, "get_open_trades", return_value=[]):
            self.assertEqual(SafetyCheck().check_all(), {})

    def test_no_unreachable_mutations_or_legacy_csv_writer(self):
        root = Path(__file__).resolve().parents[1]
        for path in (
            "execution/trade_executor.py", "execution/partial_close_engine.py",
            "execution/safety_check.py", "utils/common.py"
        ):
            code = (root / path).read_text(encoding="utf-8")
            self.assertNotIn('safe_request("POST"', code)
            self.assertNotIn('safe_request("PUT"', code)
            self.assertNotIn("csv.writer(", code)
        from execution import trade_executor, partial_close_engine, safety_check
        self.assertFalse(hasattr(trade_executor, "csv_log"))
        self.assertFalse(hasattr(partial_close_engine, "csv_log"))
        self.assertFalse(hasattr(safety_check, "csv_log"))

    def test_ambiguous_untradeable_and_invalid_quotes_rejected(self):
        valid = {"instrument": "EUR_USD", "tradeable": True,
                 "bids": [{"price": "1.1000"}],
                 "asks": [{"price": "1.1002"}]}
        invalid = [
            {"prices": [valid, valid]},
            {"prices": [{"instrument": "GBP_USD", "tradeable": True}]},
            {"prices": [dict(valid, tradeable=False)]},
            {"prices": [dict(valid, asks=[{"price": "nan"}])]},
            {"prices": {}}
        ]
        for data in invalid:
            with self.subTest(data=data):
                with patch("execution.trade_executor.safe_request",
                           return_value=FakeResponse(data)):
                    with self.assertRaises(BrokerDataError):
                        TradeExecutor().get_price("EUR_USD")

    def test_legacy_pricing_reader_rejects_wrong_instrument(self):
        from utils.common import get_pricing
        data = {"prices": [{"instrument": "GBP_USD", "tradeable": True,
                            "bids": [{"price": "1.10"}],
                            "asks": [{"price": "1.11"}]}]}
        with patch("utils.common.safe_request", return_value=FakeResponse(data)):
            self.assertIsNone(get_pricing("EUR_USD"))

    def test_trade_id_only_from_order_fill_trade_opened(self):
        from execution.oanda_patch import extract_trade_id_from_order_response
        self.assertEqual(extract_trade_id_from_order_response({
            "orderFillTransaction": {"tradeOpened": {"tradeID": "9001"}}
        }), "9001")
        invalid = [
            {"orderFillTransaction": {"id": "999"}},
            {"orderCreateTransaction": {"id": "888"}},
            {"orderFillTransaction": {"tradeReduced": {"tradeID": "9001"}}},
            {"orderFillTransaction": {"tradesOpened": [{"tradeID": "9001"}]}},
            None
        ]
        for data in invalid:
            with self.subTest(data=data):
                self.assertIsNone(extract_trade_id_from_order_response(data))

    def test_account_scoped_candle_api_contract(self):
        from data.data_handler import DataHandler
        data = {"candles": [{
            "time": "2026-11-15T09:00:00Z",
            "complete": True,
            "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.1"}
        }]}
        with patch("data.data_handler.safe_request",
                   return_value=FakeResponse(data)) as get:
            candles = DataHandler().get_candles("EUR_USD", "M15", 1)
        self.assertEqual(len(candles), 1)
        url = get.call_args.args[1]
        self.assertIn("/accounts/", url)
        self.assertIn("/instruments/EUR_USD/candles", url)

    def test_check_mode_does_not_touch_network(self):
        with patch("requests.get") as get:
            with patch("requests.post") as post:
                with patch("requests.put") as put:
                    self.assertEqual(main.main(["--check"]), 0)
                    get.assert_not_called()
                    post.assert_not_called()
                    put.assert_not_called()

    def test_http_read_failure_does_not_look_successful(self):
        from requests.exceptions import Timeout
        with patch("requests.get", side_effect=Timeout):
            from utils.common import safe_request
            self.assertIsNone(safe_request("GET", "https://example.invalid",
                                           retries=1))



if __name__ == "__main__":
    unittest.main()
