from __future__ import annotations

import unittest

import futu as ft

from fundamental_analysis import pr_screener


class FilterRow:
    def __init__(self, *, net_profit=100.0, operating_cash_flow=200.0, growth=25.0):
        self.stock_code = "HK.TEST"
        self.stock_name = "Test"
        self.pe_ttm = 2.0
        self.__dict__.update({
            ("return_on_equity_rate", "annual"): 10.0,
            ("equity_multiplier", "annual"): 2.0,
            ("net_profit", "annual"): net_profit,
            ("operating_cash_flow_ttm", "annual"): operating_cash_flow,
            ("net_profix_growth", "annual"): growth,
        })


class PrScreenerTest(unittest.TestCase):
    def test_growth_is_requested_without_filtering(self):
        filters = pr_screener.build_filters("HK", ft)
        growth_filter = next(
            item for item in filters
            if item.stock_field == ft.StockField.NET_PROFIX_GROWTH
        )

        self.assertIsInstance(growth_filter, ft.FinancialFilter)
        self.assertTrue(growth_filter.is_no_filter)
        self.assertIsNone(growth_filter.filter_min)
        self.assertIsNone(growth_filter.filter_max)

    def test_candidate_calculates_cash_coverage_and_growth_adjusted_pr(self):
        candidate = pr_screener.candidate_from_filter_row(FilterRow(), "HK")

        self.assertEqual(candidate["cash_coverage"], 2.0)
        self.assertEqual(candidate["pr_growth_adjusted"], 0.16)

    def test_candidate_tolerates_missing_cash_coverage_inputs(self):
        candidate = pr_screener.candidate_from_filter_row(
            FilterRow(net_profit=0, operating_cash_flow=None), "HK"
        )

        self.assertIsNone(candidate["cash_coverage"])

    def test_candidate_tolerates_total_profit_decline(self):
        candidate = pr_screener.candidate_from_filter_row(FilterRow(growth=-100), "HK")

        self.assertIsNone(candidate["pr_growth_adjusted"])


if __name__ == "__main__":
    unittest.main()
