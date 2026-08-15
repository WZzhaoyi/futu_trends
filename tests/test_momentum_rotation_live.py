import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "momentum_rotation_strategy.py"
)
SPEC = importlib.util.spec_from_file_location(
    "momentum_rotation_strategy_tested",
    MODULE_PATH,
)
assert SPEC is not None and SPEC.loader is not None
momentum = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = momentum
SPEC.loader.exec_module(momentum)


class LiveConfigurationTest(unittest.TestCase):
    def test_live_modes_bundle_optimized_pairs_and_notification_times(self):
        us = momentum.LIVE_PRESETS["live-us"]
        cn = momentum.LIVE_PRESETS["live-cn"]
        self.assertEqual(
            us["pairs"],
            (
                ("US.QQQ", 21),
                ("US.SPY", 21),
                ("US.FXI", 21),
                ("US.UUP", 21),
            ),
        )
        self.assertEqual(us["cash_symbols"], {"US.UUP"})
        self.assertEqual(us["notification_time"], time(16, 10))
        self.assertEqual(str(us["timezone"]), "America/New_York")
        self.assertEqual(
            cn["pairs"],
            (
                ("SZ.159941", 28),
                ("SZ.159949", 28),
                ("SH.510300", 28),
                ("SH.510880", 28),
            ),
        )
        self.assertEqual(cn["cash_symbols"], set())
        self.assertEqual(cn["notification_time"], time(15, 10))
        self.assertEqual(str(cn["timezone"]), "Asia/Shanghai")

    def test_live_cli_has_no_strategy_or_connection_overrides(self):
        args = momentum.parse_args(
            [
                "live-us",
                "--runtime-dir",
                "/tmp/momentum",
                "--config",
                "config.ini",
            ]
        )

        self.assertEqual(args.mode, "live-us")
        self.assertEqual(args.end, date.today().isoformat())
        self.assertEqual(args.max_quote_age, 14400)
        for name in (
            "etfs",
            "windows",
            "cash_symbols",
            "notification_time",
            "host",
            "port",
            "data_source",
        ):
            self.assertFalse(hasattr(args, name))

    def test_connection_and_data_source_come_from_config(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "config.ini"
            path.write_text(
                "[CONFIG]\nDATA_SOURCE=futu\nFUTU_HOST=10.0.0.8\nFUTU_PORT=12345\n",
                encoding="utf-8",
            )
            self.assertEqual(
                momentum.resolve_live_connection(str(path), ["US.QQQ"]),
                ("10.0.0.8", 12345),
            )
            path.write_text(
                "[CONFIG]\nDATA_SOURCE=futu\nDATA_SOURCE_US=yfinance\n"
                "FUTU_HOST=10.0.0.8\nFUTU_PORT=12345\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "DATA_SOURCE"):
                momentum.resolve_live_connection(str(path), ["US.QQQ"])


class LiveSignalTest(unittest.TestCase):
    def test_momentum_score_reuses_linear_tools_calculation(self):
        expected = pd.Series([np.nan, 1.23])
        with patch.object(momentum, "calc_momentum", return_value=expected) as calculate:
            score = momentum.calculate_momentum_score(np.array([1.0, 1.1]))

        self.assertEqual(score, 1.23)
        calculate.assert_called_once()
        args, kwargs = calculate.call_args
        pd.testing.assert_series_equal(args[0], pd.Series([1.0, 1.1]))
        self.assertEqual(kwargs, {"N": 2, "method": "linear"})

    def test_each_symbol_uses_its_own_window(self):
        pairs = [("US.QQQ", 3), ("US.SPY", 4)]
        closes = {
            "US.QQQ": [1.0, 2.0, 3.0, 4.0],
            "US.SPY": [1.0, 1.1, 1.2, 1.3],
        }

        scores = momentum.score_live_pairs(pairs, closes)

        self.assertAlmostEqual(
            scores["US.QQQ"],
            momentum.calculate_momentum_score(np.array([2.0, 3.0, 4.0])),
        )
        self.assertAlmostEqual(
            scores["US.SPY"],
            momentum.calculate_momentum_score(np.array([1.0, 1.1, 1.2, 1.3])),
        )

    def test_transition_distinguishes_rotation_and_cash_proxy(self):
        cash = {"US.UUP"}
        self.assertEqual(
            momentum.live_transition_action(None, "US.QQQ", cash),
            "INITIAL",
        )
        self.assertEqual(
            momentum.live_transition_action("US.QQQ", "US.SPY", cash),
            "ROTATE",
        )
        self.assertEqual(
            momentum.live_transition_action("US.SPY", "US.UUP", cash),
            "SELL",
        )
        self.assertEqual(
            momentum.live_transition_action("US.UUP", "US.QQQ", cash),
            "BUY",
        )

    def test_daily_evaluation_waits_until_after_close_buffer(self):
        self.assertFalse(
            momentum.daily_evaluation_ready(
                datetime.fromisoformat("2026-08-15T04:09:00+08:00"),
                time(16, 10),
                momentum.US_MARKET_TIMEZONE,
            )
        )
        self.assertTrue(
            momentum.daily_evaluation_ready(
                datetime.fromisoformat("2026-08-15T04:10:00+08:00"),
                time(16, 10),
                momentum.US_MARKET_TIMEZONE,
            )
        )
        self.assertFalse(
            momentum.daily_evaluation_ready(
                datetime.fromisoformat("2026-08-16T04:10:00+08:00"),
                time(16, 10),
                momentum.US_MARKET_TIMEZONE,
            )
        )
        self.assertFalse(
            momentum.daily_evaluation_ready(
                datetime.fromisoformat("2026-08-14T15:09:00+08:00"),
                time(15, 10),
                momentum.CN_MARKET_TIMEZONE,
            )
        )
        self.assertTrue(
            momentum.daily_evaluation_ready(
                datetime.fromisoformat("2026-08-14T15:10:00+08:00"),
                time(15, 10),
                momentum.CN_MARKET_TIMEZONE,
            )
        )

    def test_futu_trading_calendar_distinguishes_holiday(self):
        trading = types.SimpleNamespace(
            request_trading_days=lambda **_kwargs: (
                0,
                [{"time": "2026-08-14", "trade_date_type": "WHOLE"}],
            )
        )
        holiday = types.SimpleNamespace(
            request_trading_days=lambda **_kwargs: (0, [])
        )

        self.assertTrue(
            momentum.is_live_trading_day(
                trading,
                "US.QQQ",
                "2026-08-14",
                0,
            )
        )
        self.assertFalse(
            momentum.is_live_trading_day(
                holiday,
                "US.QQQ",
                "2026-08-14",
                0,
            )
        )


class LiveRuntimeTest(unittest.TestCase):
    def test_state_rejects_changed_pair_configuration(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "state.json"
            state = momentum.LiveState(path, [("US.QQQ", 21)])
            state.last_evaluation_date = "2026-08-14"
            state.save([("US.QQQ", 21)])

            restarted = momentum.LiveState(path, [("US.QQQ", 21)])
            self.assertEqual(restarted.last_evaluation_date, "2026-08-14")

            with self.assertRaisesRegex(ValueError, "配置不匹配"):
                momentum.LiveState(path, [("US.QQQ", 22)])

    def test_runtime_directory_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            momentum.LiveRuntimePaths.from_argument("relative/runtime", "live-us")

    def test_live_modes_use_separate_state_and_lock_files(self):
        us = momentum.LiveRuntimePaths.from_argument("/tmp/runtime", "live-us")
        cn = momentum.LiveRuntimePaths.from_argument("/tmp/runtime", "live-cn")

        self.assertEqual(us.state_file.name, "state-live-us.json")
        self.assertEqual(cn.state_file.name, "state-live-cn.json")
        self.assertNotEqual(us.lock_file, cn.lock_file)


class FakeQuoteContext:
    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.subscriptions = []
        self.closed = False
        self.__class__.instances.append(self)

    def subscribe(self, symbols, subtypes, is_first_push):
        self.subscriptions.append((symbols, subtypes, is_first_push))
        return 0, "ok"

    def request_trading_days(self, start, end, code):
        return 0, [{"time": start, "trade_date_type": "WHOLE"}]

    def get_market_snapshot(self, symbols):
        last_prices = {
            "US.QQQ": 121.0,
            "US.SPY": 111.0,
            "US.FXI": 80.0,
            "US.UUP": 100.0,
            "SZ.159941": 121.0,
            "SZ.159949": 111.0,
            "SH.510300": 80.0,
            "SH.510880": 100.0,
        }
        return 0, pd.DataFrame(
            [
                {
                    "code": symbol,
                    "last_price": last_prices[symbol],
                    "update_time": "2026-08-14 16:05:00",
                }
                for symbol in symbols
            ]
        )

    def get_cur_kline(self, symbol, count, _subtype, _adjustment):
        if symbol in {"US.QQQ", "SZ.159941"}:
            closes = np.linspace(100, 121, count)
        elif symbol in {"US.SPY", "SZ.159949"}:
            closes = np.linspace(100, 111, count)
        elif symbol in {"US.FXI", "SH.510300"}:
            closes = np.linspace(100, 80, count)
        else:
            closes = np.full(count, 100.0)
        return 0, pd.DataFrame(
            {
                "time_key": pd.bdate_range(end="2026-08-14", periods=count).strftime(
                    "%Y-%m-%d 16:00:00"
                ),
                "close": closes,
            }
        )

    def close(self):
        self.closed = True


class RecordingNotifier:
    def __init__(self):
        self.events = []
        self.closed = False

    def notify(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


class HolidayQuoteContext(FakeQuoteContext):
    instances = []

    def __init__(self, host, port):
        super().__init__(host, port)
        self.calendar_requests = 0

    def request_trading_days(self, start, end, code):
        self.calendar_requests += 1
        return 0, []

    def subscribe(self, symbols, subtypes, is_first_push):
        raise AssertionError("休市日不应订阅行情")


class LiveEndToEndTest(unittest.TestCase):
    def test_holiday_is_recorded_once_without_notification(self):
        fake_futu = types.ModuleType("futu")
        fake_futu.AuType = types.SimpleNamespace(QFQ="QFQ")
        fake_futu.OpenQuoteContext = HolidayQuoteContext
        fake_futu.RET_OK = 0
        fake_futu.SubType = types.SimpleNamespace(K_DAY="K_DAY")
        notifier = RecordingNotifier()
        HolidayQuoteContext.instances.clear()

        with tempfile.TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.ini"
            config_path.write_text(
                "[CONFIG]\nDATA_SOURCE=futu\nFUTU_HOST=127.0.0.1\nFUTU_PORT=11111\n",
                encoding="utf-8",
            )
            args = momentum.parse_args(
                [
                    "live-us",
                    "--runtime-dir",
                    raw_dir,
                    "--config",
                    str(config_path),
                    "--interval",
                    "0.001",
                    "--duration",
                    "0.02",
                ]
            )
            with (
                patch.dict(sys.modules, {"futu": fake_futu}),
                patch.object(
                    momentum,
                    "build_live_notifier",
                    return_value=notifier,
                ),
                patch.object(
                    momentum,
                    "daily_evaluation_ready",
                    return_value=True,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(momentum.run_live(args), 0)

            state = momentum.LiveState(
                Path(raw_dir) / "state-live-us.json",
                list(momentum.LIVE_PRESETS["live-us"]["pairs"]),
            )

        context = HolidayQuoteContext.instances[0]
        self.assertEqual(context.calendar_requests, 1)
        self.assertEqual(state.last_snapshot["type"], "IDLE")
        self.assertIn("非交易日", state.last_snapshot["message"])
        self.assertEqual(notifier.events, [])
        self.assertTrue(notifier.closed)

    def test_both_modes_fetch_persist_and_notify_without_backtest(self):
        fake_futu = types.ModuleType("futu")
        fake_futu.AuType = types.SimpleNamespace(QFQ="QFQ")
        fake_futu.OpenQuoteContext = FakeQuoteContext
        fake_futu.RET_OK = 0
        fake_futu.SubType = types.SimpleNamespace(K_DAY="K_DAY")
        for mode, expected_symbol in (
            ("live-us", "US.QQQ"),
            ("live-cn", "SZ.159941"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_dir:
                notifier = RecordingNotifier()
                FakeQuoteContext.instances.clear()
                config_path = Path(raw_dir) / "config.ini"
                config_path.write_text(
                    "[CONFIG]\nDATA_SOURCE=futu\nFUTU_HOST=127.0.0.1\nFUTU_PORT=11111\n",
                    encoding="utf-8",
                )
                args = momentum.parse_args(
                    [
                        mode,
                        "--runtime-dir",
                        raw_dir,
                        "--config",
                        str(config_path),
                        "--once",
                    ]
                )
                with (
                    patch.dict(sys.modules, {"futu": fake_futu}),
                    patch.object(
                        momentum,
                        "build_live_notifier",
                        return_value=notifier,
                    ),
                    patch.object(
                        momentum,
                        "prepare_history",
                        side_effect=AssertionError(
                            "live must not prepare backtest data"
                        ),
                    ),
                    patch.object(
                        momentum,
                        "run_momentum_backtest",
                        side_effect=AssertionError("live must not run a backtest"),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(momentum.run_live(args), 0)

                pairs = list(momentum.LIVE_PRESETS[mode]["pairs"])
                state = momentum.LiveState(
                    Path(raw_dir) / f"state-{mode}.json",
                    pairs,
                )
                context = FakeQuoteContext.instances[0]
                self.assertEqual(state.selected_symbol, expected_symbol)
                self.assertEqual(state.last_evaluation_date, "2026-08-14")
                self.assertEqual(len(notifier.events), 1)
                self.assertEqual(
                    notifier.events[0]["target_symbol"], expected_symbol
                )
                self.assertEqual(notifier.events[0]["action"], "INITIAL")
                self.assertEqual(notifier.events[0]["mode"], mode)
                self.assertTrue(notifier.closed)
                self.assertEqual(
                    context.subscriptions[0][0],
                    [symbol for symbol, _window in pairs],
                )
                self.assertTrue(context.closed)


if __name__ == "__main__":
    unittest.main()
