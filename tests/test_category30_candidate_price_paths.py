"""Category 30 research candidate price paths: hypothetical, fail-closed."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from analytics import historical_candidate_price_paths as pathstudy


def bar(at, *, bid_open="1.10000", ask_open="1.10020",
        bid_low="1.09990", bid_high="1.10010",
        ask_low="1.10010", ask_high="1.10030"):
    def member(open_, low, high):
        return {"o": open_, "h": high, "l": low, "c": open_}
    return {
        "time": at.isoformat().replace("+00:00", "Z"),
        "complete": True, "volume": 100,
        "bid": member(bid_open, bid_low, bid_high),
        "ask": member(ask_open, ask_low, ask_high),
        "mid": member("1.10010", "1.10000", "1.10020"),
    }


class CandidateTraceTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2025, 1, 2, 10, tzinfo=timezone.utc)

    def test_same_bar_both_stop_and_target_hit_is_stop_first(self):
        x = bar(self.at, bid_low="1.09800", bid_high="1.10600")
        result = pathstudy.trace_path([x], 0, direction="LONG",
                                          atr="0.001", slippage_pips=0)
        self.assertEqual(result["status"], "RESOLVED_HYPOTHETICAL_PRICE_PATH")
        self.assertEqual(result["close_reason"], "SL")
        self.assertEqual(result["gross_price_path_r_before_commission_financing"], "-1.000000")

    def test_reached_target_and_more_slippage_never_improves_r(self):
        x = bar(self.at, bid_low="1.09980", bid_high="1.10600")
        no_slip = pathstudy.trace_path([x], 0, direction="LONG",
                                           atr="0.001", slippage_pips=0)
        high_slip = pathstudy.trace_path([x], 0, direction="LONG",
                                             atr="0.001", slippage_pips=2)
        self.assertEqual(no_slip["close_reason"], "TP")
        self.assertEqual(high_slip["close_reason"], "TP")
        self.assertGreater(
            Decimal(no_slip["gross_price_path_r_before_commission_financing"]),
            Decimal(high_slip["gross_price_path_r_before_commission_financing"]),
        )

    def test_short_uses_ask_to_exit(self):
        x = bar(self.at, ask_low="1.09400", ask_high="1.10030")
        result = pathstudy.trace_path([x], 0, direction="SHORT",
                                          atr="0.001", slippage_pips=0)
        self.assertEqual(result["close_reason"], "TP")
        self.assertGreater(Decimal(
            result["gross_price_path_r_before_commission_financing"]), 0)

    def test_high_spread_and_gap_fail_without_entry(self):
        x = bar(self.at, ask_open="1.10040", ask_low="1.10030", ask_high="1.10050")
        wide = pathstudy.trace_path([x], 0, direction="LONG",
                                        atr="0.001", slippage_pips=0)
        self.assertEqual(wide["status"], "NO_ENTRY_SPREAD_OVER_CAP")
        normal = bar(self.at + timedelta(minutes=30))
        gap = pathstudy.trace_path([bar(self.at), normal], 1,
                                        direction="SHORT", atr="0.001", slippage_pips=0)
        self.assertEqual(gap["status"], "NO_ENTRY_AT_NEXT_BAR_GAP")

    def test_gap_during_open_path_is_unresolved_not_assumed_loss(self):
        first = bar(self.at)
        later = bar(self.at + timedelta(minutes=30))
        result = pathstudy.trace_path([first, later], 0,
                                          direction="LONG", atr="0.001", slippage_pips=0)
        self.assertEqual(result["status"], "UNRESOLVED_PRICE_HISTORY_GAP")
        self.assertNotIn("gross_price_path_r_before_commission_financing", result)


class CandidatePathIntegrationTests(unittest.TestCase):
    def test_hypothetical_report_never_reports_broker_pnl(self):
        at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        candles = [bar(at + timedelta(minutes=15 * i)) for i in range(65)]
        decision = candles[60]["time"]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2025-01.json").write_text(json.dumps({
                "candidate_details": [{
                    "setup_id": "algin-setup-fixture", "direction": "LONG",
                    "grade": "B", "decision_utc": decision,
                }],
            }))
            signals = {
                "candidate_decisions": 1,
                "source_run_id": 45645175399,
                "monthly_archive_sha256": "fixture",
            }
            with (patch.object(pathstudy, "MONTHS", ("2025-01",)),
                  patch.object(pathstudy, "inspect_signals", return_value=signals)):
                result = pathstudy.analyze({"instrument": "EUR_USD", "granularity": "M15",
                                            "candles": candles}, root)
        self.assertEqual(result["candidates"], 1)
        self.assertFalse(result["broker_verified"])
        self.assertFalse(result["realistic_net_pnl_calculated"])
        self.assertEqual(result["analysis_scope"],
                         "ATR_ONLY_CANDIDATE_PRICE_PATH_NOT_PRODUCTION_REPLAY")


if __name__ == "__main__":
    unittest.main()
