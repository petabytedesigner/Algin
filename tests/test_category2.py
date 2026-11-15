"""Category 2 regression tests. Synthetic fixtures only; zero broker mutations."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import config
import main
from broker import risk_data
from risk.risk_engine import (
    Grade, OpenPositionRisk, RiskConfig, RiskDecisionCode, RiskEngine, TradeCandidate,
)
from risk.trade_plan import (
    AccountFunds, ExposureBook, ExposureReservation, InstrumentRules, LossState,
    PlanRejected, QuoteConversions, RiskPolicy, TradePlanner,
)


NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def D(x):
    return Decimal(str(x))


class Response:
    status_code = 200

    def __init__(self, raw):
        self.raw = raw

    def json(self):
        return self.raw


def case(*, direction="LONG", grade="A", kind="CURRENCY",
         currency="USD", loss_factor="1", position_factor="1.1001",
         balance="10000", nav="10000", margin="8000",
         reserved=(), open_count=0, pending_count=0,
         verified=True, uncertain=True, loss_verified=True,
         losses=0, peak="10000", daily_loss="0",
         day_start_nav="10000", instrument="EUR_USD"):
    sig = SimpleNamespace(
        instrument=instrument, direction=direction, grade=grade,
        score=85, timestamp=NOW
    )
    acct = AccountFunds(
        "acct-1", currency, D(balance), D(nav), D(margin), D("200"),
        open_count, pending_count, "t100"
    )
    spec = InstrumentRules(
        instrument, kind, -4, 5, 0,
        D("1"), D("1000000"), D("0"), D("0.02")
    )
    quote = QuoteConversions(
        instrument, D("1.1000"), D("1.1002"), NOW,
        D(loss_factor), D(position_factor)
    )
    book = ExposureBook(
        "acct-1", tuple(reserved), verified=verified,
        uncertain_outcomes_accounted=uncertain
    )
    status = LossState(
        D(peak), losses, loss_verified,
        day_start_nav=D(day_start_nav),
        daily_realized_loss_home=D(daily_loss),
    )
    return sig, acct, spec, quote, book, status


def plan(*, atr="0.0010", **kwargs):
    sig, account, spec, quote, book, status = case(**kwargs)
    return TradePlanner().plan(
        sig, D(atr), account, spec, quote, book, status, now=NOW
    )


def reserved(reservation_id, instrument, *, direction="LONG", grade="B",
             risk="0.5", state="PENDING", margin="100"):
    return ExposureReservation(
        reservation_id, instrument, direction, grade, D(risk), state,
        D(margin) if state != "OPEN" else None
    )


class TradePlanningTests(unittest.TestCase):
    def test_P5_P12_P13_P15_long_from_signal(self):
        trade = plan()
        self.assertTrue(trade.preview_only)
        self.assertGreater(trade.units, 0)
        self.assertLess(trade.stop_loss, trade.entry)
        self.assertGreater(trade.take_profit, trade.entry)
        self.assertGreaterEqual(trade.reward_to_risk, 3)
        self.assertLessEqual(trade.computed_risk_home, trade.risk_budget_home)
        self.assertEqual(trade.risk_budget_home, D("100"))
        self.assertLessEqual(trade.estimated_margin_home, D("4000"))

    def test_P5_P7_short_signed_units_and_stops(self):
        trade = plan(direction="SHORT")
        self.assertLess(trade.units, 0)
        self.assertGreater(trade.stop_loss, trade.entry)
        self.assertLess(trade.take_profit, trade.entry)
        self.assertGreaterEqual(trade.reward_to_risk, 3)

    def test_P9_grade_risk_is_separate_from_total(self):
        a, b = plan(grade="A"), plan(grade="B")
        self.assertEqual(a.reserved_risk_pct, D("1.0"))
        self.assertEqual(b.reserved_risk_pct, D("0.5"))
        self.assertLess(abs(b.units), abs(a.units))
        self.assertEqual(main.build_risk_policy().max_total_risk_pct, D("1.0"))
        self.assertEqual(config.RISK_PER_TRADE, 1.0)

    def test_P10_invalid_policy_and_risk(self):
        for value in ("0", "-1", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(PlanRejected):
                    RiskPolicy(grade_a_pct=D(value))
        with self.assertRaises(PlanRejected):
            RiskPolicy(grade_b_pct=D("2"))
        for value in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(atr=value):
                with self.assertRaises(PlanRejected):
                    plan(atr=value)

    def test_P10_invalid_account_and_margin(self):
        for nav in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(nav=nav):
                with self.assertRaises(PlanRejected):
                    case(nav=nav)
        with self.assertRaises(PlanRejected):
            case(margin="-1")
        with self.assertRaises(PlanRejected):
            case(currency="US")

    def test_P11_nav_smaller_than_balance_limits_risk(self):
        trade = plan(nav="9800", balance="10000", peak="10000")
        self.assertEqual(trade.risk_budget_home, D("98"))
        self.assertLessEqual(trade.computed_risk_home, D("98"))

    def test_P11_conservative_base_and_quote_conversion(self):
        one = plan()
        more_expensive_loss = plan(loss_factor="1.4")
        self.assertLess(abs(more_expensive_loss.units), abs(one.units))
        self.assertLessEqual(
            more_expensive_loss.computed_risk_home,
            more_expensive_loss.risk_budget_home
        )

    def test_P12_missing_or_nonpositive_conversion(self):
        with self.assertRaises(PlanRejected):
            case(loss_factor="NaN")
        with self.assertRaises(PlanRejected):
            case(position_factor="0")

    def test_P13_round_stop_outward_and_reward_up(self):
        t = plan(atr="0.001003")
        self.assertEqual(t.stop_loss.as_tuple().exponent, -5)
        self.assertEqual(t.take_profit.as_tuple().exponent, -5)
        self.assertGreaterEqual(t.reward_to_risk, 3)

    def test_P14_instrument_minimum_and_maximum(self):
        sig, acct, spec, quote, exposure, status = case()
        from dataclasses import replace
        capped = replace(spec, maximum_order_units=D("100"))
        t = TradePlanner().plan(
            sig, D("0.0010"), acct, capped, quote, exposure, status, now=NOW
        )
        self.assertEqual(t.units, D("100"))
        minimum = replace(spec, minimum_trade_size=D("1000000"))
        with self.assertRaises(PlanRejected):
            TradePlanner().plan(
                sig, D("0.0010"), acct, minimum, quote, exposure, status, now=NOW
            )
        with self.assertRaises(PlanRejected):
            plan(kind="CFD")

    def test_P14_fractional_precision_round_down(self):
        from dataclasses import replace
        sig, acct, spec, quote, exposure, status = case()
        special = replace(spec, units_precision=2, maximum_order_units=D("3.239"))
        t = TradePlanner().plan(
            sig, D("0.0010"), acct, special, quote, exposure, status, now=NOW
        )
        self.assertEqual(t.units, D("3.23"))

    def test_P15_margin_cap_after_rounding(self):
        t = plan(margin="10")
        self.assertLessEqual(
            t.estimated_margin_home, D("5")
        )
        self.assertLessEqual(
            t.computed_risk_home, t.risk_budget_home
        )
        with self.assertRaises(PlanRejected):
            plan(margin="0.01")

    def test_P16_incomplete_broker_exposure_is_rejected(self):
        with self.assertRaises(PlanRejected):
            plan(verified=False)
        with self.assertRaises(PlanRejected):
            plan(uncertain=False)
        with self.assertRaises(PlanRejected):
            plan(open_count=1)
        with self.assertRaises(PlanRejected):
            plan(pending_count=1)

    def test_P16_unknown_outcome_counted_not_ignored(self):
        unknown = reserved("attempt-1", "GBP_USD", state="OUTCOME_UNKNOWN")
        with self.assertRaises(PlanRejected):
            plan(reserved=(unknown,), grade="A")
        t = plan(reserved=(unknown,), grade="B")
        self.assertEqual(t.reserved_risk_pct, D("0.5"))
        self.assertLessEqual(t.computed_risk_pct + unknown.risk_pct, D("1.0"))
        with self.assertRaises(PlanRejected):
            plan(reserved=(reserved("x", "EUR_USD", state="OUTCOME_UNKNOWN"),),
                 grade="B")

    def test_P17_same_instrument_hedging_and_total_count(self):
        item = reserved("p1", "EUR_USD", direction="SHORT", state="OPEN")
        with self.assertRaises(PlanRejected):
            plan(reserved=(item,), open_count=1, grade="B")
        others = (
            reserved("1", "GBP_USD", risk="0.2"),
            reserved("2", "USD_JPY", risk="0.2"),
            reserved("3", "USD_CAD", risk="0.2"),
        )
        with self.assertRaises(PlanRejected):
            plan(reserved=others, pending_count=3, grade="B")

    def test_P18_drawdown_loss_streak_and_cooldown(self):
        with self.assertRaises(PlanRejected):
            plan(nav="9000", peak="10000")
        with self.assertRaises(PlanRejected):
            plan(losses=3)
        with self.assertRaises(PlanRejected):
            plan(loss_verified=False)
        sig, acct, spec, quote, exp, loss = case()
        from dataclasses import replace
        cooldown = replace(
            loss, cooldown_until_by_instrument={"EUR_USD": NOW + timedelta(minutes=15)}
        )
        with self.assertRaises(PlanRejected):
            TradePlanner().plan(
                sig, D("0.001"), acct, spec, quote, exp, cooldown, now=NOW
            )

    def test_stale_or_future_quote_refused(self):
        from dataclasses import replace
        sig, acct, spec, quote, exp, loss = case()
        for time in (NOW - timedelta(minutes=1), NOW + timedelta(seconds=5)):
            with self.subTest(time=time):
                with self.assertRaises(PlanRejected):
                    TradePlanner().plan(
                        sig, D("0.001"), acct, spec, replace(quote, timestamp=time),
                        exp, loss, now=NOW
                    )

    def test_legacy_risk_engine_rejects_invalid_percent(self):
        engine = RiskEngine()
        for percent in ("0", "-2", "NaN", "Infinity", 1.5):
            with self.subTest(percent=percent):
                candidate = TradeCandidate(
                    "EUR_USD", "LONG", Grade.A, 85, percent, NOW
                )
                decision = engine.evaluate_candidate(candidate, [])
                self.assertFalse(decision.accept)
        with self.assertRaises(ValueError):
            RiskEngine(RiskConfig(max_total_risk_pct=float("nan")))

    def test_category1_public_contracts_and_no_writes(self):
        self.assertFalse(config.ORDER_WRITES_ENABLED)
        self.assertEqual(main.main(["--check"]), 0)
        self.assertTrue(callable(main.build_trade_plan))
        self.assertEqual(main.build_risk_engine().config.max_total_risk_pct, 1.0)


    def test_P16_unsettled_margin_is_reserved_as_well_as_risk(self):
        item = reserved(
            "pending-77", "GBP_USD", state="PENDING",
            risk="0.5", margin="60"
        )
        trade = plan(
            grade="B", reserved=(item,), pending_count=1, margin="100"
        )
        self.assertLessEqual(trade.estimated_margin_home, D("20"))
        with self.assertRaises(PlanRejected):
            plan(grade="B", reserved=(item,), pending_count=1, margin="50")

    def test_P16_unknown_intents_require_a_margin_reservation(self):
        with self.assertRaises(PlanRejected):
            ExposureReservation(
                "pending", "GBP_USD", "LONG", "B",
                D("0.5"), "PENDING"
            )
        with self.assertRaises(PlanRejected):
            ExposureReservation(
                "unknown", "GBP_USD", "LONG", "B",
                D("0.5"), "OUTCOME_UNKNOWN",
                reserved_margin_home=D("NaN")
            )
        with self.assertRaises(PlanRejected):
            ExposureReservation(
                "unknown", "GBP_USD", "LONG", "B",
                D("0.5"), "OUTCOME_UNKNOWN",
                reserved_margin_home=D("-1")
            )

    def test_P10_decimal_inputs_normalized_before_planning(self):
        cfg = RiskPolicy(
            grade_a_pct="1.0", grade_b_pct="0.5",
            max_total_risk_pct="1.0",
            minimum_stop_pips="3", max_margin_fraction="0.5"
        )
        self.assertIsInstance(cfg.grade_a_pct, Decimal)
        self.assertIsInstance(cfg.minimum_stop_pips, Decimal)
        sig, acct, instrument, quote, book, losses = case()
        from dataclasses import replace
        acct = replace(acct, balance="10000", margin_available="8000")
        quote = replace(quote, bid="1.1000", ask="1.1002")
        instrument = replace(
            instrument, minimum_trade_size="1",
            maximum_order_units="1000000", margin_rate="0.02"
        )
        result = TradePlanner(cfg).plan(
            sig, "0.0010", acct, instrument, quote, book, losses, now=NOW
        )
        self.assertGreater(result.units, 0)



    def test_P17_shared_currency_concentration_is_configurable(self):
        policy = RiskPolicy(max_currency_risk_pct=D("0.75"))
        pending = reserved("prior-gbp", "GBP_USD", grade="B", risk="0.5", margin="100")
        sig, acct, spec, quote, book, losses = case(
            grade="B", reserved=(pending,), pending_count=1
        )
        with self.assertRaises(PlanRejected):
            TradePlanner(policy).plan(
                sig, D("0.0010"), acct, spec, quote, book, losses, now=NOW
            )
        # AUD_CAD shares neither GBP nor USD, so the same booked risk is
        # within the currency concentration cap.
        sig, acct, spec, quote, book, losses = case(
            instrument="AUD_CAD", grade="B",
            reserved=(pending,), pending_count=1
        )
        different = TradePlanner(policy).plan(
            sig, D("0.0010"), acct, spec, quote, book, losses, now=NOW
        )
        self.assertEqual(different.instrument, "AUD_CAD")

    def test_P18_daily_loss_lock(self):
        with self.assertRaises(PlanRejected):
            plan(daily_loss="200", day_start_nav="10000")
        self.assertGreater(
            plan(daily_loss="199", day_start_nav="10000").units, 0
        )
        with self.assertRaises(PlanRejected):
            LossState(D("10000"), 0, True)
        with self.assertRaises(PlanRejected):
            LossState(
                D("10000"), 0, True,
                day_start_nav=D("10000"),
                daily_realized_loss_home=D("-1")
            )

    def test_P13_verified_structure_level_widens_stop(self):
        inputs = case()
        a = TradePlanner().plan(*(
            inputs[0], D("0.0010"), *inputs[1:]
        ), now=NOW)
        b = TradePlanner().plan(*(
            inputs[0], D("0.0010"), *inputs[1:]
        ), now=NOW, structure_level="1.0950")
        self.assertLess(b.stop_loss, a.stop_loss)
        self.assertLess(b.units, a.units)
        self.assertEqual(b.stop_basis, "ATR+STRUCTURE")

        sig, acct, spec, quote, book, loss = case(direction="SHORT")
        short = TradePlanner().plan(
            sig, D("0.0010"), acct, spec, quote, book, loss,
            now=NOW, structure_level="1.1050"
        )
        self.assertGreater(short.stop_loss, D("1.1050"))
        self.assertLess(short.units, 0)

    def test_P13_wrong_structure_boundary_fails_closed(self):
        sig, acct, spec, quote, book, loss = case()
        for invalid in ("1.2", "NaN", "0"):
            with self.subTest(level=invalid):
                with self.assertRaises(PlanRejected):
                    TradePlanner().plan(
                        sig, D("0.0010"), acct, spec, quote, book, loss,
                        now=NOW, structure_level=invalid
                    )



class RiskDataTests(unittest.TestCase):
    def setUp(self):
        self.credentials = (
            patch.object(config, "API_KEY", "dummy-token"),
            patch.object(config, "ACCOUNT_ID", "acct-1"),
        )
        for context in self.credentials:
            context.start()
            self.addCleanup(context.stop)

    def test_P11_full_account_details_validated(self):
        payload = {"lastTransactionID": "t1", "account": {
            "id": "acct-1", "currency": "USD",
            "balance": "10000", "NAV": "9900",
            "marginAvailable": "9000", "marginUsed": "900",
            "openTradeCount": 0, "pendingOrderCount": 0,
            "trades": [], "orders": []
        }}
        with patch("broker.risk_data.safe_request", return_value=Response(payload)) as req:
            obj = risk_data.get_account_read()
        self.assertEqual(obj.funds.currency, "USD")
        self.assertEqual(obj.funds.nav, D("9900"))
        self.assertTrue(req.call_args.args[1].endswith("/accounts/acct-1"))

    def test_P11_account_mismatch_and_omitted_exposure_fail(self):
        base = {"lastTransactionID": "t1", "account": {
            "id": "acct-1", "currency": "USD", "balance": "10000", "NAV": "10000",
            "marginAvailable": "9500", "marginUsed": "500",
            "openTradeCount": 1, "pendingOrderCount": 0,
            "trades": [], "orders": []
        }}
        for mutation in ("count", "account", "missing"):
            import copy
            obj = copy.deepcopy(base)
            if mutation == "account":
                obj["account"]["id"] = "other"
            if mutation == "missing":
                del obj["account"]["orders"]
            with self.subTest(mutation=mutation):
                with patch("broker.risk_data.safe_request", return_value=Response(obj)):
                    with self.assertRaises(PlanRejected):
                        risk_data.get_account_read()

    def test_P14_instrument_metadata_preserves_units_precision(self):
        raw = {"instruments": [{
            "name": "EUR_USD", "type": "CURRENCY",
            "pipLocation": -4, "displayPrecision": 5,
            "tradeUnitsPrecision": 2, "minimumTradeSize": "0.01",
            "maximumOrderUnits": "1000000", "maximumPositionSize": "0",
            "marginRate": "0.02"
        }]}
        with patch("broker.risk_data.safe_request", return_value=Response(raw)) as req:
            out = risk_data.get_instrument_rules("EUR_USD")
        self.assertEqual(out.unit_step, D("0.01"))
        self.assertEqual(out.pip_size, D("0.0001"))
        self.assertEqual(req.call_args.kwargs["params"], {"instruments": "EUR_USD"})

    def test_P12_home_conversions_not_deprecated_quote_factors(self):
        raw = {"prices": [{
            "instrument": "EUR_USD", "tradeable": True,
            "bids": [{"price": "1.10"}], "asks": [{"price": "1.11"}],
            "time": "2026-11-15T10:00:00Z",
            "quoteHomeConversionFactors": {"positiveUnits": "999", "negativeUnits": "999"}
        }], "homeConversions": [
            {"currency": "EUR", "accountLoss": "1.1", "positionValue": "1.11"},
            {"currency": "USD", "accountLoss": "1", "positionValue": "1"}
        ]}
        with patch("broker.risk_data.safe_request", return_value=Response(raw)) as req:
            context = risk_data.get_quote_conversions("EUR_USD", "USD")
        self.assertEqual(context.quote_loss_to_home, D("1"))
        self.assertEqual(context.base_value_to_home, D("1.11"))
        self.assertEqual(req.call_args.kwargs["params"]["includeHomeConversions"], "true")
        del raw["homeConversions"]
        with patch("broker.risk_data.safe_request", return_value=Response(raw)):
            with self.assertRaises(PlanRejected):
                risk_data.get_quote_conversions("EUR_USD", "USD")

    def test_P12_cross_currency_uses_account_loss_factor(self):
        raw = {"prices": [{
            "instrument": "EUR_USD", "tradeable": True,
            "bids": [{"price": "1.10"}], "asks": [{"price": "1.11"}],
            "time": "2026-11-15T10:00:00Z"
        }], "homeConversions": [
            {"currency": "EUR", "positionValue": "0.91"},
            {"currency": "USD", "accountLoss": "0.94"}
        ]}
        with patch("broker.risk_data.safe_request", return_value=Response(raw)):
            q = risk_data.get_quote_conversions("EUR_USD", "GBP")
        self.assertEqual(q.quote_loss_to_home, D("0.94"))
        self.assertEqual(q.base_value_to_home, D("0.91"))

    def test_fail_closed_when_conversion_missing_or_duplicate(self):
        raw = {"prices": [{
            "instrument": "EUR_USD", "tradeable": True,
            "bids": [{"price": "1.10"}], "asks": [{"price": "1.11"}],
            "time": "2026-11-15T10:00:00Z"
        }], "homeConversions": [
            {"currency": "EUR", "positionValue": "1.11"}
        ]}
        with patch("broker.risk_data.safe_request", return_value=Response(raw)):
            with self.assertRaises(PlanRejected):
                risk_data.get_quote_conversions("EUR_USD", "GBP")
        raw["homeConversions"].append({"currency": "EUR", "positionValue": "1.10"})
        with patch("broker.risk_data.safe_request", return_value=Response(raw)):
            with self.assertRaises(PlanRejected):
                risk_data.get_quote_conversions("EUR_USD", "GBP")

    def test_network_failure_refuses_to_invent_account(self):
        with patch("broker.risk_data.safe_request", return_value=None):
            with self.assertRaises(PlanRejected):
                risk_data.get_account_read()


if __name__ == "__main__":
    unittest.main()
