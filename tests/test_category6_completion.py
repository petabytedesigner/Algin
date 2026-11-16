"""Category 6 completion: audit replay, watchdog and end-to-end fake broker.

These tests NEVER call OANDA. They prove local evidence flow and failure
classification, not broker market correctness or Practice execution.
"""
from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import config
import main
from analytics.conservative_backtest import BacktestError, FillCosts, net_result
from analytics.performance import report
from broker.account_reconciler import AccountReconciler
from broker.order_engine import OrderEngine
from broker.execution_result import Outcome
from risk.trade_lifecycle import TradeRecord
from storage.event_log import AuditError, AuditProjector, EventLog
from storage.heartbeat import Heartbeat, HeartbeatError
from storage.intent_journal import IntentJournal
from storage.lifecycle_state import LifecycleStore, ProcessLease
from strategy.strategy import Strategy

NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
D = Decimal


def audit_fields(event_id="abc", *, event="INTENT_PREPARED", outcome="ATTEMPT"):
    return {
        "event_id": event_id,
        "correlation_id": "signal1",
        "stage": "REQUEST", "event": event, "outcome": outcome,
        "reason_code": "DURABLE_INTENT", "timestamp": NOW,
    }


def read_events(directory):
    path = Path(directory) / "events.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class CsvIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.csv"
        self.log = EventLog(self.directory.name)

    def test_P57_once_replay_is_idempotent_and_unique(self):
        original = self.log.emit_once(**audit_fields())
        self.log.emit_once(**audit_fields())
        self.assertEqual(len(read_events(self.directory.name)), 1)
        self.assertEqual(read_events(self.directory.name)[0]["event_id"],
                         original["event_id"])
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields())

    def test_P57_duplicate_event_id_with_different_evidence_blocks(self):
        self.log.emit_once(**audit_fields())
        with self.assertRaises(AuditError):
            self.log.emit_once(**audit_fields(event="ORDER_SENT"))
        self.assertEqual(len(read_events(self.directory.name)), 1)

    def test_P57_batch_write_and_repeat_same_result(self):
        rows = [audit_fields("a"), audit_fields("b")]
        self.log.emit_many_once(rows)
        self.log.emit_many_once(rows)
        self.assertEqual(len(read_events(self.directory.name)), 2)

    def test_P57_torn_tail_never_silently_repaired(self):
        self.log.emit(**audit_fields())
        with self.path.open("ab") as handle:
            handle.write(b"broken-partial-entry")
        prior = self.path.read_bytes()
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields("later"))
        self.assertEqual(self.path.read_bytes(), prior)

    def test_P57_wrong_header_or_invalid_record_blocks(self):
        self.path.write_text("wrong,header\n", encoding="utf-8")
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields())
        self.path.unlink()
        self.log.emit(**audit_fields())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("not,enough,columns\n")
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields("other"))

    def test_P57_symlink_or_stale_lock_cannot_be_overwritten(self):
        target = Path(self.directory.name) / "foreign.csv"
        target.write_text("unrelated")
        self.path.symlink_to(target)
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields())
        self.assertEqual(target.read_text(), "unrelated")
        self.path.unlink()
        lock = self.path.with_suffix(".lock")
        lock.write_text("orphan")
        with self.assertRaises(AuditError):
            self.log.emit(**audit_fields())
        self.assertEqual(lock.read_text(), "orphan")

    def test_P60_csv_injection_and_unverifiable_event_rejected(self):
        for value in ("=cmd", "URL,formula", "secret\nbearer"):
            with self.subTest(value=value):
                with self.assertRaises(AuditError):
                    self.log.emit(**{**audit_fields(), "detail": value})
        self.assertFalse(self.path.exists())


class HeartbeatWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.heartbeat = Heartbeat(
            str(Path(self.directory.name) / "heartbeat.json")
        )

    def test_P61_fresh_stale_blocked_and_future_timestamp(self):
        with patch("storage.heartbeat.utc_now", return_value=NOW):
            self.heartbeat.write("ACTIVE", cycle_id="run1")
            self.assertEqual(
                self.heartbeat.inspect(now=NOW)["health"], "ACTIVE"
            )
            self.assertEqual(
                self.heartbeat.inspect(
                    now=NOW + timedelta(minutes=10),
                    max_age_seconds=60
                )["health"], "STALE"
            )
            with self.assertRaises(HeartbeatError):
                self.heartbeat.inspect(now=NOW - timedelta(minutes=10))
            self.heartbeat.write(
                "BLOCKED", reason_code="BROKER_UNAVAILABLE", cycle_id="run1"
            )
            self.assertEqual(
                self.heartbeat.inspect(now=NOW)["health"], "BLOCKED"
            )

    def test_P61_corrupt_checksum_missing_and_symlink_fail_closed(self):
        with self.assertRaises(HeartbeatError):
            self.heartbeat.inspect(now=NOW)
        with patch("storage.heartbeat.utc_now", return_value=NOW):
            self.heartbeat.write("ACTIVE", cycle_id="cycle1")
        envelope = json.loads(self.heartbeat.path.read_text())
        envelope["data"]["state"] = "CYCLE_COMPLETE"
        self.heartbeat.path.write_text(json.dumps(envelope))
        with self.assertRaises(HeartbeatError):
            self.heartbeat.inspect(now=NOW)
        self.heartbeat.path.unlink()
        self.heartbeat.path.symlink_to(Path(self.directory.name) / "missing")
        with self.assertRaises(HeartbeatError):
            self.heartbeat.read()

    def test_P61_read_only_cli_returns_failure_for_stale_status(self):
        with (
            patch.object(config, "HEARTBEAT_PATH", str(self.heartbeat.path)),
            patch("storage.heartbeat.utc_now", return_value=NOW),
        ):
            self.heartbeat.write("ACTIVE", cycle_id="run1")
            self.assertEqual(main.heartbeat_check(), 0)
        with patch.object(config, "HEARTBEAT_PATH", str(self.heartbeat.path)):
            self.assertEqual(main.heartbeat_check(), 3)


class SignalReasonAndRuntimeTests(unittest.TestCase):
    def test_P59_strict_rejection_codes_from_actual_strategy(self):
        engine = Strategy()
        self.assertIsNone(engine.evaluate_signal(
            "EUR_USD", "BAD", [], 1.1, 1.09, .001, 1, 2, NOW
        ))
        self.assertEqual(engine.last_rejection_reason, "INVALID_DIRECTION")
        self.assertIsNone(engine.evaluate_signal(
            "EUR_USD", "LONG", [], 1.1, 1.09, .001, 1, 2, NOW
        ))
        self.assertEqual(engine.last_rejection_reason, "INVALID_CANDLES")

    def test_P59_session_flag_not_overridden_by_signal_flag(self):
        from execution.trade_executor import TradeExecutor
        with TemporaryDirectory() as directory:
            logfile = Path(directory) / "events"
            hb = str(Path(directory) / "hb.json")
            with (
                patch.object(config, "API_KEY", "dummy"),
                patch.object(config, "ACCOUNT_ID", "account1"),
                patch.object(config, "CSV_ENABLED", True),
                patch.object(config, "CSV_PATH", str(logfile)),
                patch.object(config, "HEARTBEAT_PATH", hb),
                patch.object(config, "CSV_LOG_SIGNALS", True),
                patch.object(config, "CSV_LOG_SESSION_BLOCK", False),
                patch.object(TradeExecutor, "get_open_trades",
                             return_value=[]),
                patch("main.MarketSessions") as sessions
            ):
                sessions.return_value.can_trade.return_value = False
                self.assertEqual(main.scan_once(), 0)
                self.assertFalse((logfile / "events.csv").exists())
                self.assertEqual(
                    Heartbeat(hb).read()["state"], "BLOCKED"
                )
                config.CSV_LOG_SESSION_BLOCK = True
                self.assertEqual(main.scan_once(), 0)
                rows = read_events(logfile)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reason_code"], "SESSION_BLOCK")

    def test_P61_broken_audit_forces_blocked_heartbeat(self):
        from execution.trade_executor import TradeExecutor
        with TemporaryDirectory() as directory:
            logdir = Path(directory) / "events"
            logdir.mkdir()
            (logdir / "events.csv").write_text("bad,header\n")
            heartbeat_path = str(Path(directory) / "heartbeat.json")
            with (
                patch.object(config, "API_KEY", "dummy"),
                patch.object(config, "ACCOUNT_ID", "account1"),
                patch.object(config, "CSV_ENABLED", True),
                patch.object(config, "CSV_PATH", str(logdir)),
                patch.object(config, "HEARTBEAT_PATH", heartbeat_path),
                patch.object(config, "CSV_LOG_SESSION_BLOCK", True),
                patch.object(TradeExecutor, "get_open_trades",
                             return_value=[]),
                patch("main.MarketSessions") as sessions
            ):
                sessions.return_value.can_trade.return_value = False
                self.assertEqual(main.scan_once(), 3)
                saved = Heartbeat(heartbeat_path).read()
                self.assertEqual(saved["state"], "BLOCKED")
                self.assertEqual(saved["reason_code"], "AUDIT_FAILURE")


class ProjectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = IntentJournal("acct-1", directory=self.directory.name)
        self.log = EventLog(Path(self.directory.name) / "audit")
        self.projector = AuditProjector("acct-1", self.log)
        self.base = {
            "order": {
                "instrument": "EUR_USD", "units": "100",
                "stopLossOnFill": {"price": "1.0980"},
                "takeProfitOnFill": {"price": "1.1060"}
            }
        }

    def _seed(self):
        self.journal.begin(
            signal_id="signal-11", attempt_id="algin-11",
            request=self.base, risk_pct="1", margin_home="100", grade="A"
        )
        self.journal.transition("algin-11", "IN_FLIGHT")
        self.journal.transition("algin-11", "FILLED",
                                trade_id="11", order_id="10",
                                fill_price="1.1000", filled_units="100",
                                protection_verified=True)

    def _record(self):
        return TradeRecord(
            trade_id="11", attempt_id="algin-11", instrument="EUR_USD",
            direction="LONG", grade="A", entry_price=D("1.1000"),
            initial_stop=D("1.0980"), take_profit=D("1.1060"),
            initial_units=D("100"), current_units=D("100"),
            current_stop=D("1.0980"), risk_pct=D("1"),
            opened_at=NOW
        )

    def test_P58_P60_provenance_order_be_partial_close_and_restart(self):
        self._seed()
        record = self._record()
        state = {"account_id": "acct-1", "trades": {"11": record.to_dict()},
                 "closed_results": {}}
        self.projector.project(self.journal.snapshot(), state)
        self.projector.project(self.journal.snapshot(), state)
        rows = read_events(self.log.directory)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {x["event"] for x in rows},
            {"INTENT_PREPARED", "ORDER_FILLED", "PROTECTION_VERIFIED"}
        )
        self.assertEqual({x["correlation_id"] for x in rows}, {"signal-11"})
        self.assertEqual({x["attempt_id"] for x in rows}, {"algin-11"})
        record = replace(record, be_state="CONFIRMED",
                         current_stop=D("1.1000"),
                         partial_phases={"phase1": {
                             "state": "CONFIRMED", "units": "50"
                         }}, current_units=D("50"))
        state["trades"]["11"] = record.to_dict()
        self.projector.project(self.journal.snapshot(), state)
        close_time = NOW + timedelta(minutes=20)
        record = replace(record, closed=True, current_units=D("0"),
                         close_transaction_ids=("105",),
                         result_counted=True, net_pl_home=D("12"),
                         closed_at=close_time)
        state["trades"]["11"] = record.to_dict()
        state["closed_results"]["11"] = {
            "transaction_id": "105", "pl": "12",
            "closed_at": close_time.isoformat().replace("+00:00", "Z"),
            "instrument": "EUR_USD"
        }
        self.projector.project(self.journal.snapshot(), state)
        self.projector.project(self.journal.snapshot(), state)
        rows = read_events(self.log.directory)
        self.assertEqual(
            {x["event"] for x in rows},
            {"INTENT_PREPARED", "ORDER_FILLED", "PROTECTION_VERIFIED",
             "BE_CONFIRMED", "PARTIAL_CONFIRMED", "TRADE_CLOSED"}
        )
        self.assertEqual(len(rows), 6)
        close = next(x for x in rows if x["event"] == "TRADE_CLOSED")
        self.assertEqual(close["transaction_id"], "105")
        self.assertEqual(close["trade_id"], "11")
        self.assertEqual(close["correlation_id"], "signal-11")

    def test_P58_absent_or_unknown_management_not_falsely_success(self):
        self._seed()
        record = replace(self._record(), be_state="UNKNOWN")
        state = {"account_id": "acct-1", "trades": {"11": record.to_dict()},
                 "closed_results": {}}
        self.projector.project(self.journal.snapshot(), state)
        events = read_events(self.log.directory)
        unknown = [x for x in events if x["event"] == "BE_UNKNOWN"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["outcome"], "UNKNOWN")
        self.assertNotIn("BE_CONFIRMED", [x["event"] for x in events])

    def test_P60_wrong_account_unknown_trade_or_invalid_source_blocks(self):
        self._seed()
        with self.assertRaises(AuditError):
            self.projector.project(self.journal.snapshot(), {
                "account_id": "other", "trades": {}, "closed_results": {}
            })
        unknown = replace(self._record(), attempt_id="algin-foreign")
        with self.assertRaises(AuditError):
            self.projector.project(self.journal.snapshot(), {
                "account_id": "acct-1",
                "trades": {"11": unknown.to_dict()},
                "closed_results": {}
            })

    def test_P58_individual_flags_apply_without_false_close_cause(self):
        self._seed()
        record = replace(self._record(), be_state="CONFIRMED",
                         current_stop=D("1.1000"))
        state = {"account_id": "acct-1", "trades": {"11": record.to_dict()},
                 "closed_results": {}}
        with patch.object(config, "CSV_LOG_BE", False):
            self.projector.project(self.journal.snapshot(), state)
        self.assertNotIn(
            "BE_CONFIRMED", [x["event"] for x in read_events(self.log.directory)]
        )
        self.projector.project(self.journal.snapshot(), state)
        self.assertIn(
            "BE_CONFIRMED", [x["event"] for x in read_events(self.log.directory)]
        )


    def _closed_result(self):
        self._seed()
        completed = replace(
            self._record(), closed=True, current_units=D("0"),
            close_transaction_ids=("105",), result_counted=True,
            net_pl_home=D("-15"), closed_at=NOW + timedelta(minutes=20)
        )
        state = {
            "account_id": "acct-1",
            "trades": {"11": completed.to_dict()},
            "closed_results": {
                "11": {
                    "transaction_id": "105", "pl": "-15",
                    "closed_at": "2026-11-15T10:20:00Z",
                    "instrument": "EUR_USD"
                }
            }
        }
        return state

    def test_P58_broker_verified_SL_hit_without_guessing_cause(self):
        state = self._closed_result()
        def broker(tx):
            self.assertEqual(tx, "105")
            return {
                "id": "105", "type": "ORDER_FILL",
                "accountID": "acct-1",
                "reason": "STOP_LOSS_ORDER",
                "tradesClosed": [{"tradeID": "11", "units": "-100",
                                  "realizedPL": "-15"}]
            }
        self.projector.project(
            self.journal.snapshot(), state, transaction_fetcher=broker
        )
        self.projector.project(
            self.journal.snapshot(), state, transaction_fetcher=broker
        )
        rows = read_events(self.log.directory)
        self.assertEqual(len([x for x in rows if x["event"] == "SL_HIT"]), 1)
        self.assertEqual(len([x for x in rows if x["event"] == "TRADE_CLOSED"]), 1)
        sl = next(x for x in rows if x["event"] == "SL_HIT")
        self.assertEqual(sl["transaction_id"], "105")
        self.assertEqual(sl["reason_code"], "STOP_LOSS_ORDER")

    def test_P58_independent_TP_flag_without_generic_trade_flag(self):
        state = self._closed_result()
        proof = {
            "id": "105", "type": "ORDER_FILL", "accountID": "acct-1",
            "reason": "TAKE_PROFIT_ORDER",
            "tradesClosed": [{"tradeID": "11", "units": "-100"}]
        }
        with (
            patch.object(config, "CSV_LOG_TRADES", False),
            patch.object(config, "CSV_LOG_TP_HIT", True)
        ):
            self.projector.project(
                self.journal.snapshot(), state,
                transaction_fetcher=lambda _: proof
            )
        self.assertEqual(
            [x["event"] for x in read_events(self.log.directory)],
            ["TP_HIT"]
        )

    def test_P58_manual_vs_bot_close_not_falsely_inferred(self):
        state = self._closed_result()
        proof = {
            "id": "105", "type": "ORDER_FILL", "accountID": "acct-1",
            "reason": "MARKET_ORDER_TRADE_CLOSE",
            "tradesClosed": [{"tradeID": "11", "units": "-100"}]
        }
        self.projector.project(
            self.journal.snapshot(), state, transaction_fetcher=lambda _: proof
        )
        events = [x["event"] for x in read_events(self.log.directory)]
        self.assertIn("TRADE_CLOSED", events)
        self.assertNotIn("MANUAL_CLOSE", events)
        self.assertNotIn("SYSTEM_CLOSE", events)

    def test_P60_wrong_close_transaction_refused_without_audit_claim(self):
        state = self._closed_result()
        bad = {
            "id": "105", "type": "ORDER_FILL", "accountID": "other",
            "reason": "STOP_LOSS_ORDER",
            "tradesClosed": [{"tradeID": "11"}]
        }
        with self.assertRaises(AuditError):
            self.projector.project(
                self.journal.snapshot(), state, transaction_fetcher=lambda _: bad
            )
        self.assertEqual(read_events(self.log.directory), [])


