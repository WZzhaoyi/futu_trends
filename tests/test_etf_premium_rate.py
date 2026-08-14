import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "etf_premium_rate.py"
)
SPEC = importlib.util.spec_from_file_location("etf_premium_rate_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
premium = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = premium
SPEC.loader.exec_module(premium)


class NavAlignmentTest(unittest.TestCase):
    def test_uses_latest_nav_strictly_before_trade_date(self):
        prices = [
            {"date": day, "close": close}
            for day, close in (
                ("2025-01-02", 1.00),
                ("2025-01-03", 1.10),
                ("2025-01-06", 1.20),
                ("2025-01-07", 1.30),
            )
        ]
        navs = [
            {
                "date": "2025-01-02",
                "unit_nav": 1.0,
                "accumulated_nav": 1.0,
            },
            {
                "date": "2025-01-06",
                "unit_nav": 1.1,
                "accumulated_nav": 1.1,
            },
        ]

        frame = premium.build_signal_frame(prices, navs)

        self.assertEqual(frame["trade_date"].dt.strftime("%Y-%m-%d").tolist(), [
            "2025-01-03",
            "2025-01-06",
        ])
        self.assertEqual(frame["nav_date"].dt.strftime("%Y-%m-%d").tolist(), [
            "2025-01-02",
            "2025-01-02",
        ])
        self.assertEqual(frame["nav_age_days"].tolist(), [1, 4])

    def test_total_return_factor_is_also_strictly_t1(self):
        prices = [
            {"date": "2022-07-04", "close": 2.384},
            {"date": "2022-07-05", "close": 0.604},
            {"date": "2022-07-06", "close": 0.611},
        ]
        navs = [
            {
                "date": "2022-07-01",
                "unit_nav": 2.3898,
                "accumulated_nav": 2.3898,
            },
            {
                "date": "2022-07-04",
                "unit_nav": 0.5992,
                "accumulated_nav": 2.3968,
            },
            {
                "date": "2022-07-05",
                "unit_nav": 0.6086,
                "accumulated_nav": 2.4344,
            },
        ]

        frame = premium.build_signal_frame(prices, navs)

        self.assertAlmostEqual(frame.iloc[0]["total_return_close"], 2.384)
        self.assertAlmostEqual(frame.iloc[1]["total_return_close"], 2.416)
        self.assertAlmostEqual(frame.iloc[0]["ret"], 2.416 / 2.384 - 1)

    def test_nav_parser_uses_names_and_ignores_unpublished_values(self):
        items = [
            {
                "FSRQ": "2025-11-20",
                "DWJZ": "1.2808",
                "LJJZ": "5.1232",
                "FHSP": "new upstream field",
            },
            {"FSRQ": "2025-11-21", "DWJZ": "---", "LJJZ": "---"},
        ]

        self.assertEqual(
            premium.parse_nav_items(items),
            [
                {
                    "date": "2025-11-20",
                    "unit_nav": 1.2808,
                    "accumulated_nav": 5.1232,
                }
            ],
        )


class StrategyStateTest(unittest.TestCase):
    def setUp(self):
        self.params = premium.StrategyParams(
            buy_threshold=0.0,
            sell_threshold=0.03,
            low_position=0.5,
        )

    def test_hysteresis_holds_position_between_thresholds(self):
        positions = premium.positions_for_premium(
            np.array([0.04, 0.02, -0.01, 0.02, 0.04]),
            self.params,
        )
        np.testing.assert_allclose(positions, [0.5, 0.5, 1.0, 1.0, 0.5])

    def test_live_only_emits_on_position_transition(self):
        self.assertEqual(
            premium.apply_live_signal("base", 0.04, self.params),
            ("low", "SELL"),
        )
        self.assertEqual(
            premium.apply_live_signal("low", 0.04, self.params),
            ("low", "NONE"),
        )
        self.assertEqual(
            premium.apply_live_signal("low", -0.01, self.params),
            ("base", "BUY"),
        )


class LiveRuntimeTest(unittest.TestCase):
    def test_runtime_dir_must_be_absolute_and_is_single_instance(self):
        with self.assertRaises(ValueError):
            premium.LiveRuntimePaths.from_argument("relative/runtime")
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = premium.LiveRuntimePaths.from_argument(raw_dir)
            runtime.prepare()
            with premium.RuntimeFileLock(runtime.lock_file):
                with self.assertRaises(RuntimeError):
                    with premium.RuntimeFileLock(runtime.lock_file):
                        pass

    def test_futu_fund_premium_is_hint_only(self):
        snapshot = premium.parse_futu_snapshot(
            {
                "last_price": 1.684,
                "update_time": "2026-08-14 11:26:57",
                "trust_valid": True,
                "trust_premium": 10.44,
            }
        )
        self.assertAlmostEqual(snapshot["iopv_premium"], 0.1044)
        self.assertAlmostEqual(snapshot["iopv_estimate"], 1.5248098515)

    def test_market_session_uses_asia_shanghai(self):
        self.assertTrue(
            premium.in_cn_market_session(
                datetime.fromisoformat("2026-08-14T10:00:00+08:00")
            )
        )
        self.assertFalse(
            premium.in_cn_market_session(
                datetime.fromisoformat("2026-08-14T12:00:00+08:00")
            )
        )


class FakeNotificationEngine:
    def __init__(self):
        self.webhooks = []
        self.telegrams = []
        self.emails = []

    def send_webhook(self, message):
        self.webhooks.append(message)

    def send_telegram_message(self, message, link):
        self.telegrams.append((message, link))

    def send_email(self, subject, message):
        self.emails.append((subject, message))


class NotificationTest(unittest.TestCase):
    def test_signal_is_sent_once_to_all_channels(self):
        engine = FakeNotificationEngine()
        notifier = premium.LiveNotifier(engine)
        event = {
            "type": "SIGNAL",
            "symbol": "159941",
            "action": "SELL",
            "price": 1.683,
            "unit_nav": 1.5073,
            "nav_date": "2026-08-12",
            "nav_age_days": 2,
            "premium": 0.1166,
            "t1_premium": 0.1166,
            "iopv_premium": 0.1051,
            "iopv_estimate": 1.5248,
            "buy_threshold": 0.01,
            "sell_threshold": 0.08,
            "target_position": 0.0,
        }

        notifier.notify(event)
        notifier.notify(event)
        notifier.close()

        self.assertEqual(len(engine.webhooks), 1)
        self.assertEqual(len(engine.telegrams), 1)
        self.assertEqual(len(engine.emails), 1)
        self.assertIn("SELL", engine.webhooks[0])
        self.assertIn("滞后2天", engine.webhooks[0])
        self.assertIn("仅提示", engine.webhooks[0])


class CliTest(unittest.TestCase):
    def test_defaults_match_target_etf_and_joinquant_period(self):
        parser = premium.build_parser()
        args = parser.parse_args(["backtest"])
        self.assertEqual(args.symbol, "159941")
        self.assertEqual(args.start, "2016-01-01")
        self.assertEqual(args.cost, 0.001)

        live = parser.parse_args(["live", "--runtime-dir", "/tmp/etf-premium"])
        self.assertEqual(live.initial_position, "base")
        self.assertEqual(live.interval, 60.0)
        self.assertEqual(live.nav_refresh, 900.0)
        self.assertEqual(live.max_nav_age, 14)
        self.assertEqual(live.max_quote_age, 180)
        self.assertEqual(live.max_errors, 5)


if __name__ == "__main__":
    unittest.main()
