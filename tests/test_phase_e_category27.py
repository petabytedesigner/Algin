"""Category 27: decision provenance, finite numeric evidence, zero broker writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import json
import unittest

from storage.decision_trace import DecisionTrace, TraceError


NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


def sample(**changes):
    record = {
        "cycle_id": "cycle123", "instrument": "EUR_USD",
        "direction": "LONG", "candle_time": "2026-11-18T11:45:00Z",
        "timestamp": NOW, "decision": "REJECTED",
        "reason_code": "TREND_OR_RESET", "strategy_version": "bos-retest-v1",
        "inputs": {"ema_fast": "1.1020", "ema_slow": "1.1010",
                   "atr": "0.0010", "close": "1.1025"},
        "metrics": {},
        "policy": {"ema_fast_period": 20, "ema_slow_period": 50,
                   "atr_period": 14, "grade_a_min": 80,
                   "grade_b_min": 60, "risk_grade_a_pct": 1},
        "risk_status": "NOT_EVALUATED_UNRECONCILED",
        "grade": None, "score": None, "signal_id": None,
        "setup_id": None, "attempt_id": None,
        "trade_id": None, "transaction_id": None,
    }
    record.update(changes)
    return record


class Category27TraceTests(unittest.TestCase):
    def test_durable_round_trip_and_no_secret_or_account_value(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            result = trace.append(**sample())
            self.assertEqual(result["decision"], "REJECTED")
            rows = trace.read_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["inputs"]["atr"], "0.0010")
            raw = (Path(root) / "decision_trace.jsonl").read_text()
            self.assertNotIn("Bearer", raw)
            self.assertNotIn("OANDA_API_TOKEN", raw)
            self.assertEqual(rows[0]["cycle_id"], "cycle123")

    def test_idempotent_same_decision_but_conflict_rejected(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            trace.append(**sample())
            trace.append(**sample())
            self.assertEqual(len(trace.read_all()), 1)
            with self.assertRaises(TraceError):
                trace.append(**sample(inputs={"atr": "0.0020"}))
            self.assertEqual(len(trace.read_all()), 1)

    def test_reject_secret_keys_nonfinite_free_text_and_unknown_fields(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            bad = (
                {"inputs": {"api_key": "secret"}},
                {"inputs": {"atr": "NaN"}},
                {"inputs": {"ema_fast": True}},
                {"policy": {"OANDA_API_TOKEN": "secret"}},
                {"reason_code": "Bearer SECRET"},
                {"unexpected_raw_response": {"token": "secret"}},
                {"metrics": {"trend_strength": 1.5}},
                {"score": float("inf")},
                {"attempt_id": "unverified-attempt"},
                {"trade_id": "123"},
                {"transaction_id": "456"},
            )
            for case in bad:
                with self.subTest(case=case), self.assertRaises(TraceError):
                    trace.append(**sample(**case))
            self.assertFalse(trace.path.exists())

    def test_torn_tail_and_tampered_entry_block_further_trace(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            trace.append(**sample())
            original = trace.path.read_bytes()
            trace.path.write_bytes(original[:-1])
            with self.assertRaises(TraceError):
                trace.append(**sample(direction="SHORT"))
            self.assertEqual(trace.path.read_bytes(), original[:-1])
            trace.path.write_bytes(original.replace(b'"atr":"0.0010"', b'"atr":"9.0000"'))
            with self.assertRaises(TraceError):
                trace.read_all()

    def test_symlink_and_lock_are_not_overwritten(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            outside = Path(root) / "outside"
            outside.write_text("sensitive")
            trace.path.symlink_to(outside)
            with self.assertRaises(TraceError):
                trace.append(**sample())
            self.assertEqual(outside.read_text(), "sensitive")
            trace.path.unlink()
            trace.lock.write_text("other-writer")
            with self.assertRaises(TraceError):
                trace.append(**sample())
            self.assertEqual(trace.lock.read_text(), "other-writer")
            self.assertFalse(trace.path.exists())

    def test_valid_accepted_signal_replays_links_without_claiming_execution(self):
        with TemporaryDirectory() as root:
            trace = DecisionTrace(root)
            row = trace.append(**sample(
                decision="OBSERVED", reason_code="OBSERVE_ONLY",
                score=75.5, grade="B",
                signal_id="algin-signal-" + "a" * 40,
                setup_id="algin-setup-" + "b" * 40,
                metrics={"trend_strength": .7, "retest_quality": .8,
                         "in_reset_zone": False},
            ))
            self.assertEqual(row["score"], "75.5")
            self.assertEqual(row["metrics"]["retest_quality"], "0.8")
            self.assertFalse(row["metrics"]["in_reset_zone"])
            self.assertIsNone(row["attempt_id"])
            self.assertIsNone(row["transaction_id"])
            self.assertEqual(trace.read_all()[0]["trace_id"], row["trace_id"])


class Category27ScanTests(unittest.TestCase):
    def test_read_only_scan_records_both_directions_without_broker_writes(self):
        import config
        import main
        from risk.scoring_engine import ScoringConfig
        from risk.trade_plan import InstrumentRules
        from storage.heartbeat import Heartbeat
        from strategy.strategy import StrategySignal

        candles = []
        for count in range(50):
            timestamp = NOW - timedelta(minutes=(50 - count) * 15)
            candles.append({
                "time": timestamp.isoformat().replace("+00:00", "Z"),
                "complete": True, "open": 1.1, "high": 1.1006,
                "low": 1.0996, "close": 1.1002,
            })

        class SyntheticStrategy:
            TRACE_VERSION = "bos-retest-v1"

            def __init__(self):
                self.ema_engine = Mock(ema=Mock(return_value=1.1001))
                self.atr_engine = Mock(atr=Mock(return_value=.0008))
                self.scoring = Mock(config=ScoringConfig())
                self.last_rejection_reason = None
                self.last_evaluation_metrics = None
                self.last_evaluation_score = None
                self.last_evaluation_grade = None

            def evaluate_signal(self, instrument, direction, *args):
                if direction == "LONG":
                    return StrategySignal(
                        instrument, direction, 75.0, "B",
                        {"trend_strength": .75, "in_reset_zone": False},
                        "BOS", NOW, "algin-setup-" + "a" * 40,
                    )
                self.last_rejection_reason = "TREND_OR_RESET"
                self.last_evaluation_metrics = None
                return None

        spec = InstrumentRules(
            "EUR_USD", "CURRENCY", -4, 5, 0,
            __import__("decimal").Decimal("1"),
            __import__("decimal").Decimal("1000000"),
            __import__("decimal").Decimal("0"),
            __import__("decimal").Decimal("0.02"),
        )
        with TemporaryDirectory() as root:
            log_dir = str(Path(root) / "csv")
            hb_path = str(Path(root) / "heartbeat.json")
            handler = Mock()
            handler.get_account_instruments.return_value = {"EUR_USD": spec}
            handler.get_candles.return_value = candles
            handler.get_price.return_value = {"bid": 1.1, "ask": 1.1002}
            sessions = Mock()
            sessions.can_trade.return_value = True
            sessions.instrument_tradeable.return_value = True

            with (patch.object(config, "API_KEY", "synthetic-token"),
                  patch.object(config, "ACCOUNT_ID", "acct-1"),
                  patch.object(config, "CSV_ENABLED", True),
                  patch.object(config, "CSV_PATH", log_dir),
                  patch.object(config, "HEARTBEAT_PATH", hb_path),
                  patch.object(main, "INSTRUMENTS", ("EUR_USD",)),
                  patch("main.utc_now", return_value=NOW),
                  patch("main.MarketSessions", return_value=sessions),
                  patch("main.DataHandler", return_value=handler),
                  patch("main.Strategy", SyntheticStrategy),
                  patch("main.CandleGate") as gate,
                  patch("main.TradeExecutor") as executor,
                  patch("main.build_news_filter") as news):
                gate.return_value.claim.return_value = True
                executor.return_value.get_open_trades.return_value = []
                news.return_value.block_trading.return_value = None
                self.assertEqual(main.scan_once(), 0)
                executor.return_value.get_open_trades.assert_called_once()
                self.assertFalse(executor.return_value.submit.called)
                self.assertEqual(Heartbeat(hb_path).read()["state"], "CYCLE_COMPLETE")

                # An unavailable decision log blocks the scan; it cannot be
                # treated as a successful, invisible observation.
                with patch("main.DecisionTrace") as failed_trace:
                    failed_trace.return_value.append.side_effect = TraceError("disk full")
                    self.assertEqual(main.scan_once(), 3)
                    blocked = Heartbeat(hb_path).read()
                    self.assertEqual(blocked["state"], "BLOCKED")
                    self.assertEqual(blocked["reason_code"], "AUDIT_FAILURE")
                    self.assertFalse(executor.return_value.submit.called)

            rows = DecisionTrace(log_dir).read_all()
            self.assertEqual(len(rows), 2)
            by_direction = {r["direction"]: r for r in rows}
            self.assertEqual(by_direction["LONG"]["decision"], "OBSERVED")
            self.assertEqual(by_direction["LONG"]["score"], "75.0")
            self.assertIsNotNone(by_direction["LONG"]["signal_id"])
            self.assertEqual(by_direction["SHORT"]["reason_code"], "TREND_OR_RESET")
            self.assertEqual(by_direction["LONG"]["cycle_id"],
                             by_direction["SHORT"]["cycle_id"])
            self.assertIsNone(by_direction["LONG"]["attempt_id"])
            self.assertEqual(by_direction["LONG"]["policy"]["grade_b_min"], "60.0")


if __name__ == "__main__":
    unittest.main()
