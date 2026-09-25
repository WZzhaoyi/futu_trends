from __future__ import annotations

import configparser
import types
import unittest
from unittest import mock

from fundamental_analysis import deep_value_screener
from fundamental_analysis import futu_fundamental_screener as fs
from fundamental_analysis import growth_value_screener
from fundamental_analysis import pr_screener
from fundamental_analysis import sepa_screener


def statement_data(periods, currency="HKD", accounting_standards="IFRS"):
    """构造 SDK 原生 report_list；故意不给可用于适配的展示名。"""
    field_ids = sorted({field_id for _, _, _, values in periods for field_id in values})
    return {
        "structure_list": [
            {"field_id": field_id, "display_name": f"ignored-{field_id}"}
            for field_id in field_ids
        ],
        "report_list": [
            {
                "date_time_str": date,
                "fiscal_year": int(date[:4]),
                "financial_type": financial_type,
                "period_text": period_text,
                "currency_code": currency,
                "accounting_standards": accounting_standards,
                "item_list": [
                    {
                        "field_id": field_id,
                        "display_name": "also-ignored",
                        "data": value,
                    }
                    for field_id, value in values.items()
                ],
            }
            for date, financial_type, period_text, values in periods
        ],
        "next_key": "-1",
    }


INCOME = statement_data([
    ("2026-06-30", 5, "2026/H1", {5045: 70, 5002: 550, 5010: 330}),
    ("2026-03-31", 1, "2026/Q1", {5045: 30, 5002: 250, 5010: 100}),
    ("2025-12-31", 7, "2025/FY", {5045: 120, 5002: 1000, 5010: 400}),
    ("2025-06-30", 5, "2025/H1", {5045: 45, 5002: 400, 5010: 220}),
    ("2025-03-31", 1, "2025/Q1", {5045: 20, 5002: 200, 5010: 70}),
    ("2024-12-31", 7, "2024/FY", {5045: 80, 5002: 800, 5010: 280}),
])
BALANCE = statement_data([
    ("2026-06-30", 5, "2026/H1", {
        5001: 1100, 5109: 550, 5002: 450, 5061: 200,
        5091: 90, 5003: 280, 5060: 220,
    }),
    ("2026-03-31", 1, "2026/Q1", {
        5001: 1000, 5109: 500, 5002: 400, 5061: 200,
        5091: 100, 5003: 250, 5060: 200,
    }),
    ("2025-12-31", 7, "2025/FY", {
        5001: 1000, 5109: 500, 5002: 400, 5061: 200,
        5091: 110, 5003: 240, 5060: 210,
    }),
    ("2025-06-30", 5, "2025/H1", {
        5001: 950, 5109: 430, 5002: 330, 5061: 190,
        5091: 140, 5003: 210, 5060: 215,
    }),
    ("2025-03-31", 1, "2025/Q1", {
        5001: 900, 5109: 400, 5002: 300, 5061: 200,
        5091: 150, 5003: 200, 5060: 220,
    }),
    ("2024-12-31", 7, "2024/FY", {
        5001: 900, 5109: 400, 5002: 300, 5061: 200,
        5091: 160, 5003: 200, 5060: 220,
    }),
])
CASHFLOW = statement_data([
    ("2026-06-30", 5, "2026/H1", {5001: 100}),
    ("2026-03-31", 1, "2026/Q1", {5001: 50}),
    ("2025-12-31", 7, "2025/FY", {5001: 150}),
    ("2025-06-30", 5, "2025/H1", {5001: 50}),
    ("2025-03-31", 1, "2025/Q1", {5001: 25}),
    ("2024-12-31", 7, "2024/FY", {5001: 90}),
])


def bundle(income=INCOME, balance=BALANCE, cashflow=CASHFLOW):
    return fs.FutuFinancials({
        "income": fs._parse_financial_reports("income", income),
        "balance": fs._parse_financial_reports("balance", balance),
        "cashflow": fs._parse_financial_reports("cashflow", cashflow),
    })


