import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "etf_premium_rate.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
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
    def test_hysteresis_positions_and_actions(self):
        params = premium.StrategyParams(
            buy_threshold=0.0, sell_threshold=0.03, low_position=0.5,
        )
        values = np.array([0.03, 0.04, 0.04, 0.0, 0.02, -0.01, -0.01, 0.03, 0.02, 0.04])
        expected = [
            ("base", "NONE"), ("low", "SELL"), ("low", "NONE"), ("low", "NONE"), ("low", "NONE"),
            ("base", "BUY"), ("base", "NONE"), ("base", "NONE"), ("base", "NONE"),
            ("low", "SELL"),
        ]
        state = "base"
        transitions = []
        for value in values:
            state, action = premium.decide_position(state, float(value), params)
            transitions.append((state, action))
        self.assertEqual(transitions, expected)
        np.testing.assert_allclose(
            premium.positions_for_premium(values, params),
            [1.0, 0.5, 0.5, 0.5, 0.5, 1.0, 1.0, 1.0, 1.0, 0.5],
        )


class LiveRuntimeTest(unittest.TestCase):
    def test_runtime_dir_must_be_absolute(self):
        with self.assertRaises(ValueError):
            premium.LiveRuntimePaths.from_argument("relative/runtime")

    def test_futu_snapshot_converts_percentage_and_estimates_iopv(self):
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
                datetime.fromisoformat("2026-08-14T02:00:00+00:00")
            )
        )
        self.assertFalse(
            premium.in_cn_market_session(
                datetime.fromisoformat("2026-08-14T04:00:00+00:00")
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
        self.assertIn("估算IOPV", engine.webhooks[0])


if __name__ == "__main__":
    unittest.main()
