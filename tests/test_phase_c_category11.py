"""Category 11 offline replay: no external feed, broker credentials or writes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
import unittest

import config
from analytics.strategy_replay import (
    ReplayAssumptions, ReplayError, replay,
)
from analytics.walk_forward import (
    holdout, cost_sensitivity,
)
from broker.practice_transport import _PRACTICE_WRITES_RELEASED
from risk.trade_plan import InstrumentRules
from strategy.strategy import Strategy, StrategySignal
from test_category5 import candles
from utils.time_tools import iso_utc, parse_utc


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
D = Decimal


def rules():
    return InstrumentRules(
        "EUR_USD", "CURRENCY", -4, 5, 0,
        D("1"), D("1000000"), D("0"), D("0.02"),
    )


def settings(*, management="HOLD", commission="0",
             entry_slip="0", exit_slip="0", financing="0"):
    return ReplayAssumptions(
        starting_equity=D("10000"), account_currency="USD",
        entry_slippage_pips=D(entry_slip),
        exit_slippage_pips=D(exit_slip),
        commission_home_per_unit_side=D(commission),
        financing_home_per_unit_bar=D(financing),
        management=management,
    )


def make_fixture(*, exit_case="TP", total=65):
    """M/B/A synthetic 15-min candles; no request to OANDA.

    A known bullish breakout is in bars 57-59. A SCRIPTED signal is injected
    only for execution mechanics, not to claim Strategy profitability.
    """
    mid = candles(now=NOW, total=60)
    for i in range(total - len(mid)):
        t = NOW + timedelta(minutes=15*i)
        sample = {
            "time": iso_utc(t), "complete": True,
            "open": 1.1026, "high": 1.1032,
            "low": 1.1023, "close": 1.1028,
        }
        mid.append(sample)
    if exit_case == "TP":
        mid[60].update(open=1.1026, high=1.115,
                       low=1.1024, close=1.114)
    elif exit_case == "BOTH":
        mid[60].update(open=1.1026, high=1.115,
                       low=1.0940, close=1.104)
    elif exit_case == "OPEN":
        for i in range(60, total):
            mid[i].update(open=1.1026, high=1.1030,
                          low=1.1024, close=1.1027)
    elif exit_case == "BE":
        mid[60].update(open=1.1026, high=1.1053,
                       low=1.1024, close=1.1050)
        mid[61].update(open=1.1050, high=1.1053,
                       low=1.1024, close=1.1034)
        mid[62].update(open=1.1034, high=1.1035,
                       low=1.0970, close=1.0980)
    else:
        raise ValueError("Unsupported case")
    records = []
    evidence = {}
    for bar in mid:
        def side(offset):
            # Every synthetic side is valid and aligned to 5 digits.
            return {
                k: format(D(str(bar[name])) + offset, ".5f")
                for k, name in (
                    ("o", "open"), ("h", "high"),
                    ("l", "low"), ("c", "close"),
                )
            }
        t = parse_utc(bar["time"])
        records.append({
            "time": bar["time"], "complete": True,
            "mid": side(D("0")),
            "bid": side(D("-0.00010")),
            "ask": side(D("0.00010")),
        })
        at = t + timedelta(minutes=15)
        evidence[bar["time"]] = {
            "broker_tradeable": True,
            "loss_to_home": "1", "gain_to_home": "1",
            "base_value_to_home": "1.1",
            "calendar": {
                "events": [], "fetched_at": iso_utc(at),
                "coverage_start": iso_utc(at-timedelta(hours=1)),
                "coverage_end": iso_utc(at+timedelta(hours=1)),
                "currencies": ["EUR", "USD"],
            },
        }
    return {
        "instrument": "EUR_USD", "granularity": "M15",
        "candles": records,
    }, evidence


class ScriptedStrategy(Strategy):
    """Exact production indicator engines, fixture-only signal injection."""
    seen = []
    def evaluate_signal(self, instrument, direction, history,
                        ema20, ema50, atr, zone_low, zone_high, timestamp):
        self.__class__.seen.append((
            iso_utc(timestamp), len(history), direction,
            history[-1]["time"],
        ))
        if (len(history) != 60 or direction != "LONG"
            or timestamp != NOW):
            return None
        return StrategySignal(
            instrument="EUR_USD", direction="LONG",
            score=85.0, grade="A", metrics={}, reason="SYNTHETIC_TEST_ONLY",
            timestamp=timestamp, setup_id="algin-setup-" + "a"*40,
        )


class InputAndSafetyTests(unittest.TestCase):
    def test_accepts_full_usable_ohlc_without_laundering_provenance(self):
        raw, facts = make_fixture()
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            result = replay(raw, rules(), facts, assumptions=settings())
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.source_kind, "SYNTHETIC")
        self.assertFalse(result.broker_verified)
        self.assertEqual(result.trades[0].opened_at, NOW)
        self.assertEqual(result.trades[0].signal_time, NOW)

    def test_actual_strategy_is_invoked_without_test_override(self):
        raw, facts = make_fixture()
        # Production strategy is used here: synthetic patterns do not
        # constitute historical evidence or a fabricated win-rate.
        result = replay(raw, rules(), facts, assumptions=settings())
        self.assertGreater(result.evaluated_bars, 0)
        self.assertFalse(result.broker_verified)

    def test_missing_bid_ask_or_incomplete_bar_never_falls_back_to_mid(self):
        for change in ("bid", "ask", "complete"):
            raw, facts = make_fixture()
            if change == "complete":
                raw["candles"][40]["complete"] = False
            else:
                del raw["candles"][40][change]
            with self.subTest(change=change), self.assertRaises(ReplayError):
                replay(raw, rules(), facts, assumptions=settings())

    def test_reordered_duplicate_and_wrong_instrument_history_refused(self):
        for change in ("duplicate", "instrument"):
            raw, facts = make_fixture()
            if change == "duplicate":
                raw["candles"][21]["time"] = raw["candles"][20]["time"]
            else:
                raw["instrument"] = "GBP_USD"
            with self.subTest(change=change), self.assertRaises(ReplayError):
                replay(raw, rules(), facts, assumptions=settings())

    def test_bad_precision_and_crossed_executable_side_rejected(self):
        for bad in ("1.100123", "0", "NaN"):
            raw, facts = make_fixture()
            raw["candles"][32]["ask"]["o"] = bad
            with self.subTest(ask=bad), self.assertRaises(ReplayError):
                replay(raw, rules(), facts, assumptions=settings())

    def test_absent_factors_and_tradeability_cannot_be_assumed(self):
        raw, facts = make_fixture()
        del facts[raw["candles"][20]["time"]]["loss_to_home"]
        with self.assertRaises(ReplayError):
            replay(raw, rules(), facts, assumptions=settings())

    def test_unconfigured_calendar_produces_zero_synthetic_entries(self):
        raw, facts = make_fixture()
        for entry in facts.values():
            entry["calendar"] = None
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            result = replay(raw, rules(), facts, assumptions=settings())
        self.assertEqual(result.trades, ())
        self.assertGreater(result.blocked.get("NEWS_UNKNOWN_OR_LOCKOUT", 0), 0)

    def test_an_injected_real_transport_is_not_instantiated(self):
        raw, facts = make_fixture()
        with (patch("analytics.strategy_replay.Strategy", ScriptedStrategy),
              patch("requests.get", side_effect=AssertionError("HTTP forbidden")),
              patch("requests.post", side_effect=AssertionError("POST forbidden"))):
            self.assertFalse(_PRACTICE_WRITES_RELEASED)
            self.assertFalse(config.ORDER_WRITES_ENABLED)
            out = replay(raw, rules(), facts, assumptions=settings())
        self.assertEqual(len(out.trades), 1)

    def test_invalid_cost_model_does_not_exceed_entry_price_bound(self):
        for slip in ("-1", "NaN", str(config.SLIPPAGE_ALLOWED+1)):
            with self.subTest(slip=slip), self.assertRaises((ReplayError, ValueError)):
                settings(entry_slip=slip)

    def test_no_uncertain_conversion_silently_becomes_one(self):
        raw, facts = make_fixture()
        facts[raw["candles"][60]["time"]]["gain_to_home"] = "NaN"
        with self.assertRaises(ReplayError):
            replay(raw, rules(), facts, assumptions=settings())


class FillAndManagementTests(unittest.TestCase):
    def go(self, case="TP", **overrides):
        raw, facts = make_fixture(exit_case=case)
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            result = replay(raw, rules(), facts, assumptions=settings(**overrides))
        return result

    def test_next_open_no_lookahead_and_bid_ask_side_on_tp(self):
        result = self.go()
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.close_reason, "TP")
        self.assertEqual(trade.entry, D("1.10270"))
        self.assertEqual(trade.closed_at, NOW + timedelta(minutes=15))
        self.assertGreater(trade.net_home, 0)
        self.assertEqual(trade.gross_home, trade.net_home)

    def test_same_bar_tp_and_sl_always_book_stop_first(self):
        t = self.go("BOTH").trades[0]
        self.assertEqual(t.close_reason, "SL")
        self.assertLess(t.net_home, 0)

    def test_spread_is_in_execution_prices_not_reduced_twice(self):
        t = self.go("TP").trades[0]
        self.assertEqual(t.costs_home, 0)
        self.assertEqual(
            t.gross_home, (t.exit - t.entry) * t.initial_units,
        )

    def test_commission_and_slippage_reduce_realized_net(self):
        base = self.go("TP").trades[0]
        expensive = self.go(
            "TP", commission=".00001", entry_slip=".2",
            exit_slip=".4",
        ).trades[0]
        self.assertGreater(expensive.costs_home, 0)
        self.assertLess(expensive.net_home, base.net_home)
        self.assertLessEqual(expensive.initial_units, base.initial_units)
        self.assertGreater(expensive.initial_units, 0)

    def test_pending_position_at_end_is_never_forced_profit(self):
        result = self.go("OPEN")
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.unresolved, 1)

    def test_be_and_partial_reproduce_production_one_r_triggers(self):
        held = self.go("BE", management="HOLD")
        managed = self.go("BE", management="BE_PARTIAL")
        self.assertEqual(len(held.trades), 1)
        self.assertEqual(len(managed.trades), 1)
        trade = managed.trades[0]
        self.assertTrue(trade.partial_executed)
        self.assertTrue(trade.be_executed)
        self.assertEqual(trade.close_reason, "SL")
        self.assertGreater(trade.net_home, held.trades[0].net_home)

    def test_no_be_trigger_from_intrabar_high_alone(self):
        raw, facts = make_fixture(exit_case="OPEN")
        raw["candles"][60]["bid"]["h"] = "1.15000"
        raw["candles"][60]["ask"]["h"] = "1.15020"
        raw["candles"][60]["mid"]["h"] = "1.15010"
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            result = replay(
                raw, rules(), facts, assumptions=settings(management="BE_PARTIAL")
            )
        # The position may TP on high, but it must NOT have had BE/partial
        # activated by that same bar's high before SL/TP checks.
        self.assertEqual(len(result.trades), 1)
        self.assertFalse(result.trades[0].be_executed)
        self.assertFalse(result.trades[0].partial_executed)

    def test_data_after_signal_cannot_change_earlier_strategy_prefix(self):
        ScriptedStrategy.seen = []
        raw, facts = make_fixture()
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            replay(raw, rules(), facts, assumptions=settings())
        prefix = [x for x in ScriptedStrategy.seen if x[1] == 60]
        self.assertEqual(len(prefix), 2)
        self.assertEqual(prefix[0][3], iso_utc(NOW - timedelta(minutes=15)))
        altered = deepcopy(raw)
        altered["candles"][-1]["mid"].update(
            {"o": "1.50000", "h": "1.50100",
             "l": "1.49900", "c": "1.50050"}
        )
        altered["candles"][-1]["bid"].update(
            {"o": "1.49990", "h": "1.50090",
             "l": "1.49890", "c": "1.50040"}
        )
        altered["candles"][-1]["ask"].update(
            {"o": "1.50010", "h": "1.50110",
             "l": "1.49910", "c": "1.50060"}
        )
        ScriptedStrategy.seen = []
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            replay(altered, rules(), facts, assumptions=settings())
        self.assertEqual(prefix, [x for x in ScriptedStrategy.seen if x[1] == 60])


class StatisticalSafeguardTests(unittest.TestCase):
    def sample(self):
        raw, facts = make_fixture(exit_case="TP")
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            return raw, facts, replay(
                raw, rules(), facts, assumptions=settings()
            )

    def test_holdout_reports_insufficient_evidence_and_no_edge_claim(self):
        _, _, result = self.sample()
        cut = NOW - timedelta(minutes=15)
        out = holdout(result, starting_equity=D("10000"), holdout_start=cut)
        self.assertEqual(out.out_of_sample["count"], 1)
        self.assertEqual(
            out.out_of_sample["sample_sufficiency"], "INSUFFICIENT_SAMPLE"
        )
        self.assertIsNone(out.out_of_sample["win_rate_wilson_95_pct"])
        self.assertFalse(out.out_of_sample["statistically_validated_edge"])
        self.assertFalse(out.broker_verified)

    def test_cut_trade_is_purged_not_in_train_or_holdout(self):
        _, _, result = self.sample()
        cut = NOW + timedelta(minutes=5)
        out = holdout(result, starting_equity=D("10000"), holdout_start=cut)
        self.assertEqual(out.purged_cross_boundary, 1)
        self.assertEqual(out.in_sample["count"], 0)
        self.assertEqual(out.out_of_sample["count"], 0)

    def test_grade_and_instrument_breakdown_keep_same_source(self):
        _, _, result = self.sample()
        report = holdout(
            result, starting_equity="10000",
            holdout_start=NOW-timedelta(minutes=1),
        )
        self.assertEqual(report.by_grade["A"]["out_of_sample"]["count"], 1)
        self.assertEqual(report.by_grade["B"]["out_of_sample"]["count"], 0)
        self.assertEqual(
            report.by_instrument["EUR_USD"]["out_of_sample"]["count"], 1
        )
        self.assertEqual(report.status, "RESEARCH_ONLY")

    def test_sensitivity_runs_independent_scenarios_without_hiding_costs(self):
        raw, facts, result = self.sample()
        scenarios = {
            "base": settings(),
            "higher_cost": settings(commission=".00005", exit_slip=".4"),
            "managed": settings(management="BE_PARTIAL"),
        }
        with patch("analytics.strategy_replay.Strategy", ScriptedStrategy):
            out = cost_sensitivity(
                raw, rules(), facts,
                scenarios=scenarios, holdout_start=NOW-timedelta(minutes=15),
            )
        self.assertEqual(set(out), set(scenarios))
        self.assertLess(
            out["higher_cost"]["replay"].trades[0].net_home,
            out["base"]["replay"].trades[0].net_home,
        )
        for item in out.values():
            self.assertFalse(item["holdout"].broker_verified)

    def test_mismatched_account_or_source_prevents_sensitivity_comparison(self):
        raw, facts, result = self.sample()
        different = settings()
        from dataclasses import replace
        with self.assertRaises(ReplayError):
            cost_sensitivity(
                raw, rules(), facts,
                scenarios={
                    "one": different,
                    "two": replace(different, account_currency="EUR"),
                },
                holdout_start=NOW,
            )

    def test_no_tiny_sample_wilson_inference(self):
        _, _, result = self.sample()
        with self.assertRaises(ReplayError):
            holdout(
                result, starting_equity="10000",
                holdout_start=NOW, min_sample=2
            )


if __name__ == "__main__":
    unittest.main()
