import unittest
from decimal import Decimal

from analytics.performance import AnalyticsError, conservative_exit, summarize


class PerformanceTests(unittest.TestCase):
    def test_same_bar_stop_wins_for_long_and_short(self):
        a = conservative_exit(direction="LONG", entry="100", stop="98",
                              target="104", open_price="100", low="97",
                              high="105", close="102", price_side="BID")
        b = conservative_exit(direction="SHORT", entry="100", stop="102",
                              target="96", open_price="100", low="95",
                              high="103", close="99", price_side="ASK")
        self.assertEqual(a, ("STOP", Decimal("98")))
        self.assertEqual(b, ("STOP", Decimal("102")))

    def test_gap_through_stop_uses_worse_execution(self):
        a = conservative_exit(direction="LONG", entry="100", stop="98",
                              target="104", open_price="97", low="96",
                              high="100", close="99", price_side="BID")
        self.assertEqual(a, ("STOP_GAP", Decimal("97")))

    def test_midpoint_cannot_be_used_for_executable_exit(self):
        with self.assertRaises(AnalyticsError):
            conservative_exit(direction="SHORT", entry="100", stop="102",
                              target="96", open_price="100", low="95",
                              high="103", close="99", price_side="MID")

    def test_net_profit_and_drawdown_after_costs(self):
        ledger = [{"instrument": "EUR_USD", "gross_price_pl_home": "100",
                   "spread_cost_home": "0", "commission_home": "3"},
                  {"instrument": "GBP_USD", "gross_price_pl_home": "-50",
                   "financing_home": "2"},
                  {"instrument": "EUR_USD", "gross_price_pl_home": "25",
                   "slippage_home": "1"}]
        report = summarize(ledger, "10000")
        self.assertEqual(report["net_profit_home"], "69")
        self.assertEqual(report["wins"], 2)
        self.assertEqual(report["losses"], 1)
        self.assertEqual(report["max_drawdown_home"], "52")
        self.assertEqual(report["net_by_instrument"]["EUR_USD"], "121")

    def test_nan_and_negative_costs_fail(self):
        with self.assertRaises(AnalyticsError):
            summarize([{"instrument":"EUR_USD","gross_price_pl_home":"NaN"}], "10000")
        with self.assertRaises(AnalyticsError):
            summarize([{"instrument":"EUR_USD","gross_price_pl_home":"3",
                        "spread_cost_home":"-1"}], "10000")


if __name__ == "__main__":
    unittest.main()
