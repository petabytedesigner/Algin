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
        out[55].update(open=1.1000, close=1.1001)
        out[56].update(open=1.1000, close=1.1003)
        out[57].update(open=1.1003, low=1.1002,
                       high=1.1028, close=1.1026)
        out[58].update(open=1.1020, low=1.10195,
                       high=1.1027, close=1.1025)
        out[59].update(open=1.10215, low=1.10212,
                       high=1.1027, close=1.1026)
    else:
        out[30]["low"] = 1.0984
        out[47]["low"] = 1.0980
        out[55].update(open=1.1000, close=1.0999)
        out[56].update(open=1.1000, close=1.0997)
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
        strat = Strategy()
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
        return {"fetched_at": fetched or iso_utc(self.now),
                "events": [] if events is None else events}

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
        with patch.object(config, "STATIC_NEWS_LIST", payload["events"], create=True), (
            patch.object(config, "STATIC_NEWS_FETCHED_AT",
                         payload["fetched_at"], create=True)
        ):
            self.assertIsNone(news.block_trading("EUR_USD", self.now))

    def test_P56_UTC_news_lockout_precisely_at_bounds(self):
        news = NewsFilter()
        event = self.high(self.now + timedelta(minutes=30))
        with (
            patch.object(config, "STATIC_NEWS_LIST", [event], create=True),
            patch.object(config, "STATIC_NEWS_FETCHED_AT",
                         iso_utc(self.now), create=True)
        ):
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
            with (
                patch.object(config, "STATIC_NEWS_LIST",
                             payload["events"], create=True),
                patch.object(config, "STATIC_NEWS_FETCHED_AT",
                             payload["fetched_at"], create=True)
            ):
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


if __name__ == "__main__":
    unittest.main()
