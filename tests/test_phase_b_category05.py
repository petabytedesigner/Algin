"""Phase B / Category 05: strict offline strategy-to-OrderEngine path.

Zero network or OANDA mutations: all submissions use the sealed in-memory
SimulatedOrderTransport. Positive tests exercise the real OrderEngine contract.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from broker.account_reconciler import AccountReconciler
from broker.practice_transport import BrokerReply, PracticeTransport
from execution.trade_coordinator import (
    AccountEvidence, CoordinatorRequest, EntryGates, SimulatedOrderTransport,
    TradeCoordinator, stable_signal_id,
)
from risk.reconciled_account import RiskHistoryAnchor
from risk.trade_plan import AccountFunds, InstrumentRules, QuoteConversions
from storage.intent_journal import IntentJournal
from storage.lifecycle_state import LifecycleStore, ProcessLease
from strategy.strategy import StrategySignal


NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
D = Decimal


class FakeReadOnlyAccount:
    def __init__(self):
        self.calls = []

    def read(self, path, *, params=None):
        self.calls.append(("GET_ONLY", path))
        if path != "":
            raise AssertionError("No broker mutations or history expected")
        return BrokerReply(200, {
            "lastTransactionID": "1000",
            "account": {
                "id": "acct-1",
                "openTradeCount": 0, "pendingOrderCount": 0,
                "trades": [], "orders": []
            }
        })


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = IntentJournal("acct-1", directory=self.temp.name)
        self.store = LifecycleStore("acct-1", directory=self.temp.name)
        self.broker = FakeReadOnlyAccount()
        service = AccountReconciler(
            "acct-1", transport=self.broker, journal=self.journal,
            store=self.store,
            lease=ProcessLease("acct-1", directory=self.temp.name),
        )
        with service:
            self.report = service.run()
        self.account = AccountFunds(
            "acct-1", "USD", D("10000"), D("10000"), D("8000"),
            D("0"), 0, 0, "1000",
        )
        self.rules = InstrumentRules(
            "EUR_USD", "CURRENCY", -4, 5, 0,
            D("1"), D("1000000"), D("0"), D("0.02"),
        )
        self.quote = QuoteConversions(
            "EUR_USD", D("1.1000"), D("1.1002"), NOW,
            D("1"), D("1.1"),
        )
        self.anchor = RiskHistoryAnchor(
            account_id="acct-1", cursor="1000",
            day=NOW.date().isoformat(), day_start_cursor="1000",
            day_start_nav=D("10000"), peak_nav=D("10000"),
            starting_loss_streak=0, history_complete=True,
        )
        self.signal = StrategySignal(
            instrument="EUR_USD", direction="LONG", score=85,
            grade="A", metrics={}, reason="synthetic confirmed BOS/retest",
            timestamp=NOW,
        )
        self.gates = EntryGates(True, True, True, True)
        self.coordinator = TradeCoordinator(self.journal)

    def request(self, *, signal=None, gates=None, account=None,
                quote=None, anchor="DEFAULT", candle_time=None,
                report=None, broker_trades=(), broker_orders=(),
                state=None, attempts=None, now=NOW):
        acc = account if account is not None else self.account
        ev = AccountEvidence(
            funds=acc, state=state if state is not None else self.store.load(),
            journal_attempts=(
                attempts if attempts is not None else self.journal.snapshot()
            ), broker_trades=tuple(broker_trades), broker_orders=tuple(broker_orders),
            report=report if report is not None else self.report,
            history_anchor=(
                self.anchor if anchor == "DEFAULT" else anchor
            ),
        )
        return CoordinatorRequest(
            signal=signal if signal is not None else self.signal,
            candle_time=candle_time or NOW - timedelta(minutes=15),
            atr=D("0.0010"), rules=self.rules,
            quote=quote if quote is not None else self.quote,
            evidence=ev, gates=gates if gates is not None else self.gates,
            now=now,
        )

    def simulation(self, *, mode="FILLED"):
        return SimulatedOrderTransport(self.account, self.quote, mode=mode)

    def test_preview_signal_filters_reconciled_risk_tradeplan_no_post(self):
        request = self.request()
        result = self.coordinator.preview(request)
        self.assertEqual(result.status, "PREVIEW")
        self.assertEqual(result.reason_code, "VERIFIED_SYNTHETIC_PLAN")
        self.assertTrue(result.plan.preview_only)
        self.assertGreater(result.plan.units, 0)
        self.assertFalse(result.entry_allowed)
        self.assertEqual(self.journal.snapshot(), ())
        self.assertEqual(self.broker.calls, [("GET_ONLY", "")])

    def test_one_synthetic_fill_via_existing_order_engine_and_protection(self):
        request = self.request()
        sim = self.simulation()
        result = self.coordinator.simulate(
            request, refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.status, "SIMULATED")
        self.assertEqual(result.reason_code, "SYNTHETIC_FILL_PROTECTED")
        self.assertTrue(result.execution.protection_verified)
        self.assertEqual(sim.post_count, 1)
        self.assertEqual(sim.last_payload["order"]["positionFill"], "OPEN_ONLY")
        self.assertEqual(sim.last_payload["order"]["timeInForce"], "FOK")
        self.assertEqual(len(self.journal.snapshot()), 1)
        self.assertEqual(self.journal.snapshot()[0]["signal_id"], result.signal_id)
        self.assertEqual(self.journal.snapshot()[0]["status"], "FILLED")
        self.assertFalse(result.entry_allowed)
        self.assertEqual(self.broker.calls, [("GET_ONLY", "")])

    def test_same_completed_candle_never_posts_a_second_time(self):
        req = self.request()
        first_sim = self.simulation()
        first = self.coordinator.simulate(
            req, refresh=lambda: self.request(), transport=first_sim
        )
        self.assertEqual(first.status, "SIMULATED")
        second_sim = self.simulation()
        second = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(),
            transport=second_sim
        )
        self.assertEqual(second.status, "BLOCKED")
        self.assertEqual(second.reason_code, "SIGNAL_ALREADY_RECORDED")
        self.assertEqual(first_sim.post_count, 1)
        self.assertEqual(second_sim.post_count, 0)

    def test_identity_same_after_restart_and_independent_of_grade_and_score(self):
        candle = NOW - timedelta(minutes=15)
        original = stable_signal_id("acct-1", "EUR_USD", "LONG", candle)
        newer = replace(self.signal, score=94, grade="B")
        self.assertEqual(original, stable_signal_id(
            "acct-1", newer.instrument, newer.direction, candle
        ))
        self.assertNotEqual(original, stable_signal_id(
            "acct-1", "EUR_USD", "SHORT", candle
        ))
        self.assertNotEqual(original, stable_signal_id(
            "acct-1", "EUR_USD", "LONG", candle + timedelta(minutes=15)
        ))
        self.assertNotEqual(original, stable_signal_id(
            "other", "EUR_USD", "LONG", candle
        ))

    def test_unverified_independent_filters_all_block_before_synthetic_post(self):
        for field, code in (
            ("completed_candle_verified", "CANDLE_UNVERIFIED"),
            ("session_allowed", "SESSION_UNVERIFIED"),
            ("news_clear", "NEWS_UNVERIFIED"),
            ("broker_tradeable", "TRADEABILITY_UNVERIFIED"),
        ):
            with self.subTest(field=field):
                gates = replace(self.gates, **{field: False})
                sim = self.simulation()
                result = self.coordinator.simulate(
                    self.request(gates=gates), refresh=lambda: self.request(),
                    transport=sim
                )
                self.assertEqual(result.reason_code, code)
                self.assertEqual(sim.post_count, 0)

    def test_missing_historical_nav_anchor_blocks_broker(self):
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(anchor=None),
            refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.reason_code, "RISK_NOT_VERIFIED")
        self.assertEqual(sim.post_count, 0)

    def test_expired_candle_refuses_entry(self):
        result = self.coordinator.preview(
            self.request(candle_time=NOW - timedelta(hours=3))
        )
        self.assertEqual(result.reason_code, "CANDLE_EXPIRED")

    def test_grade_c_never_enters_risk_planner(self):
        bad = replace(self.signal, grade="C")
        result = self.coordinator.preview(self.request(signal=bad))
        self.assertEqual(result.reason_code, "GRADE_NOT_TRADEABLE")

    def test_refresh_failure_blocks_without_post(self):
        def crash():
            raise OSError("GET read failed")
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(), refresh=crash, transport=sim
        )
        self.assertEqual(result.reason_code, "FRESH_RECONCILIATION_UNAVAILABLE")
        self.assertEqual(sim.post_count, 0)

    def test_final_risk_replan_rejects_nav_drop(self):
        lower = replace(self.account, balance=D("9900"), nav=D("9900"))
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(account=lower),
            transport=sim
        )
        self.assertEqual(result.reason_code, "RISK_OR_SIGNAL_CHANGED")
        self.assertEqual(sim.post_count, 0)

    def test_candle_change_during_final_check_refuses_old_plan(self):
        sim = self.simulation()
        request = self.request()
        result = self.coordinator.simulate(
            request,
            refresh=lambda: self.request(
                candle_time=NOW - timedelta(minutes=30)
            ), transport=sim
        )
        self.assertEqual(result.reason_code, "RISK_OR_SIGNAL_CHANGED")
        self.assertEqual(sim.post_count, 0)

    def test_stale_journal_generation_never_posts(self):
        prior = self.request()
        self.journal.begin(
            signal_id="other-signal", attempt_id="algin-other",
            request={"order": {"instrument": "GBP_USD", "units": "10"}},
            risk_pct="0.5", margin_home="5", grade="B",
        )
        sim = self.simulation()
        result = self.coordinator.simulate(
            prior, refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.reason_code, "JOURNAL_SNAPSHOT_CHANGED")
        self.assertEqual(sim.post_count, 0)

    def test_unknown_durable_intent_blocks_during_risk_reconciliation(self):
        self.journal.begin(
            signal_id="unknown", attempt_id="algin-unknown",
            request={"order": {"instrument": "EUR_USD", "units": "10"}},
            risk_pct="0.5", margin_home="5", grade="B",
        )
        self.journal.transition("algin-unknown", "IN_FLIGHT")
        self.journal.transition("algin-unknown", "UNKNOWN")
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.reason_code, "RISK_NOT_VERIFIED")
        self.assertEqual(sim.post_count, 0)

    def test_pending_broker_order_blocks_even_when_count_matches(self):
        pending = ({"id": "500", "instrument": "EUR_USD"},)
        larger = replace(self.account, broker_pending_count=1)
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(account=larger, broker_orders=pending),
            refresh=lambda: self.request(), transport=sim,
        )
        self.assertEqual(result.reason_code, "RISK_NOT_VERIFIED")
        self.assertEqual(sim.post_count, 0)

    def test_reconciler_blocker_cannot_be_forged_as_readiness(self):
        bad_report = replace(
            self.report, blocked_reasons=("BROKER_UNCERTAIN",)
        )
        sim = self.simulation()
        result = self.coordinator.simulate(
            self.request(report=bad_report),
            refresh=lambda: self.request(), transport=sim,
        )
        self.assertEqual(result.reason_code, "ACCOUNT_RECONCILIATION_BLOCK")
        self.assertEqual(sim.post_count, 0)

    def test_modified_synthetic_broker_state_refuses_order(self):
        sim = self.simulation()
        sim._account["account"]["NAV"] = "9000"
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.reason_code, "SYNTHETIC_BROKER_STATE_CHANGED")
        self.assertEqual(sim.post_count, 0)

    def test_fake_timeout_is_unknown_once_no_blind_retry(self):
        sim = self.simulation(mode="TIMEOUT")
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=sim
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.reason_code, "SYNTHETIC_OUTCOME_UNCERTAIN")
        self.assertEqual(sim.post_count, 1)
        self.assertEqual(self.journal.snapshot()[0]["status"], "UNKNOWN")
        other = self.simulation()
        blocked = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=other,
        )
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(other.post_count, 0)

    def test_fake_rejection_still_records_durable_identity(self):
        sim = self.simulation(mode="REJECTED")
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=sim,
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(sim.post_count, 1)
        self.assertEqual(self.journal.snapshot()[0]["status"], "REJECTED")
        self.assertEqual(
            self.coordinator.preview(self.request()).reason_code,
            "SIGNAL_ALREADY_RECORDED"
        )

    def test_arbitrary_write_transport_is_never_accepted(self):
        class FakeMalicious:
            def __init__(self):
                self.posts = 0

            def submit_market_order(self, payload):
                self.posts += 1
                raise AssertionError("Must never be called")

        malicious = FakeMalicious()
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(),
            transport=malicious,
        )
        self.assertEqual(result.reason_code, "OFFLINE_GATE_REQUIRED")
        self.assertEqual(malicious.posts, 0)

    def test_subclass_override_cannot_escape_sealed_transport(self):
        class EvilSubclass(SimulatedOrderTransport):
            def submit_market_order(self, payload):
                raise AssertionError("No override should run")
        fake = EvilSubclass(self.account, self.quote)
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=fake
        )
        self.assertEqual(result.reason_code, "OFFLINE_GATE_REQUIRED")
        self.assertEqual(fake.post_count, 0)

    def test_real_practice_transport_is_rejected_before_network(self):
        real = PracticeTransport()
        result = self.coordinator.simulate(
            self.request(), refresh=lambda: self.request(), transport=real
        )
        self.assertEqual(result.reason_code, "OFFLINE_GATE_REQUIRED")

    def test_order_write_config_flag_is_refused_even_in_simulator(self):
        import config
        from unittest.mock import patch
        sim = self.simulation()
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            result = self.coordinator.simulate(
                self.request(), refresh=lambda: self.request(), transport=sim
            )
        self.assertEqual(result.reason_code, "OFFLINE_GATE_REQUIRED")
        self.assertEqual(sim.post_count, 0)


if __name__ == "__main__":
    unittest.main()
