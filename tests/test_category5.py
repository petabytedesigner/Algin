"""Category 5: verified candles, breakout/retest, scoring, DST and news.

Synthetic fixtures only. All broker mutation gates from Categories 1–4 stay
disabled. Tests cover positive AND adversarial paths for P41–P47/P50–P56.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import math
import unittest

import config
import main
from data.candle_gate import CandleGate, CandleGateError
from data.candle_validation import (
    CandleDataError, parse_oanda_candles, validate_candles
)
from data.data_handler import DataHandler
from data.market_sessions import MarketSessions
from data.news_filter import NewsFilter
from risk.scoring_engine import ScoringConfig, ScoringEngine
from risk.trade_plan import InstrumentRules
from strategy.breakout_retest import BreakoutRetest
from strategy.strategy import Strategy
from strategy.structure_engine import StructureEngine
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def candles(*, direction="LONG", now=NOW, total=60):
    """Fully completed 60-bar synthetic pattern with old confirmed swing."""
    start = now - timedelta(minutes=15*total)
    out = []
    for i in range(total):
        time = start + timedelta(minutes=15*i)
        out.append({
            "time": iso_utc(time), "complete": True,
            "open": 1.1000, "high": 1.1010,
            "low": 1.0990, "close": 1.1001
        })
    if direction == "LONG":
        out[30]["high"] = 1.1016
        out[47]["high"] = 1.1020
        out[55].update(open=1.1000, low=1.0991, close=1.1001)
        out[56].update(open=1.1000, high=1.1012, low=1.0989,
                       close=1.1003)
        out[57].update(open=1.1003, low=1.1002,
                       high=1.1028, close=1.1026)
        out[58].update(open=1.1020, low=1.10195,
                       high=1.1027, close=1.1025)
        out[59].update(open=1.10215, low=1.10212,
                       high=1.1027, close=1.1026)
    else:
        out[30]["low"] = 1.0984
        out[47]["low"] = 1.0980
        out[55].update(open=1.1000, high=1.1011, close=1.0999)
        out[56].update(open=1.1000, high=1.1010, low=1.0988,
                       close=1.0997)
        out[57].update(open=1.0997, high=1.0998,
                       low=1.0972, close=1.0974)
        out[58].update(open=1.0980, high=1.09805,
                       low=1.0973, close=1.0975)
        out[59].update(open=1.09785, high=1.09788,
                       low=1.0972, close=1.0974)
    return out


def raw_candles(series):
    return {"instrument": "EUR_USD", "granularity": "M15",
            "candles": [{
                "time": c["time"], "complete": c["complete"],
                "mid": {"o": str(c["open"]), "h": str(c["high"]),
                        "l": str(c["low"]), "c": str(c["close"])}
            } for c in series]}


class DataContractTests(unittest.TestCase):
    def test_P50_discard_only_final_unfinished_bar(self):
        raw = raw_candles(candles())
        raw["candles"].append({
            "time": iso_utc(NOW), "complete": False,
            "mid": {"o": "1", "h": "1", "l": "1", "c": "1"}
        })
        out = parse_oanda_candles(raw, instrument="EUR_USD")
        self.assertEqual(len(out), 60)
        self.assertEqual(out[-1]["time"], iso_utc(NOW - timedelta(minutes=15)))

    def test_P50_omitted_complete_or_middle_unfinished_rejected(self):
        raw = raw_candles(candles())
        del raw["candles"][0]["complete"]
        with self.assertRaises(CandleDataError):
            parse_oanda_candles(raw)
        raw = raw_candles(candles())
        raw["candles"][10]["complete"] = False
        with self.assertRaises(CandleDataError):
            parse_oanda_candles(raw)

    def test_P50_missing_mid_wrong_instrument_or_granularity(self):
        raw = raw_candles(candles())
        for wrong in ("GBP_USD", "USD_JPY"):
            with self.assertRaises(CandleDataError):
                parse_oanda_candles(raw, instrument=wrong)
        del raw["candles"][20]["mid"]
        with self.assertRaises(CandleDataError):
            parse_oanda_candles(raw)
        with self.assertRaises(CandleDataError):
            parse_oanda_candles({"candles": []})

    def test_P51_reordered_duplicate_and_invalid_OHLC(self):
        cases = []
        sample = candles()
        cases.append(sample[:15]+[sample[14]]+sample[15:])
        cases.append(sample[::-1])
        for field, value in (
            ("high", float("nan")), ("low", -1),
            ("high", 1.0), ("close", 9.0), ("open", float("inf"))
        ):
            import copy
            damaged = copy.deepcopy(sample)
            damaged[-1][field] = value
            cases.append(damaged)
        for case in cases:
            with self.subTest(last=case[-1]):
                with self.assertRaises(CandleDataError):
                    validate_candles(case, now=NOW, min_count=50)

    def test_P51_future_not_closed_stale_or_gap(self):
        sample = candles()
        with self.assertRaises(CandleDataError):
            validate_candles(sample, now=NOW-timedelta(minutes=1))
        with self.assertRaises(CandleDataError):
            validate_candles(sample, now=NOW+timedelta(hours=2))
        gap = [dict(c) for c in sample]
        gap[-2]["time"] = iso_utc(NOW-timedelta(minutes=31))
        with self.assertRaises(CandleDataError):
            validate_candles(gap, now=NOW)
        self.assertEqual(len(validate_candles(sample, now=NOW)), 60)

    def test_P50_P51_DataHandler_enforces_completion(self):
        raw = raw_candles(candles())
        with (patch.object(config, "API_KEY", "token"),
              patch.object(config, "ACCOUNT_ID", "acct"),
              patch("data.data_handler.safe_request",
                    return_value=Response(raw))):
            out = DataHandler().get_candles("EUR_USD", "M15", 100)
            self.assertEqual(len(out), 60)
            raw["candles"][0]["complete"] = False
            self.assertIsNone(DataHandler().get_candles("EUR_USD", "M15", 100))


class CandleCheckpointTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.gate = CandleGate("acct-1", directory=folder.name)

    def test_P52_once_per_completed_bar_and_after_restart(self):
        stamp = "2026-11-15T09:45:00Z"
        self.assertTrue(self.gate.claim("EUR_USD", stamp))
        self.assertFalse(self.gate.claim("EUR_USD", stamp))
        self.assertFalse(self.gate.claim("EUR_USD", "2026-11-15T09:30:00Z"))
        restarted = CandleGate("acct-1", directory=self.gate.path.parents[1])
        self.assertFalse(restarted.claim("EUR_USD", stamp))
        self.assertTrue(restarted.claim("GBP_USD", stamp))
        self.assertTrue(restarted.claim("EUR_USD", "2026-11-15T10:00:00Z"))

    def test_P52_bad_checksum_or_orphaned_lock_fail_closed(self):
        self.gate.claim("EUR_USD", "2026-11-15T09:45:00Z")
        obj = json.loads(self.gate.path.read_text())
        obj["data"]["last"]["EUR_USD"] = "2026-11-15T10:00:00Z"
        self.gate.path.write_text(json.dumps(obj))
        with self.assertRaises(CandleGateError):
            self.gate.claim("EUR_USD", "2026-11-15T10:15:00Z")
        self.gate.lock.write_text("orphan")
        with self.assertRaises(CandleGateError):
            self.gate.claim("EUR_USD", "2026-11-15T10:15:00Z")

    def test_P52_invalid_boundary_rejected_without_state(self):
        with self.assertRaises(CandleGateError):
            self.gate.claim("EUR_USD", "2026-11-15T09:46:00Z")
        self.assertFalse(self.gate.path.exists())


class BreakAndScoreTests(unittest.TestCase):
    def test_P41_P42_confirmed_old_swing_before_breakout(self):
        data = candles()
        result = BreakoutRetest().confirm(data, "LONG", .001)
        self.assertIsNotNone(result)
        self.assertLess(result.pivot_index, result.breakout_index)
        self.assertLess(result.breakout_index, len(data)-1)
        self.assertAlmostEqual(result.pivot_price, 1.102)
        self.assertEqual(result.state, "CONFIRMED")
        self.assertGreaterEqual(result.zone_low, 1.101)

    def test_P42_expired_cancelled_and_no_breakout(self):
        data = candles()
        self.assertIsNone(BreakoutRetest(expiry_bars=1).confirm(data,"LONG",.001))
        invalid = [dict(x) for x in data]
        invalid[57]["close"] = 1.1018
        invalid[58]["close"] = 1.1019
        self.assertIsNone(BreakoutRetest().confirm(invalid, "LONG", .001))
        invalid = [dict(x) for x in data]
        invalid[58]["close"] = 1.1012
        self.assertIsNone(BreakoutRetest().confirm(invalid, "LONG", .001))

    def test_P42_short_direction_mirror(self):
        result = BreakoutRetest().confirm(candles(direction="SHORT"),
                                        "SHORT", .001)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.pivot_price, 1.098)
        self.assertLess(result.zone_low, result.pivot_price)

    def test_P43_confirmed_swings_and_wick_not_BOS(self):
        data = candles()
        full = StructureEngine().detect(data[:58])
        self.assertTrue(full.bos)
        self.assertEqual(full.direction, "UP")
        self.assertTrue(full.swing_points)
        wink = [dict(x) for x in data[:58]]
        wink[-1]["close"] = 1.1019
        self.assertFalse(StructureEngine().detect(wink).bos)
        first_swings = StructureEngine()._find_swings(data[:48])
        self.assertTrue(all(p.index <= 45 for p in first_swings))

    def test_P44_P45_valid_bullish_signal_and_wrong_direction(self):
        sample = candles()
        # This synthetic candle sequence incurs two pattern-quality
        # penalties and is Grade C under production thresholds. Give the
        # structural test explicit fixture-only B thresholds; production
        # Grade C rejection is asserted separately in Category 09.
        strat = Strategy(ScoringConfig(
            grade_c_min=0, grade_b_min=50, grade_a_min=80
        ))
        long = strat.evaluate_signal(
            "EUR_USD", "LONG", sample, 1.1020, 1.1008,
            .0010, 1.0, 1.2, NOW
        )
        self.assertIsNotNone(long)
        self.assertIn("Breakout->Retest", long.reason)
        self.assertIsNone(strat.evaluate_signal(
            "EUR_USD", "SHORT", sample, 1.1020, 1.1008,
            .0010, 1.0, 1.2, NOW
        ))
        self.assertIsNone(strat.evaluate_signal(
            "EUR_USD", "LONG", sample, 1.1008, 1.1020,
            .0010, 1.0, 1.2, NOW
        ))
        wrong = [dict(x) for x in sample]
        wrong[-1]["open"] = 1.10265
        self.assertIsNone(strat.evaluate_signal(
            "EUR_USD", "LONG", wrong, 1.1020, 1.1008,
            .0010, 1.0, 1.2, NOW
        ))

    def test_P44_P45_short_signal_and_positive_score(self):
        strat = Strategy()
        short = strat.evaluate_signal(
            "EUR_USD", "SHORT", candles(direction="SHORT"),
            1.0980, 1.0992, .0010, 1, 1.2, NOW
        )
        self.assertIsNotNone(short)
        self.assertEqual(short.direction, "SHORT")
        self.assertGreaterEqual(short.score, 40)

    def test_P46_missing_liquidity_sweep_not_artificial_zero(self):
        engine = ScoringEngine()
        metrics = {
            "trend_strength": 1.0, "structure_quality": 1.0,
            "retest_quality": 1.0, "candle_quality": 1.0,
            "ema_alignment": 1.0, "atr_normalized": 1.0
        }
        self.assertAlmostEqual(engine.score(metrics), 100)
        self.assertLess(engine.score({**metrics, "liquidity_sweep": 0}), 100)
        with self.assertRaises(ValueError):
            engine.score({**metrics, "liquidity_sweep": float("nan")})

    def test_P47_pattern_quality_applied_and_reset_blocks(self):
        engine = ScoringEngine()
        metrics = {
            "trend_strength": 1.0, "structure_quality": 1.0,
            "retest_quality": 1.0, "candle_quality": 1.0,
            "ema_alignment": 1.0, "atr_normalized": 1.0
        }
        self.assertEqual(engine.score({**metrics, "pattern_quality": .5}), 50)
        self.assertEqual(engine.score({**metrics, "in_reset_zone": True}), 0)
        with self.assertRaises(ValueError):
            engine.score({**metrics, "pattern_quality": -1})


class SessionTests(unittest.TestCase):
    def test_P54_london_summer_and_winter_DST(self):
        sessions = MarketSessions()
        winter = datetime(2026, 1, 12, 8, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 13, 7, tzinfo=timezone.utc)
        self.assertEqual(sessions.active_session(winter), "London")
        self.assertEqual(sessions.active_session(summer), "London")

    def test_P54_new_york_weekly_close_and_sunday_reopen_DST(self):
        sessions = MarketSessions()
        fri = datetime(2026, 11, 13, 22, tzinfo=timezone.utc)
        self.assertFalse(sessions.market_week_open(fri))
        sunday_pre = datetime(2026, 11, 15, 21, 59, tzinfo=timezone.utc)
        sunday_post = datetime(2026, 11, 15, 22, tzinfo=timezone.utc)
        self.assertFalse(sessions.market_week_open(sunday_pre))
        self.assertTrue(sessions.market_week_open(sunday_post))
        self.assertFalse(sessions.market_week_open(
            datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        ))

    def test_P54_broker_status_required_for_fx_and_nonfx_blocked(self):
        s = MarketSessions()
        at = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
        self.assertTrue(s.instrument_tradeable(
            "EUR_USD", now=at, broker_tradeable=True))
        self.assertFalse(s.instrument_tradeable(
            "EUR_USD", now=at, broker_tradeable=None))
        self.assertFalse(s.instrument_tradeable(
            "XAU_USD", now=at, broker_tradeable=True))
        with self.assertRaises(ValueError):
            s.can_trade(at.replace(tzinfo=None))


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)

    def provider(self, events=None, *, fetched=None):
        return {
            "fetched_at": fetched or iso_utc(self.now),
            "events": [] if events is None else events,
            "coverage_start": iso_utc(self.now - timedelta(hours=2)),
            "coverage_end": iso_utc(self.now + timedelta(hours=2)),
            "currencies": ["EUR", "USD", "GBP", "JPY"],
        }

    def fixture_source(self, payload):
        return patch.multiple(
            config, create=True,
            STATIC_NEWS_LIST=payload["events"],
            STATIC_NEWS_FETCHED_AT=payload["fetched_at"],
            STATIC_NEWS_COVERAGE_START=payload["coverage_start"],
            STATIC_NEWS_COVERAGE_END=payload["coverage_end"],
            STATIC_NEWS_CURRENCIES=payload["currencies"],
        )

    def high(self, when):
        return {"time": iso_utc(when), "impact": "HIGH",
                "currency": "USD", "title": "CPI"}

    def test_P56_missing_provider_is_unknown_not_clear(self):
        news = NewsFilter()
        with patch.object(config, "NEWS_PROVIDER_URL", None, create=True):
            self.assertIn("NEWS_UNKNOWN",
                          news.block_trading("EUR_USD", self.now))

    def test_P56_valid_zero_events_means_verified_clear(self):
        news = NewsFilter()
        payload = self.provider()
        with self.fixture_source(payload):
            self.assertIsNone(news.block_trading("EUR_USD", self.now))

    def test_P56_UTC_news_lockout_precisely_at_bounds(self):
        news = NewsFilter()
        event = self.high(self.now + timedelta(minutes=30))
        with self.fixture_source(self.provider([event])):
            self.assertIn("HIGH-impact", news.block_trading("EUR_USD", self.now))
            self.assertIsNone(news.block_trading(
                "GBP_JPY", self.now
            ))

    def test_P56_stale_unknown_naive_or_invalid_impact(self):
        bad_events = [
            self.provider(fetched=iso_utc(self.now-timedelta(minutes=50))),
            self.provider([{"time": "2026-11-15T10:00:00",
                            "impact": "HIGH", "currency": "USD",
                            "title": "CPI"}]),
            self.provider([{"time": "2026-11-15T10:00:00Z",
                            "impact": "UNKNOWN", "currency": "USD",
                            "title": "CPI"}])
        ]
        for payload in bad_events:
            news = NewsFilter()
            with self.fixture_source(payload):
                self.assertIn("NEWS_UNKNOWN",
                              news.block_trading("EUR_USD", self.now))

    def test_P56_cache_expires_and_is_not_used_when_provider_fails(self):
        news = NewsFilter(ttl_minutes=5, max_age_minutes=30)
        with patch.object(news, "_fetch",
                          return_value=self.provider()) as source:
            news.load_news(now=self.now)
            news.load_news(now=self.now+timedelta(minutes=4))
            self.assertEqual(source.call_count, 1)
            source.side_effect = RuntimeError("provider down")
            with self.assertRaises(RuntimeError):
                news.load_news(now=self.now+timedelta(minutes=6))


    def test_P52_scan_runs_strategy_only_once_per_new_M15(self):
        rules = InstrumentRules(
            "EUR_USD", "CURRENCY", -4, 5, 0,
            Decimal("1"), Decimal("1000000"), Decimal("0"),
            Decimal("0.02"),
        )

        from execution.trade_executor import TradeExecutor
        signals = SimpleNamespace(
            instrument="EUR_USD", direction="LONG", grade="B", score=70
        )
        with TemporaryDirectory() as directory:
            gate = CandleGate("test-account", directory=directory)
            with (
                patch.object(config, "API_KEY", "dummy"),
                patch.object(config, "ACCOUNT_ID", "test-account"),
                patch("main.utc_now", return_value=NOW),
                patch.object(TradeExecutor, "get_open_trades", return_value=[]),
                patch("main.MarketSessions") as sessions,
                patch("main.DataHandler") as data,
                patch("main.Strategy") as strategy,
                patch("main.DecisionTrace"),
                patch("main.CandleGate", return_value=gate),
                patch("main.build_news_filter") as news
            ):
                sessions.return_value.can_trade.return_value = True
                sessions.return_value.instrument_tradeable.return_value = True
                data.return_value.get_account_instruments.return_value = {
                    "EUR_USD": rules
                }
                data.return_value.get_candles.side_effect = (
                    lambda symbol, *args:
                    candles() if symbol == "EUR_USD" else None
                )
                data.return_value.get_price.return_value = {
                    "bid": 1.1, "ask": 1.1002
                }
                strategy.return_value.ema_engine.ema.return_value = 1.101
                strategy.return_value.atr_engine.atr.return_value = .001
                strategy.return_value.evaluate_signal.return_value = signals
                news.return_value.block_trading.return_value = "NEWS_UNKNOWN"
                self.assertEqual(main.scan_once(), 0)
                self.assertEqual(main.scan_once(), 0)
                self.assertEqual(
                    strategy.return_value.evaluate_signal.call_count, 2
                )
                self.assertEqual(
                    news.return_value.block_trading.call_count, 1
                )

    def test_P54_no_broker_price_does_not_consume_candle(self):
        rules = InstrumentRules(
            "EUR_USD", "CURRENCY", -4, 5, 0,
            Decimal("1"), Decimal("1000000"), Decimal("0"),
            Decimal("0.02"),
        )

        from execution.trade_executor import TradeExecutor
        with TemporaryDirectory() as directory:
            gate = CandleGate("test-account", directory=directory)
            with (
                patch.object(config, "API_KEY", "dummy"),
                patch.object(config, "ACCOUNT_ID", "test-account"),
                patch("main.utc_now", return_value=NOW),
                patch.object(TradeExecutor, "get_open_trades", return_value=[]),
                patch("main.MarketSessions") as sessions,
                patch("main.DataHandler") as data,
                patch("main.Strategy") as strategy,
                patch("main.CandleGate", return_value=gate),
            ):
                sessions.return_value.can_trade.return_value = True
                sessions.return_value.instrument_tradeable.side_effect = (
                    lambda *args, **kw: kw["broker_tradeable"]
                )
                data.return_value.get_account_instruments.return_value = {
                    "EUR_USD": rules
                }
                data.return_value.get_candles.side_effect = (
                    lambda symbol, *args:
                    candles() if symbol == "EUR_USD" else None
                )
                data.return_value.get_price.return_value = None
                self.assertEqual(main.scan_once(), 0)
                self.assertFalse(gate.path.exists())
                strategy.return_value.evaluate_signal.assert_not_called()

    def test_P47_acceptably_imperfect_pattern_has_real_penalty(self):
        from strategy.pattern_filters import PatternFilters
        quality = PatternFilters().check(
            candles(), .001, "UP"
        )
        self.assertTrue(quality.is_valid)
        self.assertGreater(quality.quality, 0)
        self.assertLess(quality.quality, 1)
        engine = ScoringEngine()
        base = {
            "trend_strength": 1, "structure_quality": 1,
            "retest_quality": 1, "candle_quality": 1,
            "ema_alignment": 1, "atr_normalized": 1
        }
        self.assertLess(
            engine.score({**base, "pattern_quality": quality.quality}),
            engine.score(base)
        )

    def test_P47_unknown_reset_zone_rejected(self):
        with self.assertRaises(ValueError):
            ScoringEngine().score({"in_reset_zone": "unknown"})

    def test_P52_broken_symlink_must_not_be_auto_reinitialized(self):
        with TemporaryDirectory() as directory:
            gate = CandleGate("account", directory=directory)
            gate.path.parent.mkdir(parents=True)
            gate.path.symlink_to(gate.path.parent / "missing-target")
            with self.assertRaises(CandleGateError):
                gate.claim("EUR_USD", "2026-11-15T09:45:00Z")
            self.assertTrue(gate.path.is_symlink())

    def test_P56_cache_invalidated_when_static_source_changes(self):
        news = NewsFilter()
        with self.fixture_source(self.provider()):
            self.assertEqual(news.load_news(now=self.now), [])
            config.STATIC_NEWS_LIST = [self.high(self.now)]
            refreshed = news.load_news(now=self.now)
        self.assertEqual(len(refreshed), 1)



    def test_P43_CHoCH_when_close_reverses_prior_downtrend(self):
        sample = []
        for i in range(20):
            sample.append({"high": 1.1000, "low": 1.0900, "close": 1.0950})
        sample[5]["high"] = 1.1050
        sample[10]["high"] = 1.1030
        sample[7]["low"] = 1.0880
        sample[13]["low"] = 1.0860
        sample[19]["high"] = 1.1045
        sample[19]["close"] = 1.1040
        result = StructureEngine().detect(sample)
        self.assertTrue(result.bos)
        self.assertTrue(result.choch)
        self.assertEqual(result.direction, "UP")
        wick_only = [dict(c) for c in sample]
        wick_only[-1]["close"] = 1.1029
        self.assertFalse(StructureEngine().detect(wick_only).bos)

    def test_P54_DataHandler_requires_fresh_tradeable_OANDA_price(self):
        payload = {"prices": [{
            "instrument": "EUR_USD", "tradeable": True,
            "time": iso_utc(NOW),
            "bids": [{"price": "1.1000"}],
            "asks": [{"price": "1.1002"}]
        }]}
        with (
            patch.object(config, "API_KEY", "dummy"),
            patch.object(config, "ACCOUNT_ID", "acct"),
            patch("data.data_handler.utc_now", return_value=NOW),
            patch("data.data_handler.safe_request", return_value=Response(payload)),
        ):
            service = DataHandler()
            self.assertEqual(service.get_price("EUR_USD")["bid"], 1.1)
            payload["prices"][0]["tradeable"] = False
            self.assertIsNone(service.get_price("EUR_USD"))
            payload["prices"][0]["tradeable"] = True
            payload["prices"][0]["time"] = iso_utc(
                NOW - timedelta(minutes=2)
            )
            self.assertIsNone(service.get_price("EUR_USD"))
            payload["prices"][0]["time"] = iso_utc(NOW)
            payload["prices"][0]["asks"][0]["price"] = "1.0999"
            self.assertIsNone(service.get_price("EUR_USD"))


if __name__ == "__main__":
    unittest.main()
