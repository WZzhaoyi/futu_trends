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
# 5071「购买固定资产」按港股口径本身为负号（流出）。
# TTM 经营现金流 = 100 + 150 − 50 = 200；TTM 资本开支 = (−30) + (−40) − (−10) = −60。
# 故 FCF_TTM = 200 − 60 = 140，r = FCF/OCF = 0.7。
CASHFLOW = statement_data([
    ("2026-06-30", 5, "2026/H1", {5001: 100, 5071: -30}),
    ("2026-03-31", 1, "2026/Q1", {5001: 50}),
    ("2025-12-31", 7, "2025/FY", {5001: 150, 5071: -40}),
    ("2025-06-30", 5, "2025/H1", {5001: 50, 5071: -10}),
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
                # pcf_ttm=700 即 P/OCF=7.0；r=0.7 时 P/FCF = 7.0 / 0.7 = 10.0 ≤ 11
                candidate = {
                    "code": "HK.TEST", "total_market_val": 100, "pcf_ttm": 700.0,
                }
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

    def test_growth_value_l2_enforces_p_over_fcf_ceiling(self):
        """P/FCF ≤ 11 是硬门槛，且边界要卡在 11 上（含等于）。

        夹具下 TTM 经营现金流 = 200、FCF_TTM = 140，r = 0.7，
        故 P/FCF = (pcf_ttm ÷ 100) ÷ 0.7，阈值对应 pcf_ttm = 770。
        """
        financials = bundle()
        for pcf_ttm, expected in ((600.0, True), (770.0, True), (771.0, False)):
            with self.subTest(pcf_ttm=pcf_ttm):
                candidate = {
                    "code": "HK.TEST", "total_market_val": 100, "pcf_ttm": pcf_ttm,
                }
                candidate["l2"] = growth_value_screener.refine_futu(
                    candidate, financials,
                )
                self.assertTrue(candidate["l2"]["fcf_ok"])
                self.assertEqual(
                    candidate["l2"]["fcf_ttm"], 140.0,
                )
                self.assertAlmostEqual(
                    candidate["l2"]["p_over_fcf"], (pcf_ttm / 100) / 0.7, places=4,
                )
                self.assertEqual(
                    growth_value_screener.l2_passes(candidate), expected,
                )

    def test_pcf_ceiling_tracks_pfcf_max_not_prefilter(self):
        """判据的等价阈值由 PFCF_MAX 推出，不能跟着 L1 的预筛上限漂。

        夹具下 r = 0.7，故 ceiling = 100 × 11 × 0.7 = 770；
        若误用 PCF_TTM_MAX(1200) 会得到 840，判据就被悄悄放松了。
        """
        financials = bundle()
        candidate = {"code": "HK.TEST", "total_market_val": 100, "pcf_ttm": 700.0}
        l2 = growth_value_screener.refine_futu(candidate, financials)

        self.assertEqual(growth_value_screener.PFCF_MAX, 11.0)
        self.assertEqual(growth_value_screener.PCF_TTM_MAX, 1200.0)
        self.assertAlmostEqual(l2["pcf_ttm_ceiling"], 770.0, places=2)

    def test_growth_value_reject_reason_attribution(self):
        """拒绝原因按判据求值顺序归因；None 必须与 l2_passes 完全一致。"""
        base = {"ok": True, "piotroski_like_available": 8, "piotroski_like_score": 4,
                "fcf_ok": True}
        cases = [
            ({"ok": False}, "l2_unavailable"),
            ({**base, "piotroski_like_available": 7}, "piotroski_incomplete"),
            ({**base, "piotroski_like_score": 3}, "piotroski_below_threshold"),
            ({**base, "piotroski_like_score": None}, "piotroski_below_threshold"),
            ({**base, "fcf_ok": False}, "fcf_unavailable"),
            ({**base, "p_over_fcf": None}, "fcf_non_positive"),
            ({**base, "p_over_fcf": 12.0}, "pfcf_above_max"),
            ({**base, "p_over_fcf": 11.0}, None),
            ({**base, "p_over_fcf": 5.0}, None),
        ]
        for l2, expected in cases:
            with self.subTest(l2=l2):
                candidate = {"l2": l2}
                self.assertEqual(
                    growth_value_screener.l2_reject_reason(candidate), expected,
                )
                self.assertEqual(
                    growth_value_screener.l2_passes(candidate), expected is None,
                )

    def test_deep_value_reject_reason_attribution(self):
        base = {"ok": True, "condition_currency_ok": True,
                "condition_market_cap_lt_ncav": True}
        cases = [
            ({"ok": False}, "l2_unavailable"),
            ({**base, "condition_currency_ok": False}, "currency_mismatch"),
            ({**base, "condition_market_cap_lt_ncav": False},
             "market_cap_not_below_ncav"),
            (base, None),
        ]
        for l2, expected in cases:
            with self.subTest(l2=l2):
                candidate = {"l2": l2}
                self.assertEqual(
                    deep_value_screener.l2_reject_reason(candidate), expected,
                )
                self.assertEqual(
                    deep_value_screener.l2_passes(candidate), expected is None,
                )

    def test_screen_records_l2_reject_reason_counts(self):
        """计数必须能对账：sum(原因) == l2_refined − returned。"""
        candidates = [
            {"code": "X1", "l2": {"ok": False}},
            {"code": "X2", "l2": {"ok": True, "piotroski_like_available": 8,
                                  "piotroski_like_score": 4, "fcf_ok": True,
                                  "p_over_fcf": 20.0}},
            {"code": "X3", "l2": {"ok": True, "piotroski_like_available": 8,
                                  "piotroski_like_score": 2, "fcf_ok": True,
                                  "p_over_fcf": 5.0}},
            {"code": "X4", "l2": {"ok": True, "piotroski_like_available": 8,
                                  "piotroski_like_score": 4, "fcf_ok": True,
                                  "p_over_fcf": 5.0}},
        ]
        with mock.patch.object(fs, "run_l1", return_value=list(candidates)), \
                mock.patch.object(fs, "run_futu_refine", return_value=None):
            result = fs.screen(
                growth_value_screener, "A", configparser.ConfigParser(),
                snapshot=False, refine=True,
            )

        self.assertEqual(result["l2_refined"], 4)
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["l2_reject_reasons"], {
            "l2_unavailable": 1,
            "pfcf_above_max": 1,
            "piotroski_below_threshold": 1,
        })
        self.assertEqual(
            sum(result["l2_reject_reasons"].values()),
            result["l2_refined"] - result["returned"],
        )

    def test_growth_value_l2_rejects_non_positive_free_cash_flow(self):
        """FCF ≤ 0 时 P/FCF 无意义，必须判不合格而不是放行。"""
        candidate = {"code": "HK.TEST", "total_market_val": 100, "pcf_ttm": 700.0}
        # 资本开支远超经营现金流：TTM 资本开支由 5071 吞掉全部 OCF
        heavy = statement_data([
            ("2026-06-30", 5, "2026/H1", {5001: 100, 5071: -300}),
            ("2025-12-31", 7, "2025/FY", {5001: 150, 5071: -300}),
            ("2025-06-30", 5, "2025/H1", {5001: 50, 5071: -100}),
        ])
        financials = bundle(cashflow=heavy)
        candidate["l2"] = growth_value_screener.refine_futu(candidate, financials)
        self.assertTrue(candidate["l2"]["fcf_ok"])
        self.assertLessEqual(candidate["l2"]["fcf_ttm"], 0)
        self.assertIsNone(candidate["l2"]["p_over_fcf"])
        self.assertFalse(growth_value_screener.l2_passes(candidate))

    def test_fixed_asset_sign_is_normalised_across_a_and_hk(self):
        """A 的 3043 是正号流出、港股的 5071 是负号流出，归一后必须同号同值。"""
        a_cash = statement_data([("2026-06-30", 7, "2026/FY", {3043: 60.0})])
        hk_cash = statement_data([("2026-06-30", 7, "2026/FY", {5071: -60.0})])
        a_financials = fs.FutuFinancials(
            {"cashflow": fs._parse_financial_reports("cashflow", a_cash)},
        )
        hk_financials = fs.FutuFinancials(
            {"cashflow": fs._parse_financial_reports("cashflow", hk_cash)},
        )
        self.assertEqual(
            a_financials.net_fixed_asset_cash_flow_ttm(fs.FUTU_FIELD_SETS[3]),
            -60.0,
        )
        self.assertEqual(
            hk_financials.net_fixed_asset_cash_flow_ttm(fs.FUTU_FIELD_SETS[5]),
            -60.0,
        )

    def test_growth_value_prefilter_is_necessary_condition_for_p_over_fcf(self):
        """L1 预筛必须写成 PCF_TTM ≤ 1200（百分比刻度），且经营现金流下限严格 >0。"""
        filters = growth_value_screener.build_filters("HK", fs.ft)
        pcf = next(
            item for item in filters
            if item.stock_field == fs.ft.StockField.PCF_TTM
        )
        self.assertEqual(pcf.filter_max, 1200.0)
        ocf = next(
            item for item in filters
            if getattr(item, "stock_field", None) == fs.ft.StockField.OPERATING_CASH_FLOW_TTM
        )
        self.assertEqual(ocf.filter_min, fs.POSITIVE_MIN)

    def test_positive_min_clears_futu_quantisation_threshold(self):
        """实测 Futu 把 1e-9 及以下的下限量化成 0，POSITIVE_MIN 必须高于该阈值。

        否则「要求为正」会退化成「≥0」，放进恰好等于 0 的标的。
        """
        self.assertGreater(fs.POSITIVE_MIN, 0.001)
        self.assertLessEqual(fs.POSITIVE_MIN, 0.01)

    def test_strategies_share_positive_min_for_positivity_bounds(self):
        """四个策略里「要求为正」的下限必须都引用 POSITIVE_MIN，不能各写各的。"""
        expectations = {
            pr_screener: (
                fs.ft.StockField.PB_RATE, fs.ft.StockField.PE_TTM,
                fs.ft.StockField.NET_PROFIT,
                fs.ft.StockField.OPERATING_CASH_FLOW_TTM,
            ),
            growth_value_screener: (
                fs.ft.StockField.PE_TTM, fs.ft.StockField.PB_RATE,
                fs.ft.StockField.PCF_TTM,
                fs.ft.StockField.OPERATING_CASH_FLOW_TTM,
                fs.ft.StockField.SUM_OF_BUSINESS_GROWTH,
                fs.ft.StockField.NET_PROFIX_GROWTH,
            ),
            deep_value_screener: (
                fs.ft.StockField.PE_TTM, fs.ft.StockField.PB_RATE,
                fs.ft.StockField.NET_PROFIT,
                fs.ft.StockField.CASH_AND_CASH_EQUIVALENTS,
            ),
        }
        for strategy, fields in expectations.items():
            with self.subTest(strategy=strategy.NAME):
                actual = {
                    getattr(item, "stock_field", None): item.filter_min
                    for item in strategy.build_filters("A", fs.ft)
                    if getattr(item, "stock_field", None) in fields
                }
                self.assertEqual(set(actual), set(fields))
                for field, value in actual.items():
                    self.assertEqual(value, fs.POSITIVE_MIN, msg=str(field))

    def test_growth_thresholds_use_positive_min(self):
        """增速下限统一用 POSITIVE_MIN：百分比口径上 0.01 即 0.01%，仍是桶底。"""
        self.assertEqual(growth_value_screener.REV_GROWTH_MIN, fs.POSITIVE_MIN)
        self.assertEqual(growth_value_screener.PROFIT_GROWTH_MIN, fs.POSITIVE_MIN)

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
