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


def find_leg(name: str):
    for leg in momentum.LIVE_LEGS:
        if leg.name == name:
            return leg
    raise AssertionError(f"LIVE_LEGS 缺少 {name}")


class LiveConfigurationTest(unittest.TestCase):
    def test_live_legs_match_deployed_configuration(self):
        us_a = find_leg("US-A")
        self.assertEqual(us_a.market, "US")
        self.assertEqual(
            us_a.symbols,
            ("US.QQQ", "US.SPY", "US.FXI", "US.GLD"),
        )
        self.assertEqual(us_a.window, 22)
        self.assertEqual(us_a.cooldown, 13)
        self.assertEqual(us_a.gap_eps, 0.0)
        self.assertEqual(us_a.cash_symbols, ())
        self.assertEqual(us_a.slippage, momentum.DEFAULT_SLIPPAGE_US)

        us_b = find_leg("US-B")
        self.assertEqual(us_b.market, "US")
        self.assertEqual(
            us_b.symbols,
            ("US.UUP", "US.QQQ", "US.FXI", "US.GLD"),
        )
        self.assertEqual(us_b.window, 20)
        self.assertEqual(us_b.cooldown, 0)
        self.assertEqual(us_b.gap_eps, 0.32)
        self.assertEqual(us_b.cash_symbols, ("US.UUP",))
        self.assertEqual(us_b.slippage, momentum.DEFAULT_SLIPPAGE_US)

        cn_a = find_leg("CN-A")
        self.assertEqual(cn_a.market, "CN")
        self.assertEqual(
            cn_a.symbols,
            ("SZ.159941", "SZ.159949", "SH.510300", "SH.518880"),
        )
        self.assertEqual(cn_a.window, 26)
        self.assertEqual(cn_a.cooldown, 10)
        self.assertEqual(cn_a.slippage, momentum.DEFAULT_SLIPPAGE_CN)

        cn_b = find_leg("CN-B")
        self.assertEqual(cn_b.market, "CN")
        self.assertEqual(
            cn_b.symbols,
            ("SZ.159941", "SZ.159949", "SH.510300", "SH.510880"),
        )
        self.assertEqual(cn_b.window, 21)
        self.assertEqual(cn_b.cooldown, 16)
        self.assertEqual(cn_b.slippage, momentum.DEFAULT_SLIPPAGE_CN)

    def test_market_specs_have_staggered_notification_times(self):
        self.assertEqual(
            momentum.MARKET_SPECS["US"]["notification_time"], time(16, 10)
        )
        self.assertEqual(
            str(momentum.MARKET_SPECS["US"]["timezone"]),
            "America/New_York",
        )
        self.assertEqual(
            momentum.MARKET_SPECS["CN"]["notification_time"], time(15, 10)
        )
        self.assertEqual(
            str(momentum.MARKET_SPECS["CN"]["timezone"]), "Asia/Shanghai"
        )
        self.assertEqual(
            len({leg.market for leg in momentum.LIVE_LEGS}), 2
        )
        self.assertEqual(
            [leg.name for leg in momentum.LIVE_LEGS],
            ["US-A", "US-B", "CN-A", "CN-B"],
        )

    def test_live_cli_has_no_strategy_or_connection_overrides(self):
        args = momentum.parse_args(
            [
                "live",
                "--runtime-dir",
                "/tmp/momentum",
                "--config",
                "config.ini",
            ]
        )

        self.assertEqual(args.mode, "live")
        self.assertEqual(args.end, date.today().isoformat())
        for name in (
            "etfs",
            "windows",
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
    def test_momentum_score_uses_linear_method(self):
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

    def test_leg_decision_initial_none_and_cooldown(self):
        leg = find_leg("US-A")
        scores = {"US.QQQ": 0.8, "US.SPY": 0.6, "US.FXI": 0.5, "US.GLD": 0.2}
        today = date(2026, 8, 14)

        self.assertEqual(
            momentum.live_leg_decision(leg, scores, None, None, today),
            "INITIAL",
        )
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores, "US.QQQ", "2026-08-13", today
            ),
            "NONE",
        )
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores, "US.SPY", "2026-08-13", today
            ),
            "NONE",
        )
        cooldown_expired = momentum.live_leg_decision(
            leg, scores, "US.SPY", "2026-07-31", today
        )
        self.assertEqual(cooldown_expired, "ROTATE")
        cooldown_blocked = momentum.live_leg_decision(
            leg, scores, "US.SPY", "2026-08-10", today
        )
        self.assertEqual(cooldown_blocked, "NONE")

    def test_leg_decision_cash_and_min_score_rules(self):
        leg = find_leg("US-B")
        scores = {
            "US.UUP": 0.9,
            "US.QQQ": 0.8,
            "US.FXI": 0.5,
            "US.GLD": 0.2,
        }
        today = date(2026, 8, 14)

        self.assertEqual(
            momentum.live_leg_decision(leg, scores, None, None, today),
            "INITIAL",
        )
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores, "US.QQQ", "2026-08-01", today
            ),
            "SELL",
        )
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores, "US.UUP", "2026-08-01", today
            ),
            "NONE",
        )
        scores_below_threshold = {
            "US.QQQ": 0.05,
            "US.UUP": 0.02,
            "US.FXI": 0.01,
            "US.GLD": 0.0,
        }
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores_below_threshold, "US.QQQ", "2026-08-01", today, 0.1
            ),
            "SELL",
        )
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores_below_threshold, None, None, today, 0.1
            ),
            "INITIAL",
        )
        scores_above_threshold = {
            "US.QQQ": 0.8,
            "US.UUP": 0.2,
            "US.FXI": 0.1,
            "US.GLD": 0.0,
        }
        self.assertEqual(
            momentum.live_leg_decision(
                leg, scores_above_threshold, None, None, today, 0.1
            ),
            "INITIAL",
        )

    def test_leg_decision_epsilon_blocks_narrow_gap(self):
        leg = find_leg("US-B")
        scores = {
            "US.QQQ": 0.80,
            "US.SPY": 0.75,
            "US.FXI": 0.50,
            "US.GLD": 0.20,
        }
        today = date(2026, 8, 14)

        self.assertEqual(
            momentum.live_leg_decision(leg, scores, "US.SPY", "2026-08-01", today),
            "NONE",
        )
        wide_gap = {
            "US.QQQ": 0.95,
            "US.SPY": 0.60,
            "US.FXI": 0.50,
            "US.GLD": 0.20,
        }
        self.assertEqual(
            momentum.live_leg_decision(leg, wide_gap, "US.SPY", "2026-08-01", today),
            "ROTATE",
        )
        reentry_from_cash_not_blocked = momentum.live_leg_decision(
            leg, wide_gap, "US.UUP", "2026-08-10", today
        )
        self.assertEqual(reentry_from_cash_not_blocked, "BUY")

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
    def test_state_rejects_changed_leg_configuration(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "state.json"
            state = momentum.LiveState(path, momentum.LIVE_LEGS)
            state.leg_state("US-A")["selected_symbol"] = "US.QQQ"
            state.save()

            restarted = momentum.LiveState(path, momentum.LIVE_LEGS)
            self.assertEqual(
                restarted.leg_state("US-A")["selected_symbol"], "US.QQQ"
            )

            tampered = [
                momentum.LiveLeg(
                    name=leg.name,
                    market=leg.market,
                    symbols=leg.symbols,
                    window=leg.window + 1,
                    cooldown=leg.cooldown,
                    gap_eps=leg.gap_eps,
                    cash_symbols=leg.cash_symbols,
                    slippage=leg.slippage,
                )
                for leg in momentum.LIVE_LEGS
            ]
            with self.assertRaisesRegex(ValueError, "配置不匹配"):
                momentum.LiveState(path, tuple(tampered))

    def test_runtime_directory_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            momentum.LiveRuntimePaths.from_argument("relative/runtime", "live")

    def test_runtime_uses_single_state_and_lock_files(self):
        runtime = momentum.LiveRuntimePaths.from_argument("/tmp/runtime", "live")

        self.assertEqual(runtime.state_file.name, "state-live.json")
        self.assertEqual(runtime.lock_file.name, "live.lock")


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
            "US.GLD": 90.0,
            "SZ.159941": 121.0,
            "SZ.159949": 111.0,
            "SH.510300": 80.0,
            "SH.510880": 100.0,
            "SH.518880": 100.0,
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
    def test_evaluates_all_four_legs_independently(self):
        fake_futu = types.ModuleType("futu")
        fake_futu.AuType = types.SimpleNamespace(QFQ="QFQ")
        fake_futu.OpenQuoteContext = FakeQuoteContext
        fake_futu.RET_OK = 0
        fake_futu.SubType = types.SimpleNamespace(K_DAY="K_DAY")
        notifier = RecordingNotifier()
        FakeQuoteContext.instances.clear()

        with tempfile.TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.ini"
            config_path.write_text(
                "[CONFIG]\nDATA_SOURCE=futu\nFUTU_HOST=127.0.0.1\nFUTU_PORT=11111\n",
                encoding="utf-8",
            )
            args = momentum.parse_args(
                [
                    "live",
                    "--runtime-dir",
                    raw_dir,
                    "--config",
                    str(config_path),
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

            state = momentum.LiveState(
                Path(raw_dir) / "state-live.json",
                momentum.LIVE_LEGS,
            )
            context = FakeQuoteContext.instances[0]

        self.assertEqual(len(notifier.events), 4)
        events_by_leg = {event["leg"]: event for event in notifier.events}
        self.assertEqual(set(events_by_leg), {"US-A", "US-B", "CN-A", "CN-B"})
        for leg, expected in (
            ("US-A", "US.QQQ"),
            ("US-B", "US.QQQ"),
            ("CN-A", "SZ.159941"),
            ("CN-B", "SZ.159941"),
        ):
            event = events_by_leg[leg]
            self.assertEqual(event["action"], "INITIAL")
            self.assertEqual(event["target_symbol"], expected)
            self.assertEqual(event["selected_symbol"], expected)
            self.assertEqual(event["evaluation_date"], "2026-08-14")
            self.assertEqual(event["market"], leg.split("-")[0])
            self.assertEqual(
                state.leg_state(leg)["selected_symbol"], expected
            )
            self.assertEqual(
                state.leg_state(leg)["last_evaluation_date"], "2026-08-14"
            )
        self.assertEqual(state.last_snapshot["type"], "SIGNAL")
        self.assertEqual(len(context.subscriptions), 2)
        self.assertEqual(
            set(context.subscriptions[0][0]),
            {"US.QQQ", "US.SPY", "US.FXI", "US.GLD", "US.UUP"},
        )
        self.assertEqual(
            set(context.subscriptions[1][0]),
            {
                "SZ.159941",
                "SZ.159949",
                "SH.510300",
                "SH.510880",
                "SH.518880",
            },
        )
        self.assertTrue(context.closed)
        self.assertTrue(notifier.closed)

    def test_on_holiday_records_idle_without_signal(self):
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
                    "live",
                    "--runtime-dir",
                    raw_dir,
                    "--config",
                    str(config_path),
                ]
            )
            with (
                patch.dict(sys.modules, {"futu": fake_futu}),
                patch.object(
                    momentum,
                    "build_live_notifier",
                    return_value=notifier,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(momentum.run_live(args), 0)

            state = momentum.LiveState(
                Path(raw_dir) / "state-live.json",
                momentum.LIVE_LEGS,
            )

        self.assertEqual(notifier.events, [])
        self.assertEqual(state.last_snapshot["type"], "IDLE")
        for leg in momentum.LIVE_LEGS:
            self.assertEqual(
                state.leg_state(leg.name)["last_evaluation_date"],
                date.today().isoformat(),
            )

    def test_with_markets_filter_evaluates_only_requested_market(self):
        fake_futu = types.ModuleType("futu")
        fake_futu.AuType = types.SimpleNamespace(QFQ="QFQ")
        fake_futu.OpenQuoteContext = FakeQuoteContext
        fake_futu.RET_OK = 0
        fake_futu.SubType = types.SimpleNamespace(K_DAY="K_DAY")
        notifier = RecordingNotifier()
        FakeQuoteContext.instances.clear()

        with tempfile.TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.ini"
            config_path.write_text(
                "[CONFIG]\nDATA_SOURCE=futu\nFUTU_HOST=127.0.0.1\nFUTU_PORT=11111\n",
                encoding="utf-8",
            )
            args = momentum.parse_args(
                [
                    "live",
                    "--markets",
                    "CN",
                    "--runtime-dir",
                    raw_dir,
                    "--config",
                    str(config_path),
                ]
            )
            with (
                patch.dict(sys.modules, {"futu": fake_futu}),
                patch.object(
                    momentum,
                    "build_live_notifier",
                    return_value=notifier,
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(momentum.run_live(args), 0)

            state = momentum.LiveState(
                Path(raw_dir) / "state-live.json",
                momentum.LIVE_LEGS,
            )
            context = FakeQuoteContext.instances[0]

        self.assertEqual(
            {event["leg"] for event in notifier.events},
            {"CN-A", "CN-B"},
        )
        self.assertEqual(len(context.subscriptions), 1)
        self.assertEqual(
            state.leg_state("US-A")["last_evaluation_date"], None
        )
        self.assertEqual(
            state.leg_state("CN-A")["last_evaluation_date"], "2026-08-14"
        )


if __name__ == "__main__":
    unittest.main()
