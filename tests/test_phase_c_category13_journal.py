"""Category 13: offline forward journal integrity and verified-only P/L."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest

from broker.financial_ledger import initial_ledger, record_transaction
from operations.forward_journal import (
    ForwardJournal, ForwardEvidenceError, read_run, report_runs
)
from operations.forward_performance import (
    verified_realized_performance, ForwardPerformanceError,
)
from risk.trade_lifecycle import TradeRecord

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def demo_closed_state():
    opened = TradeRecord(
        trade_id="11", attempt_id="algin-offline-01",
        instrument="EUR_USD", direction="LONG", grade="A",
        entry_price=Decimal("1.10000"), initial_stop=Decimal("1.09900"),
        take_profit=Decimal("1.10300"), initial_units=Decimal("100"),
        current_units=Decimal("0"), current_stop=Decimal("1.09900"),
        risk_pct=Decimal("0.1"), opened_at=NOW,
        closed=True, closed_at=NOW+timedelta(minutes=15),
        close_transaction_ids=("103",), result_counted=True,
        net_pl_home=Decimal("5"),
    )
    ledger = initial_ledger("100")
    record_transaction(ledger, {
        "id": "101", "accountID": "acct-offline", "type": "ORDER_FILL",
        "pl": "0", "financing": "0", "commission": "0",
        "guaranteedExecutionFee": "0",
        "tradeOpened": {
            "tradeID": "11", "units": "100", "financing": "0",
            "guaranteedExecutionFee": "0",
        },
    })
    record_transaction(ledger, {
        "id": "103", "accountID": "acct-offline", "type": "ORDER_FILL",
        "pl": "5", "financing": "0", "commission": "0",
        "guaranteedExecutionFee": "0",
        "tradesClosed": [{
            "tradeID": "11", "units": "-100", "realizedPL": "5",
            "financing": "0", "guaranteedExecutionFee": "0",
        }],
    })
    return {
        "trades": {"11": opened.to_dict()},
        "closed_results": {"11": {
            "transaction_id": "103", "pl": "5",
            "closed_at": "2026-11-17T12:15:00Z",
            "instrument": "EUR_USD",
        }},
        "financial_ledger": ledger,
        "processed_transactions": ["101", "103"],
        "cursor": "103",
    }


class ForwardJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def new(self):
        return ForwardJournal(self.folder)

    def test_new_run_is_unique_fsynced_and_verifiable(self):
        log=self.new()
        try:
            log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
            log.emit("SAMPLE", "CYCLE_COMPLETE", pid=1221,
                     age_seconds=4, cycle_id="cycle-1",
                     now=NOW+timedelta(seconds=30))
            log.emit("STOP", "SAMPLE_WINDOW_COMPLETE", now=NOW+timedelta(seconds=31))
        finally:
            log.close()
        rows=read_run(log.path)
        self.assertEqual([x["event"] for x in rows],
                         ["START", "SAMPLE", "STOP"])
        self.assertEqual([x["seq"] for x in rows], [0, 1, 2])
        self.assertEqual(rows[1]["pid"], 1221)
        summary=report_runs(self.folder)
        self.assertEqual(summary["completed_runs"], 1)
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["completed_cycle_samples"], 1)
        self.assertEqual(summary["broker_verified_cycles"], 0)
        self.assertEqual(summary["broker_validation_status"], "NOT_ATTESTED_BY_HEARTBEAT")
        self.assertEqual(summary["uptime_24h"], "NOT_MEASURED_ON_DEVICE")
        self.assertEqual(summary["performance"], "NOT_VERIFIED")

    def test_missing_history_reports_no_trade_statistics(self):
        result=report_runs(self.folder/"missing")
        self.assertEqual(result["status"], "NO_OBSERVATION_EVIDENCE")
        self.assertEqual(result["performance"], "NOT_VERIFIED")

    def test_torn_jsonl_tail_fails_closed(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.close()
        with log.path.open("ab") as output:
            output.write(b'{"uncommitted":')
        with self.assertRaises(ForwardEvidenceError):
            read_run(log.path)

    def test_tampered_checksum_rejected(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.close()
        rows=log.path.read_text().splitlines()
        record=json.loads(rows[0])
        record["reason"]="FALSE_SUCCESS"
        log.path.write_text(json.dumps(record)+"\n")
        with self.assertRaises(ForwardEvidenceError):
            read_run(log.path)

    def test_out_of_sequence_or_removed_events_rejected(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.emit("LIVENESS", "ACTIVE", cycle_id="cycle-1", now=NOW)
        log.emit("STOP", "SAMPLE_WINDOW_COMPLETE", now=NOW)
        log.close()
        rows=log.path.read_text().splitlines()
        log.path.write_text(rows[0]+"\n"+rows[-1]+"\n")
        with self.assertRaises(ForwardEvidenceError):
            read_run(log.path)

    def test_reject_post_terminal_append(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.emit("BLOCKED", "BROKER_UNAVAILABLE", now=NOW)
        with self.assertRaises(ForwardEvidenceError):
            log.emit("SAMPLE", "ACTIVE", now=NOW)
        log.close()

    def test_unfinished_run_has_explicit_interruption_flag(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.close()
        value=report_runs(self.folder)
        self.assertEqual(value["interrupted_runs"], 1)
        self.assertEqual(value["status"], "UNVERIFIED_INTERRUPTED_RUNS")

    def test_blocked_and_gated_samples_not_counted_healthy(self):
        log=self.new()
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.emit("GATE", "SESSION_BLOCK", now=NOW)
        log.emit("BLOCKED", "BOT_BROKER_UNAVAILABLE", now=NOW)
        log.close()
        report=report_runs(self.folder)
        self.assertEqual(report["gated_samples"], 1)
        self.assertEqual(report["healthy_samples"], 0)
        self.assertEqual(report["blocked_runs"], 1)

    def test_symlink_and_unexpected_file_block_history(self):
        fake=self.folder/"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jsonl"
        target=self.folder/"external"
        target.write_text("not a journal")
        fake.symlink_to(target)
        with self.assertRaises(ForwardEvidenceError):
            read_run(fake)
        with self.assertRaises(ForwardEvidenceError):
            report_runs(self.folder)

    def test_duplicate_explicit_run_id_cannot_overwrite(self):
        ident="a"*32
        first=ForwardJournal(self.folder, run_id=ident)
        first.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        first.close()
        with self.assertRaises(ForwardEvidenceError):
            ForwardJournal(self.folder, run_id=ident)

    def test_event_reason_rejects_sensitive_arbitrary_text(self):
        log=self.new()
        with self.assertRaises(ForwardEvidenceError):
            log.emit("START", "Bearer API_TOKEN_VALUE", now=NOW)
        log.emit("START", "SUPERVISOR_LAUNCHING", now=NOW)
        log.close()

    def test_no_duplicate_fake_execution_status_allowed(self):
        log=self.new()
        with self.assertRaises(ForwardEvidenceError):
            log.emit("TRADE_FILLED", "BROKER_PROVEN", now=NOW)
        log.close()


class VerifiedRealizedPerformanceTests(unittest.TestCase):
    def test_no_proven_state_never_claims_pnl(self):
        value=verified_realized_performance(None)
        self.assertIsNone(value["win_rate_pct"])
        self.assertIsNone(value["account_nav_drawdown_pct"])
        self.assertEqual(value["status"], "NO_PERSISTED_BROKER_EVIDENCE")

    def test_broker_ledger_closed_trade_net_and_win_rate(self):
        value=verified_realized_performance(demo_closed_state())
        self.assertEqual(value["status"], "VERIFIED_REALIZED_ONLY")
        self.assertEqual(value["net_pl_home"], "5")
        self.assertEqual(value["trades"], 1)
        self.assertEqual(value["win_rate_pct"], "100")
        self.assertEqual(value["expectancy_home"], "5")
        self.assertEqual(value["account_nav_drawdown_status"], "NOT_VERIFIED")
        self.assertIsNone(value["account_nav_drawdown_pct"])

    def test_missing_closure_transaction_blocks_reporting(self):
        state=demo_closed_state()
        state["processed_transactions"].remove("103")
        with self.assertRaises(ForwardPerformanceError):
            verified_realized_performance(state)

    def test_wrong_closed_result_id_blocks_reporting(self):
        state=demo_closed_state()
        state["closed_results"]["11"]["transaction_id"]="102"
        with self.assertRaises(ForwardPerformanceError):
            verified_realized_performance(state)

    def test_financial_ledger_missing_commission_refuses_complete_net(self):
        state=demo_closed_state()
        del state["financial_ledger"]["events"]["103"]["components"]["commission"]
        with self.assertRaises(ForwardPerformanceError):
            verified_realized_performance(state)

    def test_ledger_close_without_open_never_counts_win(self):
        state=demo_closed_state()
        del state["financial_ledger"]["events"]["101"]
        value=verified_realized_performance
        with self.assertRaises(ForwardPerformanceError):
            value(state)


if __name__ == "__main__":
    unittest.main()