class NetAccountingTests(unittest.TestCase):
    def test_P71_no_implicit_gross_basis_and_explicit_inclusions(self):
        cost = FillCosts(D("2"), D("1"), D("3"), D("4"))
        with self.assertRaises(BacktestError):
            net_result(gross_pl="100", costs=cost)
        self.assertEqual(
            net_result(gross_pl="100", costs=cost, gross_includes=()),
            D("90")
        )
        self.assertEqual(
            net_result(gross_pl="100", costs=cost,
                       gross_includes=("spread", "slippage")), D("93")
        )
        with self.assertRaises(BacktestError):
            net_result(gross_pl="100", costs=cost, gross_includes="spread")
        with self.assertRaises(BacktestError):
            net_result(gross_pl="100", costs=cost,
                       gross_includes=("spread", "spread"))

    def test_P74_streaks_concentration_and_zero_outcomes(self):
        rows = [
            {"trade_id": "1", "instrument": "EUR_USD",
             "session": "London", "net_pl": "-10"},
            {"trade_id": "2", "instrument": "EUR_USD",
             "session": "London", "net_pl": "-20"},
            {"trade_id": "3", "instrument": "GBP_USD",
             "session": "Asia", "net_pl": "90"},
            {"trade_id": "4", "instrument": "GBP_USD",
             "session": "Asia", "net_pl": "10"},
        ]
        summary = report(rows, starting_equity="1000")
        self.assertEqual(summary["max_loss_streak"], 2)
        self.assertEqual(summary["max_win_streak"], 2)
        self.assertEqual(summary["largest_win"], D("90"))
        self.assertEqual(summary["largest_loss"], D("-20"))
        self.assertEqual(summary["largest_win_share_pct"], D("90"))
        self.assertEqual(summary["median_trade"], D("0"))
        self.assertEqual(summary["net_pl"], D("70"))
        empty = report([], starting_equity="1000")
        self.assertIsNone(empty["largest_win_share_pct"])
        self.assertEqual(empty["max_loss_streak"], 0)

    def test_P74_missing_realized_net_result_stays_rejected(self):
        with self.assertRaises(BacktestError):
            report([{"trade_id": "1", "instrument": "EUR_USD",
                     "session": "London", "net_pl": "NaN"}],
                   starting_equity="1000")
        with self.assertRaises(BacktestError):
            report([{"trade_id": "1", "instrument": "EUR_USD",
                     "session": "London"}], starting_equity="1000")