class FutuFundamentalRefineTest(unittest.TestCase):
    def test_deep_value_current_ratio_filter_uses_futu_percentage_units(self):
        current_ratio_filter = next(
            item for item in deep_value_screener.build_filters("HK", fs.ft)
            if item.stock_field == fs.ft.StockField.CURRENT_RATIO
        )

        # Futu V1 represents 1.5x as 150 percentage points.
        self.assertEqual(current_ratio_filter.filter_min, 150.0)

    def test_generic_refine_uses_latest_common_cumulative_report_and_annual_history(self):
        result = fs.generic_futu_refine({"code": "HK.TEST"}, bundle())

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "futu")
        self.assertEqual(result["report_period"], "2026/H1")
        self.assertEqual(result["financial_type"], 5)
        self.assertEqual(result["latest_available_periods"], {
            "income": "2026/H1",
            "balance": "2026/H1",
            "cashflow": "2026/H1",
        })
        self.assertEqual(result["periods"], 2)
        self.assertEqual(result["avg_roe_pct"], 22.0)
        self.assertEqual(result["min_roe_pct"], 20.0)
        # fixture 的展示名故意无效，必须按原生 field_id 解析。
        self.assertEqual(result["net_income"], 70)
        self.assertEqual(result["latest_roa_pct"], 6.3636)
        self.assertEqual(result["current_ratio"], 2.25)
        self.assertEqual(result["gross_margin_pct"], 60.0)
        self.assertEqual(result["operating_cash_flow"], 100)
        self.assertEqual(result["piotroski_like_score"], 8)
        self.assertEqual(result["piotroski_like_available"], 8)

    def test_negative_futu_cost_sign_is_handled_for_a_share_fields(self):
        income = statement_data([
            ("2026-03-31", 1, "2026/Q1", {3043: 10, 3002: 100, 3010: -60}),
        ], currency="CNY", accounting_standards="CAS")
        balance = statement_data([
            ("2026-03-31", 1, "2026/Q1", {
                3001: 200, 3097: 100, 3002: 120, 3056: 60,
            }),
        ], currency="CNY", accounting_standards="CAS")
        cashflow = statement_data([
            ("2026-03-31", 1, "2026/Q1", {3001: 20}),
        ], currency="CNY", accounting_standards="CAS")

        result = fs.generic_futu_refine(
            {"code": "SH.TEST"}, bundle(income, balance, cashflow),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["gross_margin_pct"], 40.0)
        # 未下发任何债务分项表示零余额，不应当解释为未知名称。
        self.assertEqual(result["piotroski_like_flags"]["lower_long_term_debt"], None)

    def test_deep_value_uses_futu_currency_to_reject_adr_mismatch(self):
        income = statement_data([
            ("2026-06-30", 5, "2026/H1", {8037: 10}),
        ], currency="CNY", accounting_standards="US GAAP")
        balance = statement_data([
            ("2026-06-30", 5, "2026/H1", {
                8002: 500, 8003: 250, 8048: 200, 8057: 100, 8081: 300,
            }),
        ], currency="CNY", accounting_standards="US GAAP")
        financials = fs.FutuFinancials({
            "income": fs._parse_financial_reports("income", income),
            "balance": fs._parse_financial_reports("balance", balance),
        })

        result = deep_value_screener.refine_futu(
            {"code": "US.TEST", "total_market_val": 100}, financials,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["condition_currency_ok"])
        self.assertFalse(result["condition_market_cap_lt_ncav"])
        self.assertFalse(result["condition_cash_minus_liabilities_gt_market_cap"])
        self.assertEqual(result["interest_bearing_debt"], 100)

    def test_us_cumulative_flows_align_with_single_quarter_balance_by_period(self):
        income = statement_data([
            ("2026-06-26", 6, "2026/Q9", {
                8037: 90, 8002: 300, 8003: 120,
            }),
        ], currency="USD", accounting_standards="US GAAP")
        balance = statement_data([
            ("2026-06-26", 3, "2026/Q3", {
                8001: 1000, 8081: 400, 8002: 300, 8049: 150, 8068: 80,
            }),
        ], currency="USD", accounting_standards="US GAAP")
        cashflow = statement_data([
            ("2026-06-26", 6, "2026/Q9", {8015: 120}),
        ], currency="USD", accounting_standards="US GAAP")

        result = fs.generic_futu_refine(
            {"code": "US.TEST"}, bundle(income, balance, cashflow),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["report_period"], "2026/Q9")
        self.assertEqual(result["financial_type"], 6)
        self.assertEqual(result["latest_available_periods"]["balance"], "2026/Q3")
        self.assertEqual(
            fs._financial_query_type("US.AAPL", "balance"),
            fs.FINANCIAL_SINGLE_QUARTER_TYPE,
        )
        self.assertEqual(
            fs._financial_query_type("US.AAPL", "cashflow"),
            fs.FINANCIAL_CUMULATIVE_TYPE,
        )

    def test_missing_required_native_field_fails_closed(self):
        income = statement_data([
            ("2026-03-31", 1, "2026/Q1", {5002: 250}),
        ])
        result = fs.generic_futu_refine(
            {"code": "HK.TEST"}, bundle(income=income),
        )

        self.assertFalse(result["ok"])
        self.assertIn("required Futu field_id", result["note"])

    def test_growth_value_does_not_pass_an_incomplete_score(self):
        candidate = {
            "l2": {
                "ok": True,
                "piotroski_like_score": 4,
                "piotroski_like_available": 6,
            },
        }

        self.assertFalse(growth_value_screener.l2_passes(candidate))

    def test_runner_reuses_context_cache_and_requests_cumulative_reports(self):
        responses = {1: INCOME, 2: BALANCE, 3: CASHFLOW}

        class FakeContext:
            def __init__(self):
                self.calls = []
                self.closed = False

            def get_financials_statements(self, code, **kwargs):
                self.calls.append((code, kwargs))
                return fs.ft.RET_OK, responses[kwargs["statement_type"]]

            def close(self):
                self.closed = True

        ctx = FakeContext()
        config = configparser.ConfigParser()
        config["CONFIG"] = {"FUTU_HOST": "127.0.0.1", "FUTU_PORT": "11111"}
        strategy = types.SimpleNamespace(refine_futu=fs.generic_futu_refine)
        candidates = [{"code": "HK.TEST"}, {"code": "HK.TEST"}]

        with mock.patch.object(fs.ft, "OpenQuoteContext", return_value=ctx), \
                mock.patch.object(fs.time, "sleep") as sleep:
            fs.run_futu_refine(candidates, strategy, config)

        self.assertEqual(
            [(code, kwargs["statement_type"]) for code, kwargs in ctx.calls],
            [("HK.TEST", 1), ("HK.TEST", 2), ("HK.TEST", 3)],
        )
        self.assertTrue(all(
            kwargs["financial_type"] == fs.FINANCIAL_CUMULATIVE_TYPE
            and kwargs["num"] == fs.FINANCIAL_PERIODS
            for _, kwargs in ctx.calls
        ))
        self.assertTrue(ctx.closed)
        self.assertTrue(all(candidate["l2"]["ok"] for candidate in candidates))
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(fs.FINANCIAL_THROTTLE_SEC)] * 3,
        )

    def test_four_strategies_use_expected_futu_l2_contracts(self):
        responses = {1: INCOME, 2: BALANCE, 3: CASHFLOW}

        class FakeContext:
            def __init__(self):
                self.calls = []

            def get_financials_statements(self, code, **kwargs):
                self.calls.append((code, kwargs["statement_type"]))
                return fs.ft.RET_OK, responses[kwargs["statement_type"]]

            def close(self):
                pass

        expected_statements = {
            sepa_screener: [],
            pr_screener: [],
            growth_value_screener: [1, 2, 3],
            deep_value_screener: [1, 2],
        }
        config = configparser.ConfigParser()
        config["CONFIG"] = {"FUTU_HOST": "127.0.0.1", "FUTU_PORT": "11111"}

        for strategy, statement_types in expected_statements.items():
            with self.subTest(strategy=strategy.NAME):
                ctx = FakeContext()
                candidate = {"code": "HK.TEST", "total_market_val": 100}
                with mock.patch.object(fs.ft, "OpenQuoteContext", return_value=ctx), \
                        mock.patch.object(fs.time, "sleep"):
                    fs.run_futu_refine([candidate], strategy, config)

                self.assertEqual(
                    [statement_type for _, statement_type in ctx.calls],
                    statement_types,
                )
                passes = getattr(strategy, "l2_passes", None)
                if strategy in (growth_value_screener, deep_value_screener):
                    self.assertTrue(candidate["l2"]["ok"])
                    self.assertTrue(passes(candidate))
                else:
                    self.assertNotIn("l2", candidate)
                    self.assertIsNone(passes)

    def test_financial_api_failure_is_not_retried(self):
        class FakeContext:
            def __init__(self):
                self.calls = 0

            def get_financials_statements(self, code, **kwargs):
                self.calls += 1
                return fs.ft.RET_ERROR, "rate limited"

            def close(self):
                pass

        ctx = FakeContext()
        config = configparser.ConfigParser()
        config["CONFIG"] = {"FUTU_HOST": "127.0.0.1", "FUTU_PORT": "11111"}
        candidate = {"code": "HK.TEST"}

        with mock.patch.object(fs.ft, "OpenQuoteContext", return_value=ctx), \
                mock.patch.object(fs.time, "sleep") as sleep:
            fs.run_futu_refine(
                [candidate],
                types.SimpleNamespace(refine_futu=fs.generic_futu_refine),
                config,
            )

        self.assertEqual(ctx.calls, 1)
        sleep.assert_called_once_with(fs.FINANCIAL_THROTTLE_SEC)
        self.assertFalse(candidate["l2"]["ok"])
        self.assertIn("rate limited", candidate["l2"]["note"])

    def test_screen_refines_all_candidates(self):
        candidates = [
            {"code": "HK.ONE"},
            {"code": "HK.TWO"},
            {"code": "HK.THREE"},
        ]
        refined_codes = []

        def refine_all(items, strategy, config):
            refined_codes.extend(item["code"] for item in items)
            for item in items:
                item["l2"] = {"ok": item["code"] != "HK.TWO"}
            return items

        strategy = types.SimpleNamespace(
            NAME="test",
            refine_futu=fs.generic_futu_refine,
            l2_passes=lambda candidate: candidate["l2"]["ok"],
        )

        with mock.patch.object(fs, "run_l1", return_value=candidates), \
                mock.patch.object(fs, "run_futu_refine", side_effect=refine_all):
            result = fs.screen(strategy, "HK", configparser.ConfigParser(),
                               snapshot=False, refine=True)

        self.assertEqual(refined_codes, ["HK.ONE", "HK.TWO", "HK.THREE"])
        self.assertEqual(result["l2_refined"], 3)
        self.assertTrue(result["l2_filtered"])
        self.assertEqual(
            [candidate["code"] for candidate in result["candidates"]],
            ["HK.ONE", "HK.THREE"],
        )

    def test_screen_skips_l2_for_strategy_without_explicit_refiner(self):
        candidates = [{"code": "HK.ONE"}, {"code": "HK.TWO"}]
        strategy = types.SimpleNamespace(
            NAME="test",
            l2_passes=lambda candidate: False,
        )

        with mock.patch.object(fs, "run_l1", return_value=candidates), \
                mock.patch.object(fs, "run_futu_refine") as refine:
            result = fs.screen(strategy, "HK", configparser.ConfigParser(),
                               snapshot=False, refine=True)

        refine.assert_not_called()
        self.assertEqual(result["l2_refined"], 0)
        self.assertFalse(result["l2_filtered"])
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["candidates"], candidates)


if __name__ == "__main__":
    unittest.main()
