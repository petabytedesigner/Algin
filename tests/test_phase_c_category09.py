"""Phase C / Category 09: strategy determinism, BOS identity and grade policy.

Synthetic candles only. No OANDA I/O or broker writes. Reuse Category 5's
known breakout and retest fixtures without changing the trading policy.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from math import isfinite
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import json
import random
import unittest

import config
from data.market_sessions import MarketSessions
from execution.trade_coordinator import stable_signal_id
from storage.intent_journal import IntentJournal, JournalError
from utils.time_tools import iso_utc, parse_utc
from risk.scoring_engine import ScoringConfig, ScoringEngine
from strategy.breakout_retest import BreakoutRetest
from strategy.strategy import Strategy
from strategy.structure_engine import StructureEngine
from test_category5 import candles, NOW


class BreakoutIdentityTests(unittest.TestCase):
    def test_default_expiry_uses_single_config_source(self):
        with patch.object(config, "MAX_RETEST_BARS", 4):
            self.assertEqual(BreakoutRetest().expiry_bars, 4)
            self.assertEqual(Strategy().breakout_retest.expiry_bars, 4)
        with patch.object(config, "MAX_RETEST_BARS", 1):
            self.assertIsNone(
                BreakoutRetest().confirm(candles(), "LONG", .001)
            )
            strat = Strategy()
            self.assertEqual(strat.breakout_retest.expiry_bars, 1)
            self.assertIsNone(strat.evaluate_signal(
                "EUR_USD", "LONG", candles(), 1.1020, 1.1008,
                .001, 1, 1.2, NOW,
            ))
            self.assertEqual(strat.last_rejection_reason,
                             "BREAKOUT_RETEST_MISSING")
        with patch.object(config, "MAX_RETEST_BARS", 2):
            self.assertIsNotNone(BreakoutRetest().confirm(
                candles(), "LONG", .001
            ))

    def test_invalid_config_does_not_become_hidden_fallback(self):
        for value in (True, False, 0, -1, 13, "4", 2.5):
            with self.subTest(value=value):
                with patch.object(config, "MAX_RETEST_BARS", value):
                    with self.assertRaises(ValueError):
                        Strategy()

    def test_long_short_breakout_pivot_bos_identities_match(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                series = candles(direction=direction)
                sequence = BreakoutRetest().confirm(series, direction, .001)
                self.assertIsNotNone(sequence)
                structure = StructureEngine().detect_breakout(
                    series, breakout_index=sequence.breakout_index,
                    pivot_index=sequence.pivot_index,
                    pivot_price=sequence.pivot_price,
                    direction=direction,
                )
                self.assertTrue(structure.bos)
                self.assertEqual(
                    structure.direction,
                    "UP" if direction == "LONG" else "DOWN",
                )
                side = [
                    p for p in structure.swing_points
                    if p.is_high == (direction == "LONG")
                ]
                self.assertEqual(side[-1].index, sequence.pivot_index)
                self.assertEqual(side[-1].price, sequence.pivot_price)

    def test_wrong_pivot_index_or_price_never_certifies_bos(self):
        series = candles()
        seq = BreakoutRetest().confirm(series, "LONG", .001)
        engine = StructureEngine()
        variants = (
            (seq.pivot_index - 1, seq.pivot_price),
            (seq.pivot_index, seq.pivot_price + .0002),
            (seq.breakout_index - 2, seq.pivot_price),
        )
        for idx, price in variants:
            with self.subTest(pivot_index=idx, pivot_price=price):
                result = engine.detect_breakout(
                    series, breakout_index=seq.breakout_index,
                    pivot_index=idx, pivot_price=price,
                    direction="LONG",
                )
                self.assertFalse(result.bos)

    def test_suffix_future_candles_cannot_change_confirmed_breakout(self):
        series = candles()
        seq = BreakoutRetest().confirm(series, "LONG", .001)
        engine = StructureEngine()
        before = engine.detect_breakout(
            series, breakout_index=seq.breakout_index,
            pivot_index=seq.pivot_index,
            pivot_price=seq.pivot_price, direction="LONG",
        )
        changed = [dict(bar) for bar in series]
        for i in range(seq.breakout_index + 1, len(changed)):
            changed[i].update(high=1.500, low=.500, open=1.0, close=1.2)
        after = engine.detect_breakout(
            changed, breakout_index=seq.breakout_index,
            pivot_index=seq.pivot_index,
            pivot_price=seq.pivot_price, direction="LONG",
        )
        self.assertEqual(before, after)

    def test_first_long_confirmed_retest_consumes_breakout(self):
        series = [dict(bar) for bar in candles()]
        # The penultimate candle now qualifies as the FIRST confirmation.
        series[-2].update(open=1.1022, low=1.10215,
                          high=1.1027, close=1.1025)
        first = BreakoutRetest().confirm(series[:-1], "LONG", .001)
        self.assertIsNotNone(first)
        self.assertEqual(first.breakout_index, 57)
        # A second otherwise-valid retest of the SAME breakout is rejected.
        second = BreakoutRetest().confirm(series, "LONG", .001)
        self.assertIsNone(second)

    def test_first_short_confirmed_retest_consumes_breakout(self):
        series = [dict(bar) for bar in candles(direction="SHORT")]
        series[-2].update(open=1.0978, high=1.09788,
                          low=1.0973, close=1.0975)
        self.assertIsNotNone(
            BreakoutRetest().confirm(series[:-1], "SHORT", .001)
        )
        self.assertIsNone(
            BreakoutRetest().confirm(series, "SHORT", .001)
        )

    def test_randomized_unrelated_candles_after_bos_do_not_change_bos(self):
        gen = random.Random(20261117)
        original = candles()
        seq = BreakoutRetest().confirm(original, "LONG", .001)
        engine = StructureEngine()
        baseline = engine.detect_breakout(
            original, breakout_index=seq.breakout_index,
            pivot_index=seq.pivot_index, pivot_price=seq.pivot_price,
            direction="LONG",
        )
        for _ in range(100):
            altered = [dict(bar) for bar in original]
            for i in range(seq.breakout_index + 1, len(altered)):
                x = gen.uniform(.85, 1.30)
                altered[i].update(
                    open=x, close=x, high=x + .01, low=x - .01,
                )
            observed = engine.detect_breakout(
                altered, breakout_index=seq.breakout_index,
                pivot_index=seq.pivot_index, pivot_price=seq.pivot_price,
                direction="LONG",
            )
            self.assertEqual(observed, baseline)


class DurableSetupIdentityTests(unittest.TestCase):
    def test_setup_id_is_stable_when_rolling_window_changes_offsets(self):
        original = candles()
        time0 = parse_utc(original[0]["time"])
        extra = []
        for i in range(8, 0, -1):
            extra.append({
                "time": iso_utc(time0 - timedelta(minutes=15*i)),
                "complete": True, "open": 1.1000,
                "high": 1.1010, "low": 1.0990, "close": 1.1001,
            })
        config_b = ScoringConfig(
            grade_c_min=0, grade_b_min=50, grade_a_min=80
        )
        engine = Strategy(scoring_config=config_b)
        a = engine.evaluate_signal(
            "EUR_USD", "LONG", original, 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        )
        b = engine.evaluate_signal(
            "EUR_USD", "LONG", extra + original, 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        )
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.setup_id, b.setup_id)
        self.assertTrue(a.setup_id.startswith("algin-setup-"))
        self.assertEqual(len(a.setup_id), 52)

    def test_same_setup_on_later_candle_is_one_durable_signal(self):
        setup = "algin-setup-" + "a" * 40
        early = NOW - timedelta(minutes=30)
        later = NOW - timedelta(minutes=15)
        first = stable_signal_id(
            "acct-1", "EUR_USD", "LONG", early, setup_id=setup
        )
        second = stable_signal_id(
            "acct-1", "EUR_USD", "LONG", later, setup_id=setup
        )
        self.assertEqual(first, second)
        with TemporaryDirectory() as root:
            journal = IntentJournal("acct-1", directory=root)
            journal.begin(
                signal_id=first, attempt_id="algin-first",
                request={"order": {
                    "instrument": "EUR_USD", "units": "10"
                }}, risk_pct="0.5", margin_home="5", grade="B",
            )
            with self.assertRaises(JournalError):
                journal.begin(
                    signal_id=second, attempt_id="algin-second",
                    request={"order": {
                        "instrument": "EUR_USD", "units": "10"
                    }}, risk_pct="0.5", margin_home="5", grade="B",
                )

    def test_legacy_candle_identity_hash_remains_unmodified(self):
        candle = NOW - timedelta(minutes=15)
        original = json.dumps(
            ("acct-1", "EUR_USD", "LONG", "M15", iso_utc(candle)),
            separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        expected = (
            "algin-signal-" + hashlib.sha256(original).hexdigest()[:40]
        )
        self.assertEqual(stable_signal_id(
            "acct-1", "EUR_USD", "LONG", candle,
        ), expected)
        self.assertNotEqual(stable_signal_id(
            "acct-1", "EUR_USD", "LONG", candle,
        ), stable_signal_id(
            "acct-1", "EUR_USD", "LONG", candle + timedelta(minutes=15),
        ))

    def test_unverified_setup_identity_is_refused(self):
        for identity in ("other", "algin-setup-" + "z"*40, 123, ""):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    stable_signal_id(
                        "acct-1", "EUR_USD", "LONG",
                        NOW - timedelta(minutes=15),
                        setup_id=identity,
                    )

    def test_coordinator_consumes_strict_setup_id_from_strategy_signal(self):
        from test_phase_b_category05 import CoordinatorTests
        fixture = CoordinatorTests(
            "test_preview_signal_filters_reconciled_risk_tradeplan_no_post"
        )
        fixture.setUp()
        try:
            setup = "algin-setup-" + "b"*40
            req = fixture.request(signal=replace(
                fixture.signal, setup_id=setup
            ))
            result = fixture.coordinator.preview(req)
            self.assertEqual(result.status, "PREVIEW")
            self.assertEqual(result.signal_id, stable_signal_id(
                "acct-1", "EUR_USD", "LONG", req.candle_time,
                setup_id=setup,
            ))
        finally:
            fixture.doCleanups()


class ScoringAndGradeTests(unittest.TestCase):
    METRICS = {
        "trend_strength": .8, "structure_quality": .8,
        "retest_quality": .8, "candle_quality": .8,
        "ema_alignment": .8, "atr_normalized": .8,
        "pattern_quality": 1.0,
    }

    def test_required_metric_absence_is_not_implicitly_zero(self):
        engine = ScoringEngine()
        for key in (
            "trend_strength", "structure_quality",
            "retest_quality", "candle_quality",
            "ema_alignment", "atr_normalized",
        ):
            with self.subTest(metric=key):
                reduced = dict(self.METRICS)
                del reduced[key]
                with self.assertRaises(ValueError):
                    engine.score(reduced)

    def test_missing_optional_liquidity_sweep_is_not_scored_as_failure(self):
        engine = ScoringEngine()
        self.assertAlmostEqual(engine.score(self.METRICS), 80)
        self.assertAlmostEqual(
            engine.score({**self.METRICS, "liquidity_sweep": 0.0}),
            80 * 6 / 7,
        )

    def test_grade_boundaries_are_explicit_and_monotonic(self):
        grade = ScoringEngine().grade_from_score
        self.assertEqual(grade(100.0), "A")
        self.assertEqual(grade(80.0), "A")
        self.assertEqual(grade(79.999), "B")
        self.assertEqual(grade(60.0), "B")
        self.assertEqual(grade(59.999), "C")
        self.assertEqual(grade(40.0), "C")
        self.assertEqual(grade(39.999), "D")

    def test_strategy_never_emits_tradeable_grade_c(self):
        config_for_c = ScoringConfig(
            grade_c_min=0, grade_b_min=99.9, grade_a_min=100,
        )
        engine = Strategy(scoring_config=config_for_c)
        result = engine.evaluate_signal(
            "EUR_USD", "LONG", candles(), 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        )
        self.assertIsNone(result)
        self.assertEqual(engine.last_rejection_reason,
                         "GRADE_NOT_TRADEABLE")

    def test_original_pattern_penalized_fixture_is_grade_c_blocked(self):
        engine = Strategy()
        self.assertIsNone(engine.evaluate_signal(
            "EUR_USD", "LONG", candles(), 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        ))
        self.assertEqual(engine.last_rejection_reason, "GRADE_NOT_TRADEABLE")

    def test_valid_b_fixture_is_tradeable_and_deterministic(self):
        # Make only the synthetic fixture's score policy permissive enough
        # to isolate BOS/retest determinism; production remains 80/60/40.
        engine = Strategy(scoring_config=ScoringConfig(
            grade_c_min=0, grade_b_min=50, grade_a_min=80,
        ))
        first = engine.evaluate_signal(
            "EUR_USD", "LONG", candles(), 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        )
        second = engine.evaluate_signal(
            "EUR_USD", "LONG", candles(), 1.1020, 1.1008,
            .001, 1.0, 1.2, NOW,
        )
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertIn(first.grade, ("A", "B"))
        self.assertTrue(isfinite(first.score))


class SessionConsistencyTests(unittest.TestCase):
    def test_disabled_intraday_hours_never_disable_weekly_market_lock(self):
        sessions = MarketSessions()
        tuesday_off_hours = datetime(
            2026, 11, 17, 22, tzinfo=timezone.utc
        )
        self.assertTrue(sessions.market_week_open(tuesday_off_hours))
        self.assertFalse(sessions.can_trade(tuesday_off_hours))
        self.assertFalse(sessions.instrument_tradeable(
            "EUR_USD", now=tuesday_off_hours, broker_tradeable=True,
            require_session=True,
        ))
        self.assertTrue(sessions.instrument_tradeable(
            "EUR_USD", now=tuesday_off_hours, broker_tradeable=True,
            require_session=False,
        ))
        friday_closed = datetime(2026, 11, 13, 22, tzinfo=timezone.utc)
        self.assertFalse(sessions.instrument_tradeable(
            "EUR_USD", now=friday_closed, broker_tradeable=True,
            require_session=False,
        ))

    def test_disabled_session_still_requires_verified_quote_and_fx(self):
        now = datetime(2026, 11, 17, 22, tzinfo=timezone.utc)
        for broker_flag in (False, None, 1, "true"):
            with self.subTest(flag=broker_flag):
                self.assertFalse(MarketSessions().instrument_tradeable(
                    "EUR_USD", now=now,
                    broker_tradeable=broker_flag, require_session=False,
                ))
        self.assertFalse(MarketSessions().instrument_tradeable(
            "XAU_USD", now=now,
            broker_tradeable=True, require_session=False,
        ))
        self.assertFalse(MarketSessions().instrument_tradeable(
            "EUR_EUR", now=now,
            broker_tradeable=True, require_session=False,
        ))
        self.assertFalse(MarketSessions().instrument_tradeable(
            "EUR_USD", now=now,
            broker_tradeable=True, require_session="false",
        ))


if __name__ == "__main__":
    unittest.main()