class FullFakeBrokerCycleTests(unittest.TestCase):
    """Exercise real order coordinator + durable journal + account reconciler
    + lifecycle management + audit projector with two fake HTTP adapters."""

    def test_P64_P67_one_fill_be_partial_close_restart_and_audit(self):
        # Reuse the extensive Category 3/4 fake HTTP protocol contracts.
        from test_category3 import FakeTransport, inputs
        from test_category4 import RecoveryTransport
        with TemporaryDirectory() as directory:
            plan, rules, conversion = inputs()
            initial = FakeTransport()
            journal = IntentJournal("acct-1", directory=directory)
            engine = OrderEngine(
                "acct-1", transport=initial, journal=journal
            )
            result = engine.submit(
                plan, rules, conversion, signal_id="signal-e2e", now=NOW
            )
            self.assertEqual(result.outcome, Outcome.FILLED)
            self.assertTrue(result.protection_verified)
            self.assertEqual(initial.posts, 1)
            request = initial.payload["order"]
            live = RecoveryTransport()
            live.cursor = "100"
            live.trades = [{
                "id": "2201", "instrument": request["instrument"],
                "state": "OPEN", "price": "1.1002",
                "openTime": "2026-11-15T10:00:00Z",
                "initialUnits": request["units"],
                "currentUnits": request["units"],
                "clientExtensions": request["tradeClientExtensions"],
                "stopLossOrder": request["stopLossOnFill"],
                "takeProfitOrder": request["takeProfitOnFill"],
            }]
            store = LifecycleStore("acct-1", directory=directory)
            lease = ProcessLease("acct-1", directory=directory)
            reconciler = AccountReconciler(
                "acct-1", transport=live, journal=journal,
                store=store, lease=lease
            )
            log = EventLog(Path(directory) / "audit")
            projector = AuditProjector("acct-1", log)
            with reconciler:
                first = reconciler.run()
            self.assertFalse(first.blocked_reasons)
            projector.project(journal.snapshot(), store.load())
            record = TradeRecord.from_dict(store.load()["trades"]["2201"])
            from execution.trade_management import TradeManagement
            management = TradeManagement(reconciler)
            favorable_bid = record.entry_price + D("2")*record.initial_risk_distance
            favorable_ask = favorable_bid + D("0.0002")
            with reconciler:
                prepared_be = management.stage_break_even(
                    "2201", rules, bid=favorable_bid, ask=favorable_ask
                )
            self.assertFalse(prepared_be["broker_write_attempted"])
            projector.project(journal.snapshot(), store.load())
            live.cursor = "101"
            live.transactions = [{
                "id": "101", "accountID": "acct-1",
                "type": "STOP_LOSS_ORDER",
                "tradeID": "2201", "price": str(record.entry_price)
            }]
            live.trades[0]["stopLossOrder"]["price"] = str(record.entry_price)
            with reconciler:
                verified_be = reconciler.run()
            self.assertFalse(verified_be.blocked_reasons)
            projector.project(journal.snapshot(), store.load())
            with reconciler:
                prepared_partial = management.stage_partial(
                    "2201", rules, bid=favorable_bid, ask=favorable_ask
                )
            self.assertFalse(prepared_partial["broker_write_attempted"])
            projector.project(journal.snapshot(), store.load())
            units = prepared_partial["units"]
            original = D(request["units"])
            remaining = original - D(units)
            live.cursor = "102"
            live.transactions = [{
                "id": "102", "accountID": "acct-1",
                "type": "ORDER_FILL",
                "tradeReduced": {
                    "tradeID": "2201", "units": units, "realizedPL": "10"
                }
            }]
            live.trades[0]["currentUnits"] = str(remaining)
            with reconciler:
                verified_partial = reconciler.run()
            self.assertFalse(verified_partial.blocked_reasons)
            projector.project(journal.snapshot(), store.load())
            live.cursor = "103"
            live.transactions = [{
                "id": "103", "accountID": "acct-1",
                "type": "ORDER_FILL", "time": "2026-11-15T10:20:00Z",
                "tradesClosed": [{
                    "tradeID": "2201", "units": str(-remaining),
                    "realizedPL": "-15"
                }]
            }]
            live.trades = []
            with reconciler:
                close = reconciler.run()
            self.assertEqual(close.new_closed_trade_ids, ("2201",))
            projector.project(journal.snapshot(), store.load())
            # Restart the projector/reconciler; no trade was re-submitted.
            rebooted = AuditProjector("acct-1", EventLog(log.directory))
            rebooted.project(journal.snapshot(), store.load())
            with reconciler:
                same = reconciler.run()
            self.assertFalse(same.new_closed_trade_ids)
            self.assertEqual(initial.posts, 1)
            events = read_events(log.directory)
            names = {r["event"] for r in events}
            for expected in (
                "INTENT_PREPARED", "ORDER_FILLED",
                "PROTECTION_VERIFIED", "BE_INTENT", "BE_CONFIRMED",
                "PARTIAL_INTENT", "PARTIAL_CONFIRMED", "TRADE_CLOSED"
            ):
                self.assertIn(expected, names)
            self.assertEqual(len(events), len({
                r["event_id"] for r in events
            }))
            self.assertEqual(
                {r["correlation_id"] for r in events}, {"signal-e2e"}
            )
            self.assertEqual(
                next(r for r in events if r["event"] == "TRADE_CLOSED")[
                    "transaction_id"
                ], "103"
            )

    def test_P64_P67_timeout_unknown_never_resubmits_or_claims_success(self):
        from test_category3 import FakeTransport, inputs
        from broker.practice_transport import BrokerCommunicationError
        with TemporaryDirectory() as directory:
            plan, rules, conversion = inputs()
            transport = FakeTransport()
            transport.submit_failure = BrokerCommunicationError("timed out")
            journal = IntentJournal("acct-1", directory=directory)
            engine = OrderEngine("acct-1", transport=transport, journal=journal)
            result = engine.submit(
                plan, rules, conversion, signal_id="unknown-signal", now=NOW
            )
            self.assertEqual(result.outcome, Outcome.UNKNOWN)
            self.assertEqual(transport.posts, 1)
            projector = AuditProjector(
                "acct-1", EventLog(Path(directory) / "audit")
            )
            projector.project(journal.snapshot())
            rows = read_events(Path(directory) / "audit")
            self.assertTrue(any(
                row["outcome"] == "UNKNOWN" for row in rows
            ))
            self.assertFalse(any(
                row["outcome"] == "SUCCESS" for row in rows
            ))
            with self.assertRaises(Exception):
                engine.submit(
                    plan, rules, conversion,
                    signal_id="unknown-signal", now=NOW
                )
            self.assertEqual(transport.posts, 1)


if __name__ == "__main__":
    unittest.main()
