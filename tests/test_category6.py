"""Category 6: synthetic audit, heartbeat, stop-first, costs, reporting.

No real OANDA mutation is permitted by these tests.
"""
from datetime import datetime,timezone
from decimal import Decimal as D
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import csv
import json
import unittest

from analytics.conservative_backtest import (
    BacktestError, FillCosts, stop_first_bar, net_result
)
from analytics.performance import report
from storage.event_log import EventLog, AuditError, FIELDS
from storage.heartbeat import Heartbeat,HeartbeatError

NOW=datetime(2026,11,15,10,tzinfo=timezone.utc)

class AuditTests(unittest.TestCase):
    def test_P57_P60_attempt_success_failure_unknown_separate_and_correlated(self):
        with TemporaryDirectory() as tmp:
            log=EventLog(tmp)
            for i,status in enumerate(("ATTEMPT","SUCCESS","FAILURE","UNKNOWN")):
                log.emit(event_id="evt"+str(i),correlation_id="signal1",
                         stage="RESULT",event="ORDER",outcome=status,
                         reason_code="BROKER_OUTCOME",attempt_id="attempt1",
                         timestamp=NOW)
            with (Path(tmp)/"events.csv").open(newline="") as handle:
                reader=csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames),FIELDS)
                rows=list(reader)
            self.assertEqual([r["outcome"] for r in rows],
                             ["ATTEMPT","SUCCESS","FAILURE","UNKNOWN"])
            self.assertEqual({r["correlation_id"] for r in rows},{"signal1"})

    def test_P60_no_secret_or_csv_injection_in_audit_fields(self):
        with TemporaryDirectory() as tmp:
            log=EventLog(tmp)
            for value in ("Bearer secret","=HYPERLINK(1)","token\n", "abc,def"):
                with self.assertRaises(AuditError):
                    log.emit(event_id="a",correlation_id="b",stage="REQUEST",
                             event="ORDER",outcome="ATTEMPT",
                             reason_code=value,timestamp=NOW)
            self.assertFalse((Path(tmp)/"events.csv").exists())

    def test_P57_incompatible_existing_schema_fail_closed(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"events.csv"
            path.write_text("bad,header\n")
            with self.assertRaises(AuditError):
                EventLog(tmp).emit(event_id="a",correlation_id="b",
                                   stage="BLOCK",event="ENTRY_DENIED",
                                   outcome="BLOCKED",reason_code="NEWS_UNKNOWN",
                                   timestamp=NOW)
            self.assertEqual(path.read_text(),"bad,header\n")

    def test_P61_atomic_heartbeat_states(self):
        with TemporaryDirectory() as tmp:
            hb=Heartbeat(str(Path(tmp)/"hb.json"))
            for state in ("ACTIVE","CYCLE_COMPLETE","BLOCKED"):
                hb.write(state,reason_code="NEWS_UNKNOWN",cycle_id="cycle1")
                envelope=json.loads(hb.path.read_text())
                self.assertEqual(envelope["data"]["state"],state)
            with self.assertRaises(HeartbeatError):
                hb.write("BROKER_ORDER_SENT")

class SimulationTests(unittest.TestCase):
    def test_P70_stop_first_long_short_double_touch(self):
        self.assertEqual(stop_first_bar("LONG",low="98",high="105",
                                        stop="99",target="104"),"SL")
        self.assertEqual(stop_first_bar("SHORT",low="95",high="102",
                                        stop="101",target="96"),"SL")
        self.assertEqual(stop_first_bar("LONG",low="100",high="105",
                                        stop="99",target="104"),"TP")
        self.assertEqual(stop_first_bar("SHORT",low="95",high="100",
                                        stop="101",target="96"),"TP")
        self.assertEqual(stop_first_bar("LONG",low="100",high="103",
                                        stop="99",target="104"),"OPEN")

    def test_P71_costs_deducted_once_and_no_negative_or_nan(self):
        costs=FillCosts(D("2"),D("1"),D("3"),D("4"))
        self.assertEqual(net_result(gross_pl="100",costs=costs),D("90"))
        self.assertEqual(net_result(gross_pl="100",costs=costs,
                                    gross_includes=("spread","slippage")),D("93"))
        with self.assertRaises(BacktestError):
            net_result(gross_pl="100",costs=costs,
                       gross_includes=("spread","spread"))
        with self.assertRaises(BacktestError):
            FillCosts(commission=D("-1")).total()
        with self.assertRaises(BacktestError):
            net_result(gross_pl="NaN",costs=costs)

    def test_P74_metrics_and_breakdown(self):
        result=report([
            {"trade_id":"1","instrument":"EUR_USD","session":"London","net_pl":"100"},
            {"trade_id":"2","instrument":"EUR_USD","session":"New_York","net_pl":"-50"},
            {"trade_id":"3","instrument":"GBP_USD","session":"London","net_pl":"25"},
        ],starting_equity="1000")
        self.assertEqual(result["trades"],3)
        self.assertEqual(result["net_pl"],D("75"))
        self.assertEqual(result["win_rate_pct"],D(2)*100/3)
        self.assertEqual(result["profit_factor"],D("2.5"))
        self.assertEqual(result["expectancy"],D("25"))
        self.assertEqual(result["max_drawdown_home"],D("50"))
        self.assertEqual(result["by_session"]["London"],D("125"))
        self.assertEqual(result["by_instrument"]["EUR_USD"],D("50"))

    def test_P74_duplicate_or_incomplete_results_rejected(self):
        sample={"trade_id":"1","instrument":"EUR_USD",
                "session":"London","net_pl":"5"}
        with self.assertRaises(BacktestError):
            report([sample,sample],starting_equity="1000")
        with self.assertRaises(BacktestError):
            report([{"trade_id":"2","net_pl":"3"}],starting_equity="1000")
        empty=report([],starting_equity="1000")
        self.assertIsNone(empty["win_rate_pct"])
        self.assertIsNone(empty["expectancy"])

if __name__=="__main__":
    unittest.main()
