"""Category 6 negative/positive regressions; offline broker fixtures only.

P57–61, P64–67, P70, P71, P74. Do not use live OANDA credentials.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import config
import main
from analytics.costs import CostError, CostLedger, CostRecord
from analytics.intrabar import IntrabarError, stop_or_target
from analytics.performance import (
    CompletedTrade, PerformanceError, performance_report
)
from broker.order_engine import OrderEngine
from broker.practice_transport import BrokerWriteDisabled, PracticeTransport
from execution.trade_executor import BrokerDataError, TradeExecutor
from storage.intent_journal import IntentJournal
from storage.lifecycle_state import LifecycleStore
from telemetry.events import AuditError, AuditEvents, FIELDS, event_id
from telemetry.heartbeat import HeartbeatError, HeartbeatStore
from telemetry.trace import TradeTrace
from test_category3 import FakeTransport, inputs, NOW


def d(x):
    return Decimal(str(x))


class AuditEventsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = folder.name
        self.audit = AuditEvents("acct-1", directory=self.base)

    def write(self, kind="SIGNAL_BLOCK", **kw):
        args = {"eid": "ev-123", "stage": "STRATEGY",
                "outcome": "BLOCKED", "reason_code": "NO_SIGNAL",
                "instrument": "EUR_USD", "cycle_id": "cycle-1"}
        args.update(kw)
        return self.audit.emit(kind, **args)

    def test_P57_stable_uniform_columns_attempt_success_unknown(self):
        with (patch.object(config, "CSV_ENABLED", True),
              patch.object(config, "CSV_LOG_SIGNALS", True),
              patch.object(config, "CSV_LOG_TRADES", True)):
            self.write()
            self.write(
                "ORDER_ATTEMPT", eid="ev-124", stage="BROKER",
                outcome="PREPARED", reason_code="",
                attempt_id="algin-12", signal_id="sig-12"
            )
            self.write(
                "ORDER_RESULT", eid="ev-125", stage="BROKER",
                outcome="UNKNOWN", reason_code="BROKER_RESULT_UNKNOWN",
                attempt_id="algin-12", signal_id="sig-12"
            )
            self.write(
                "ORDER_RESULT", eid="ev-126", stage="BROKER",
                outcome="FILLED", reason_code="BROKER_TRANSACTION_CONFIRMED",
                attempt_id="algin-12", trade_id="13"
            )
        with self.audit.path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(tuple(reader.fieldnames), FIELDS)
            rows = list(reader)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["outcome"] for r in rows],
                         ["BLOCKED", "PREPARED", "UNKNOWN", "FILLED"])
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertNotIn("acct-1", self.audit.path.read_text())

    def test_P57_replay_idempotent_but_conflicting_id_fails(self):
        with patch.object(config, "CSV_ENABLED", True):
            self.assertTrue(self.write())
            self.assertFalse(self.write())
            self.assertEqual(len(self.audit.read()), 1)
            with self.assertRaises(AuditError):
                self.write(reason_code="DIFFERENT_REASON")

    def test_P57_corrupt_header_and_partial_row_never_repaired(self):
        self.audit.path.write_text("a,b,c\nx,y,z\n")
        with self.assertRaises(AuditError):
            self.audit.read()
        self.assertEqual(self.audit.path.read_text(), "a,b,c\nx,y,z\n")
        self.audit.path.write_text(",".join(FIELDS)+"\nrow,too,short\n")
        with self.assertRaises(AuditError):
            self.audit.read()

    def test_P57_orphaned_lock_and_symlink_denied(self):
        self.audit.lock.write_text("orphan")
        with patch.object(config, "CSV_ENABLED", True):
            with self.assertRaises(AuditError):
                self.write()
        self.audit.lock.unlink()
        outside = Path(self.base) / "outside"
        outside.write_text("private")
        self.audit.path.symlink_to(outside)
        with patch.object(config, "CSV_ENABLED", True):
            with self.assertRaises(AuditError):
                self.write()
        self.assertEqual(outside.read_text(), "private")

    def test_P58_global_and_per_event_flags(self):
        with (patch.object(config, "CSV_ENABLED", False),
              patch.object(config, "CSV_LOG_SIGNALS", True)):
            self.assertFalse(self.write())
            self.assertFalse(self.audit.path.exists())
        with (patch.object(config, "CSV_ENABLED", True),
              patch.object(config, "CSV_LOG_SIGNALS", False)):
            self.assertFalse(self.write())
            self.assertFalse(self.audit.path.exists())
        with (patch.object(config, "CSV_ENABLED", True),
              patch.object(config, "CSV_LOG_PARTIAL", True),
              patch.object(config, "CSV_LOG_NEWS_BLOCK", False)):
            self.assertTrue(self.write(
                "PARTIAL", stage="MANAGEMENT", outcome="CONFIRMED",
                reason_code="BROKER_PARTIAL_CONFIRMED"
            ))
            self.assertFalse(self.write("NEWS_BLOCK", stage="NEWS"))
        self.assertEqual(self.audit.read()[0]["event_type"], "PARTIAL")

    def test_P58_flags_exist_for_every_supported_event(self):
        from telemetry.events import EVENT_FLAGS
        for kind, flag in EVENT_FLAGS.items():
            self.assertTrue(hasattr(config, flag), (kind, flag))

    def test_P60_untrusted_free_text_secret_or_nan_disallowed(self):
        for injected in (
            {"reason_code": "Authorization Bearer top-secret-token"},
            {"cycle_id": "';DROP TABLE events"},
            {"risk_pct": "NaN"},
            {"attempt_id": "=HYPERLINK(http://malicious)"},
        ):
            with self.subTest(payload=injected):
                with self.assertRaises(AuditError):
                    self.write(**injected)
        self.assertFalse(self.audit.path.exists())


    def test_P58_disable_any_event_type_without_writing(self):
        from telemetry.events import EVENT_FLAGS
        with patch.object(config, "CSV_ENABLED", True):
            for kind, flag in EVENT_FLAGS.items():
                with self.subTest(kind=kind), patch.object(config, flag, False):
                    result = self.audit.emit(
                        kind, eid=event_id("test", kind), stage="STRATEGY",
                        outcome="BLOCKED", reason_code="EVENT_DISABLED"
                    )
                    self.assertFalse(result)
            self.assertFalse(self.audit.path.exists())

class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "heartbeat.json")
        self.heartbeat = HeartbeatStore("acct-1", path=self.path)

    def test_P61_alive_completed_and_broker_not_verified_by_scan(self):
        started = self.heartbeat.start("cycle-1", kind="SCAN", now=NOW)
        self.assertEqual(started["state"], "ALIVE")
        self.assertIsNone(started["last_complete_at"])
        complete = self.heartbeat.finish("cycle-1", kind="SCAN",
                                         success=True, now=NOW)
        self.assertEqual(complete["state"], "CYCLE_COMPLETE")
        self.assertEqual(complete["broker_state"], "OPEN_TRADES_ONLY")
        status = self.heartbeat.status(now=NOW)
        self.assertEqual(status["health"], "RECENT_NOT_PROCESS_PROOF")
        self.assertEqual(status["state"], "CYCLE_COMPLETE")

    def test_P61_failure_does_not_mark_cycle_completed(self):
        self.heartbeat.start("cycle-1", kind="MANAGEMENT", now=NOW)
        self.heartbeat.finish("cycle-1", kind="MANAGEMENT",
                              success=False, now=NOW)
        status = self.heartbeat.status(now=NOW)
        self.assertEqual(status["health"], "BLOCKED")
        self.assertEqual(status["broker_state"], "UNVERIFIED")

    def test_P61_incomplete_cycle_and_restart_blocked(self):
        self.heartbeat.start("cycle-1", kind="SCAN", now=NOW)
        again = HeartbeatStore("acct-1", path=self.path)
        with self.assertRaises(HeartbeatError):
            again.start("cycle-2", kind="SCAN", now=NOW)
        self.assertEqual(again.read()["state"], "ALIVE")

    def test_P61_wrong_account_checksum_and_staleness(self):
        self.heartbeat.start("cycle-1", kind="SCAN", now=NOW)
        self.heartbeat.finish("cycle-1", kind="SCAN", success=True, now=NOW)
        self.assertEqual(
            self.heartbeat.status(now=NOW+timedelta(minutes=5))["health"], "STALE"
        )
        other = HeartbeatStore("acct-2", path=self.path)
        self.assertNotEqual(self.heartbeat.path, other.path)
        self.assertIsNone(other.read())
        blob = json.loads(self.heartbeat.path.read_text())
        blob["data"]["state"] = "ALIVE"
        self.heartbeat.path.write_text(json.dumps(blob))
        with self.assertRaises(HeartbeatError):
            self.heartbeat.read()

    def test_P61_mismatch_finish_cannot_fake_success(self):
        self.heartbeat.start("cycle-1", kind="MANAGEMENT", now=NOW)
        with self.assertRaises(HeartbeatError):
            self.heartbeat.finish("cycle-2", kind="MANAGEMENT", success=True, now=NOW)
        self.assertEqual(self.heartbeat.read()["state"], "ALIVE")


class IntrabarTests(unittest.TestCase):
    def setUp(self):
        self.bar = {"time": "2026-11-15T09:45:00Z", "complete": True,
                    "open": "1.1000", "close": "1.1001",
                    "high": "1.1030", "low": "1.0970"}

    def test_P70_same_bar_both_hit_long_and_short_stop_first(self):
        long = stop_or_target(self.bar, direction="LONG",
                              entry="1.1000", stop="1.0980", target="1.1020")
        short = stop_or_target(self.bar, direction="SHORT",
                               entry="1.1000", stop="1.1020", target="1.0980")
        for value in (long, short):
            self.assertEqual(value.outcome, "STOP")
            self.assertIn("BOTH_TOUCHED_STOP_FIRST", value.evidence)

    def test_P70_incomplete_or_invalid_candle_rejected(self):
        for damaged in (
            dict(self.bar, complete=False),
            dict(self.bar, high="NaN"),
            dict(self.bar, high="1.0960")
        ):
            with self.assertRaises(IntrabarError):
                stop_or_target(damaged, direction="LONG",
                               entry="1.1000", stop="1.0980", target="1.1020")

    def test_P70_finer_bars_need_full_coverage_before_ordering(self):
        early = dict(self.bar, time="2026-11-15T09:45:00Z",
                     open="1.1000", high="1.1010", low="1.0970",
                     close="1.0990")
        middle = dict(self.bar, time="2026-11-15T09:50:00Z",
                      open="1.0990", high="1.1030", low="1.0985",
                      close="1.1020")
        late = dict(self.bar, time="2026-11-15T09:55:00Z",
                    open="1.1020", high="1.1025", low="1.0999",
                    close="1.1001")
        result = stop_or_target(
            self.bar, direction="LONG", entry="1.1000",
            stop="1.0980", target="1.1020", finer=[early, middle, late]
        )
        self.assertEqual(result.outcome, "STOP")
        self.assertIn("CHRONOLOGICAL_FINER_BARS", result.evidence)
        for incomplete in ([early, middle], [late, early, middle]):
            with self.assertRaises(IntrabarError):
                stop_or_target(
                    self.bar, direction="LONG", entry="1.1000",
                    stop="1.0980", target="1.1020", finer=incomplete
                )


    def test_P70_verified_finer_sequence_can_prove_TP_before_SL(self):
        # All 3 M5 bars reconcile M15 OHLC; earlier sub-bar hits only TP.
        base = {"complete": True}
        first = dict(base, time="2026-11-15T09:45:00Z",
                     open="1.1000", high="1.1030", low="1.0990", close="1.1020")
        second = dict(base, time="2026-11-15T09:50:00Z",
                      open="1.1020", high="1.1025", low="1.0970", close="1.0990")
        third = dict(base, time="2026-11-15T09:55:00Z",
                     open="1.0990", high="1.1010", low="1.0980", close="1.1001")
        result = stop_or_target(
            self.bar, direction="LONG", entry="1.1000",
            stop="1.0980", target="1.1020", finer=[first, second, third]
        )
        self.assertEqual(result.outcome, "TAKE_PROFIT")
        self.assertIn("CHRONOLOGICAL_FINER_BARS", result.evidence)

def costs(tx, trade, pl, *, instrument="EUR_USD", basis="EXECUTABLE_FILLS",
          financing=0, commission=0, spread=0, slippage=0, fee=0, dividend=0):
    return CostRecord(
        transaction_id=str(tx), trade_id=str(trade),
        instrument=instrument, price_basis=basis,
        realized_pl_home=d(pl), financing_home=d(financing),
        commission_home=d(commission), guaranteed_fee_home=d(fee),
        dividend_adjustment_home=d(dividend),
        spread_cost_home=d(spread), slippage_cost_home=d(slippage)
    )


class CostTests(unittest.TestCase):
    def test_P71_executable_fill_spread_and_slippage_not_subtracted_twice(self):
        r = costs(11, 1, 100, financing="-4", commission="2",
                  spread="3", slippage="5", fee="1", dividend="0.5")
        self.assertEqual(r.net_home, d("93.5"))
        self.assertTrue(r.informative_costs["spread_in_execution_price"])

    def test_P71_mid_simulation_deducts_spread_slippage_once(self):
        r = costs(11, 1, 100, basis="MID_REFERENCE", financing=-4,
                  commission=2, spread=3, slippage=5, fee=1)
        self.assertEqual(r.net_home, d("85"))
        self.assertFalse(r.informative_costs["spread_in_execution_price"])

    def test_P71_missing_nonfinite_or_negative_commission_rejected(self):
        for changes in (
            {"financing_home": None}, {"commission_home": d("-1")},
            {"spread_cost_home": d("NaN")},
            {"price_basis": "BROKER_MID_MIXED"},
        ):
            row = costs(11, 1, 100)
            with self.subTest(changes=changes):
                with self.assertRaises(CostError):
                    replace(row, **changes)

    def test_P71_transaction_idempotent_but_not_fee_double_allocation(self):
        ledger = CostLedger()
        row = costs(11, 1, 100, commission=2)
        self.assertTrue(ledger.register(row))
        self.assertFalse(ledger.register(row))
        self.assertEqual(ledger.net_home, d("98"))
        with self.assertRaises(CostError):
            ledger.register(costs(11, 2, -10, commission=2))
        with self.assertRaises(CostError):
            ledger.verify_complete_trade("1", ["11", "12"])


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.ledger = CostLedger()
        rows = [
            costs(101, 1, 100, instrument="EUR_USD"),
            costs(102, 2, -50, instrument="EUR_USD"),
            costs(103, 3, -70, instrument="GBP_USD"),
            costs(104, 4, 30, instrument="GBP_USD"),
        ]
        for r in rows:
            self.ledger.register(r)
        self.outcomes = [
            CompletedTrade(str(i), "EUR_USD" if i < 3 else "GBP_USD",
                           "London" if i < 3 else "New York",
                           f"2026-11-10T20:{i:02d}:00Z", (str(100+i),),
                           "2026-11-09T09:00:00Z" if i < 3
                           else "2026-11-09T18:00:00Z")
            for i in range(1,5)
        ]

    def test_P74_complete_performance_metrics_and_drawdown(self):
        report = performance_report(
            self.outcomes, self.ledger, initial_equity_home="1000"
        )
        o = report["overall"]
        self.assertEqual(o["trade_count"], 4)
        self.assertEqual(d(o["net_pl_home"]), d("10"))
        self.assertEqual(d(o["average_win_home"]), d("65"))
        self.assertEqual(d(o["average_loss_home"]), d("-60"))
        self.assertEqual(d(o["win_rate_pct"]), d("50"))
        self.assertEqual(d(o["expectancy_home_per_trade"]), d("2.5"))
        self.assertEqual(d(o["max_drawdown_home"]), d("120"))
        self.assertEqual(d(o["ending_equity_home"]), d("1010"))
        self.assertEqual(o["max_consecutive_losses"], 2)
        self.assertTrue(report["high_concentration_warning"])

    def test_P74_instrument_session_and_large_winners(self):
        report = performance_report(
            self.outcomes, self.ledger, initial_equity_home=1000
        )
        self.assertEqual(report["by_instrument"]["EUR_USD"]["trade_count"], 2)
        self.assertEqual(report["by_session"]["New York"]["net_pl_home"], "-40")
        self.assertGreater(
            d(report["overall"]["largest_winner_share_of_gross_wins_pct"]), 50
        )

    def test_P74_no_loss_profit_factor_undefined_not_infinity(self):
        ledger = CostLedger()
        ledger.register(costs(101, 1, 100))
        result = performance_report(
            self.outcomes[:1], ledger, initial_equity_home=1000
        )
        self.assertIsNone(result["overall"]["profit_factor"])

    def test_P74_missing_costs_and_unknown_outcomes_not_reported(self):
        with self.assertRaises(PerformanceError):
            performance_report(self.outcomes[:-1], self.ledger,
                               initial_equity_home=1000)
        with self.assertRaises(PerformanceError):
            performance_report(
                self.outcomes + self.outcomes[:1], self.ledger,
                initial_equity_home=1000
            )
        with self.assertRaises((PerformanceError, CostError)):
            performance_report(
                self.outcomes, self.ledger, initial_equity_home=None
            )


    def test_P74_session_must_match_original_open_time(self):
        with self.assertRaises(PerformanceError):
            replace(self.outcomes[0], session="New York")
        with self.assertRaises(PerformanceError):
            replace(self.outcomes[0], opened_at="2026-11-16T09:00:00Z")
        with self.assertRaises(PerformanceError):
            replace(self.outcomes[0], opened_at="2026-11-09T09:00:00")

    def test_P74_empty_report_has_no_fabricated_performance(self):
        empty = performance_report([], CostLedger(),
                                   initial_equity_home="1000")
        self.assertEqual(empty["status"], "NO_TRADES")
        self.assertIsNone(empty["overall"]["profit_factor"])
        self.assertIsNone(empty["overall"]["win_rate_pct"])

class OrderIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.journal = IntentJournal("acct-1", directory=self.folder)
        self.store = LifecycleStore("acct-1", directory=self.folder)
        self.transport = FakeTransport()
        self.engine = OrderEngine(
            "acct-1", journal=self.journal, transport=self.transport
        )

    def test_P65_positive_LONG_and_order_trace_sanitized(self):
        plan, rules, conv = inputs(direction="LONG")
        self.engine.audit.emit(
            "SIGNAL", eid=event_id("sig", "signal-long"),
            stage="STRATEGY", outcome="OBSERVED",
            signal_id="signal-long", instrument="EUR_USD",
            direction="LONG", grade="A",
            candle_time="2026-11-15T09:45:00Z"
        )
        result = self.engine.submit(
            plan, rules, conv, signal_id="signal-long", now=NOW
        )
        self.assertTrue(result.protection_verified)
        self.assertEqual(self.transport.posts, 1)
        trace = TradeTrace(
            "acct-1", events=self.engine.audit,
            journal=self.journal, state=self.store
        ).attempt(result.attempt_id)
        self.assertEqual(trace["state"], "TRADE_VERIFIED")
        self.assertEqual(trace["broker_fill_price"], "1.1002")
        self.assertEqual(trace["broker_trade_id"], "2201")
        self.assertEqual(trace["signal_id"], "signal-long")
        self.assertEqual(trace["candle_times"], ["2026-11-15T09:45:00Z"])
        self.assertEqual(len(trace["event_ids"]), 5)
        events = self.engine.audit.read()
        self.assertEqual(
            [x["event_type"] for x in events],
            ["SIGNAL", "RISK", "ORDER_ATTEMPT", "ORDER_RESULT", "TRADE"]
        )
        text = self.engine.audit.path.read_text()
        self.assertNotIn(str(config.API_KEY), text) if config.API_KEY else None
        self.assertNotIn("Bearer ", text)

    def test_P65_positive_SHORT_and_protected_trade(self):
        plan, rules, conv = inputs(direction="SHORT")
        result = self.engine.submit(
            plan, rules, conv, signal_id="signal-short", now=NOW
        )
        self.assertTrue(result.protection_verified)
        self.assertEqual(self.transport.posts, 1)
        order = self.transport.payload["order"]
        self.assertTrue(d(order["units"]) < 0)
        self.assertEqual(order["positionFill"], "OPEN_ONLY")
        self.assertEqual(
            self.journal.get(result.attempt_id)["status"], "FILLED"
        )

    def test_P66_blocked_preflight_zero_posts_and_recovery(self):
        plan, rules, conv = inputs(direction="LONG")
        self.transport.account["account"]["marginAvailable"] = "0"
        from broker.execution_validation import PreflightRejected
        with self.assertRaises(PreflightRejected):
            self.engine.submit(plan, rules, conv, signal_id="blocked-1", now=NOW)
        self.assertEqual(self.transport.posts, 0)
        self.assertEqual(len(self.journal.snapshot()), 0)
        self.assertEqual(
            self.engine.audit.read()[0]["reason_code"],
            "ACCOUNT_RISK_OR_MARGIN_UNVERIFIED"
        )
        self.transport.account["account"]["marginAvailable"] = "8000"
        result = self.engine.submit(
            plan, rules, conv, signal_id="valid-2", now=NOW
        )
        self.assertTrue(result.protection_verified)
        self.assertEqual(self.transport.posts, 1)

    def test_P66_spread_blocks_before_intent_and_resumes_after(self):
        from broker.execution_validation import PreflightRejected
        plan, rules, conv = inputs()
        self.transport.quote["prices"][0]["asks"][0]["price"] = "1.10039"
        with self.assertRaises(PreflightRejected):
            self.engine.submit(plan, rules, conv, signal_id="spread-1", now=NOW)
        self.assertEqual(self.transport.posts, 0)
        self.assertFalse(self.journal.path.exists())
        self.transport.quote["prices"][0]["asks"][0]["price"] = "1.1002"
        result = self.engine.submit(
            plan, rules, conv, signal_id="spread-2", now=NOW
        )
        self.assertEqual(result.outcome.value, "FILLED")

    def test_P67_lost_response_restart_GET_only_no_second_submit(self):
        from broker.practice_transport import BrokerCommunicationError
        plan, rules, conv = inputs()
        self.transport.submit_failure = BrokerCommunicationError("Timeout")
        unknown = self.engine.submit(
            plan, rules, conv, signal_id="restart-1", now=NOW
        )
        self.assertEqual(unknown.outcome.value, "UNKNOWN")
        self.transport.provision_reconciliation("FILLED")
        restarted = OrderEngine(
            "acct-1", journal=IntentJournal("acct-1", directory=self.folder),
            transport=self.transport
        )
        recovered = restarted.reconcile(unknown.attempt_id)
        self.assertEqual(recovered.outcome.value, "FILLED")
        self.assertEqual(self.transport.posts, 1)
        self.assertEqual(
            [x["outcome"] for x in restarted.audit.read()
             if x["event_type"] == "ORDER_RESULT"],
            ["UNKNOWN", "FILLED"]
        )

    def test_P64_live_transport_release_gate_untouched(self):
        from unittest.mock import Mock
        session = Mock()
        transport = PracticeTransport(session=session)
        with patch.object(config, "ORDER_WRITES_ENABLED", True):
            with self.assertRaises(BrokerWriteDisabled):
                transport.submit_market_order({"order": {"type": "MARKET"}})
        session.request.assert_not_called()


    def test_P66_broker_pricing_GET_error_logged_without_POST(self):
        from broker.execution_validation import PreflightRejected
        from broker.practice_transport import BrokerCommunicationError
        plan, rules, conversion = inputs()
        original = self.transport.read
        def blocked(path, *, params=None):
            if path == "/pricing":
                raise BrokerCommunicationError("Synthetic read failure")
            return original(path, params=params)
        with patch.object(self.transport, "read", side_effect=blocked):
            with self.assertRaises(PreflightRejected):
                self.engine.submit(
                    plan, rules, conversion,
                    signal_id="missing-price", now=NOW
                )
        self.assertEqual(self.transport.posts, 0)
        self.assertFalse(self.journal.path.exists())
        self.assertIn(
            "SPREAD_OR_QUOTE_UNVERIFIED",
            [r["reason_code"] for r in self.engine.audit.read()]
        )

    def test_P60_trace_rejects_tampered_broker_fill_price(self):
        from telemetry.trace import TraceError
        plan, rules, conversion = inputs()
        filled = self.engine.submit(
            plan, rules, conversion,
            signal_id="trace-tamper", now=NOW
        )
        raw = json.loads(self.journal.path.read_text())
        raw["attempts"][filled.attempt_id]["fill_price"] = "Bearer SECRET"
        self.journal.path.write_text(json.dumps(raw))
        with self.assertRaises(TraceError):
            TradeTrace("acct-1", events=self.engine.audit,
                       journal=self.journal, state=self.store).attempt(
                           filled.attempt_id
                       )


    def test_P57_audit_failure_after_intent_prevents_broker_mutation(self):
        plan, rules, conv = inputs()
        with patch.object(self.engine.audit, "emit",
                          side_effect=AuditError("disk full")):
            with self.assertRaises(AuditError):
                self.engine.submit(
                    plan, rules, conv, signal_id="audit-failure", now=NOW
                )
        self.assertEqual(self.transport.posts, 0)
        attempts = self.journal.snapshot()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "PREPARED")

    def test_P60_trace_includes_confirmed_final_close_transaction(self):
        from risk.trade_lifecycle import TradeRecord
        from utils.time_tools import iso_utc
        plan, rules, conversion = inputs()
        result = self.engine.submit(
            plan, rules, conversion, signal_id="sig-closed", now=NOW
        )
        self.assertTrue(result.protection_verified)
        request = self.transport.payload["order"]
        rec = TradeRecord(
            trade_id=result.trade_id, attempt_id=result.attempt_id,
            instrument="EUR_USD", direction="LONG",
            grade=plan.grade, entry_price=result.fill_price,
            initial_stop=request["stopLossOnFill"]["price"],
            take_profit=request["takeProfitOnFill"]["price"],
            initial_units=result.filled_units, current_units=d("0"),
            current_stop=request["stopLossOnFill"]["price"],
            risk_pct=plan.reserved_risk_pct, opened_at=NOW,
            closed=True, close_transaction_ids=("300",),
            net_pl_home=d("10"), result_counted=True,
            closed_at=NOW + timedelta(minutes=45)
        )
        self.store.initialize("299", {result.trade_id: rec.to_dict()})
        self.store.mutate(lambda state: state.update({
            "cursor": "300",
            "processed_transactions": ["300"],
            "counted_closures": ["300"],
            "closed_results": {result.trade_id: {
                "transaction_id": "300", "pl": "10",
                "closed_at": iso_utc(rec.closed_at),
                "instrument": "EUR_USD"
            }}
        }))
        trace = TradeTrace(
            "acct-1", events=self.engine.audit,
            journal=self.journal, state=self.store
        ).attempt(result.attempt_id)
        self.assertEqual(trace["state"], "CLOSED_CONFIRMED")
        self.assertEqual(trace["closing_transaction_ids"], ["300"])
        self.assertTrue(trace["closed_confirmed"])

class ObservationAuditTests(unittest.TestCase):
    def test_P59_P61_no_session_code_and_cycle_completion(self):
        with TemporaryDirectory() as folder:
            with (
                patch.object(config, "ACCOUNT_ID", "fake"),
                patch.object(config, "API_KEY", "fake-token"),
                patch.object(config, "CSV_PATH", str(Path(folder) / "csv")),
                patch.object(config, "HEARTBEAT_PATH", str(Path(folder) / "hb.json")),
                patch.object(TradeExecutor, "get_open_trades", return_value=[]),
                patch("main.MarketSessions") as session,
            ):
                session.return_value.can_trade.return_value = False
                status = main.scan_once()
                heartbeat = HeartbeatStore("fake")
                audit = AuditEvents("fake")
                self.assertEqual(status, 0)
                self.assertEqual(heartbeat.read()["state"], "CYCLE_COMPLETE")
                self.assertEqual(heartbeat.read()["broker_state"], "OPEN_TRADES_ONLY")
                self.assertEqual(audit.read()[0]["reason_code"],
                                 "OUTSIDE_ENTRY_SESSION")

    def test_P59_broker_failure_reason_and_heartbeat_BLOCKED(self):
        with TemporaryDirectory() as folder:
            with (
                patch.object(config, "ACCOUNT_ID", "fake-bad"),
                patch.object(config, "API_KEY", "fake-token"),
                patch.object(config, "CSV_PATH", str(Path(folder) / "csv")),
                patch.object(config, "HEARTBEAT_PATH", str(Path(folder) / "hb.json")),
                patch.object(TradeExecutor, "get_open_trades",
                             side_effect=BrokerDataError("timeout")),
            ):
                self.assertEqual(main.scan_once(), 3)
                self.assertEqual(HeartbeatStore("fake-bad").read()["state"], "BLOCKED")
                reasons = [x["reason_code"] for x in AuditEvents("fake-bad").read()]
                self.assertIn("BROKER_SNAPSHOT_UNVERIFIED", reasons)


    def test_P59_strategy_returns_detailed_fixed_rejection_code(self):
        from strategy.strategy import Strategy
        from test_category5 import candles
        strategy = Strategy()
        rejected = strategy.evaluate_signal(
            "EUR_USD", "SHORT", candles(), 1.1020, 1.1008,
            .0010, 1, 1.2, NOW
        )
        self.assertIsNone(rejected)
        self.assertEqual(strategy.last_rejection_code,
                         "TREND_MISMATCH_OR_RESET")



    def test_P61_unfinished_heartbeat_blocks_scan_before_broker_read(self):
        with TemporaryDirectory() as folder:
            with (
                patch.object(config, "ACCOUNT_ID", "orphan-account"),
                patch.object(config, "API_KEY", "fake-token"),
                patch.object(config, "HEARTBEAT_PATH",
                             str(Path(folder) / "heartbeat.json")),
                patch.object(TradeExecutor, "get_open_trades") as read
            ):
                hb = HeartbeatStore("orphan-account")
                hb.start("orphan-cycle", kind="SCAN", now=NOW)
                self.assertEqual(main.scan_once(), 3)
                read.assert_not_called()
                self.assertEqual(hb.read()["state"], "ALIVE")



if __name__ == "__main__":
    unittest.main()
