import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "momentum_rotation_strategy.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
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


def market_legs(market: str):
    return tuple(leg for leg in momentum.LIVE_LEGS if leg.market == market)


class LiveConfigurationTest(unittest.TestCase):
    def test_live_legs_match_deployed_configuration(self):
        us_a = find_leg("US-A")
        self.assertEqual(us_a.market, "US")
        self.assertEqual(
            us_a.symbols,
            ("US.QQQ", "US.FXI", "US.GLD", "US.UUP"),
        )
        self.assertEqual(us_a.window, 22)
        self.assertEqual(us_a.cooldown, 0)
        self.assertEqual(us_a.gap_eps, 0.49)
        self.assertEqual(us_a.cash_symbols, ("US.UUP",))
        self.assertEqual(us_a.slippage, momentum.DEFAULT_SLIPPAGE_US)

        us_b = find_leg("US-B")
        self.assertEqual(us_b.market, "US")
        self.assertEqual(
            us_b.symbols,
            ("US.QQQ", "US.SPY", "US.FXI", "US.GLD", "US.UUP"),
        )
        self.assertEqual(us_b.window, 22)
        self.assertEqual(us_b.cooldown, 0)
        self.assertEqual(us_b.gap_eps, 0.30)
        self.assertEqual(us_b.cash_symbols, ("US.UUP",))
        self.assertEqual(us_b.slippage, momentum.DEFAULT_SLIPPAGE_US)

        cn_a = find_leg("CN-A")
        self.assertEqual(cn_a.market, "CN")
        self.assertEqual(
            cn_a.symbols,
            ("SZ.159941", "SZ.159949", "SH.510300", "SH.518880"),
        )
        self.assertEqual(cn_a.window, 26)
        self.assertEqual(cn_a.cooldown, 3)
        self.assertEqual(cn_a.gap_eps, 0.0)
        self.assertEqual(cn_a.slippage, momentum.DEFAULT_SLIPPAGE_CN)

        cn_b = find_leg("CN-B")
        self.assertEqual(cn_b.market, "CN")
        self.assertEqual(
            cn_b.symbols,
            ("SZ.159941", "SZ.159949", "SH.510300", "SH.518880"),
        )
        self.assertEqual(cn_b.window, 24)
        self.assertEqual(cn_b.cooldown, 0)
        self.assertEqual(cn_b.gap_eps, 0.34)
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
        for name in (
            "etfs",
            "windows",
            "notification_time",
            "host",
            "port",
            "data_source",
        ):
            with self.subTest(option=name), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as rejected:
                    momentum.parse_args([
                        "live", "--runtime-dir", "/tmp/momentum",
                        "--" + name.replace("_", "-"), "override",
                    ])
                self.assertEqual(rejected.exception.code, 2)

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
    def test_momentum_score_matches_linear_log_price_fit(self):
        # log 价格为 [0, 0, log(1.01)]：斜率 log(1.01)/2，R²=3/4。
        score = momentum.calculate_momentum_score(np.array([1.0, 1.0, 1.01]))
        self.assertAlmostEqual(score, (1.01 ** 125 - 1) * 0.75)

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

    def test_decision_action_matrix(self):
        today = date(2026, 8, 14)
        growth_scores = {
            "SZ.159941": 0.8,
            "SZ.159949": 0.6,
            "SH.510300": 0.5,
            "SH.518880": 0.2,
        }
        cash_scores = {
            "US.UUP": 0.9,
            "US.QQQ": 0.8,
            "US.FXI": 0.5,
            "US.GLD": 0.2,
        }
        scores_below_threshold = {
            "US.QQQ": 0.05,
            "US.UUP": 0.02,
            "US.FXI": 0.01,
            "US.GLD": 0.0,
        }
        narrow_gap = {
            "US.QQQ": 0.80,
            "US.SPY": 0.75,
            "US.FXI": 0.50,
            "US.GLD": 0.20,
        }
        wide_gap = {
            "US.QQQ": 0.95,
            "US.SPY": 0.60,
            "US.FXI": 0.50,
            "US.GLD": 0.20,
        }
        cases = (
            (
                "initial",
                growth_scores,
                {"initialized": False},
                "INITIAL",
                "SZ.159941",
                False,
            ),
            (
                "unchanged",
                growth_scores,
                {"previous_state_symbol": "SZ.159941"},
                "NONE",
                "SZ.159941",
                False,
            ),
            (
                "cooldown blocked",
                growth_scores,
                {
                    "previous_state_symbol": "SH.510300",
                    "last_change_date": date(2026, 8, 12),
                    "cooldown": 3,
                },
                "NONE",
                "SH.510300",
                True,
            ),
            (
                "cooldown expired",
                growth_scores,
                {
                    "previous_state_symbol": "SH.510300",
                    "last_change_date": date(2026, 7, 31),
                    "cooldown": 3,
                },
                "ROTATE",
                "SZ.159941",
                False,
            ),
            (
                "cash selected",
                cash_scores,
                {
                    "cash_symbols": ("US.UUP",),
                    "previous_state_symbol": "US.QQQ",
                },
                "SELL",
                None,
                False,
            ),
            (
                "cash unchanged",
                cash_scores,
                {
                    "cash_symbols": ("US.UUP",),
                    "previous_state_symbol": "US.UUP",
                },
                "NONE",
                None,
                False,
            ),
            (
                "below minimum",
                scores_below_threshold,
                {"previous_state_symbol": "US.QQQ", "min_score": 0.1},
                "SELL",
                None,
                False,
            ),
            (
                "below minimum initial",
                scores_below_threshold,
                {"min_score": 0.1, "initialized": False},
                "INITIAL",
                None,
                False,
            ),
            (
                "epsilon blocked",
                narrow_gap,
                {"previous_state_symbol": "US.SPY", "gap_eps": 0.30},
                "NONE",
                "US.SPY",
                True,
            ),
            (
                "epsilon rotate",
                wide_gap,
                {"previous_state_symbol": "US.SPY", "gap_eps": 0.30},
                "ROTATE",
                "US.QQQ",
                False,
            ),
            (
                "cash reentry",
                wide_gap,
                {
                    "cash_symbols": ("US.UUP",),
                    "previous_state_symbol": "US.UUP",
                    "gap_eps": 0.30,
                },
                "BUY",
                "US.QQQ",
                False,
            ),
        )
        for name, scores, kwargs, action, target, blocked in cases:
            with self.subTest(case=name):
                decision = momentum.decide_rotation(
                    scores,
                    decision_date=today,
                    **kwargs,
                )
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.target_symbol, target)
                self.assertEqual(decision.blocked, blocked)


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
            momentum.LiveRuntimePaths.from_argument("relative/runtime", "CN")

    def test_runtime_uses_market_owned_state_and_lock_files(self):
        cn_runtime = momentum.LiveRuntimePaths.from_argument("/tmp/runtime", "CN")
        us_runtime = momentum.LiveRuntimePaths.from_argument("/tmp/runtime", "US")

        self.assertEqual(cn_runtime.state_file.name, "state-live-cn.json")
        self.assertEqual(cn_runtime.lock_file.name, "live-cn.lock")
        self.assertEqual(us_runtime.state_file.name, "state-live-us.json")
        self.assertEqual(us_runtime.lock_file.name, "live-us.lock")

    def test_state_drops_legacy_last_evaluation_date(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "state.json"
            state = momentum.LiveState(path, market_legs("CN"))
            state.leg_state("CN-A")["last_evaluation_date"] = "2026-08-14"
            state.save()

            restarted = momentum.LiveState(path, market_legs("CN"))

        self.assertNotIn(
            "last_evaluation_date",
            restarted.leg_state("CN-A"),
        )


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


class CompletingBarQuoteContext:
    def __init__(self):
        self.requested_counts = []

    def get_market_snapshot(self, symbols):
        return 0, pd.DataFrame(
            [
                {
                    "code": symbol,
                    "last_price": 123.0,
                    "update_time": "2026-08-14 14:00:00",
                }
                for symbol in symbols
            ]
        )

    def get_cur_kline(self, symbol, count, _subtype, _adjustment):
        self.requested_counts.append(count)
        return 0, pd.DataFrame(
            {
                "time_key": [
                    "2026-08-11 16:00:00",
                    "2026-08-12 16:00:00",
                    "2026-08-13 16:00:00",
                    "2026-08-14 16:00:00",
                ],
                "close": [10.0, 11.0, 12.0, 99.0],
            }
        )


class LiveMarketDataTest(unittest.TestCase):
    def test_scores_use_only_complete_daily_closes(self):
        context = CompletingBarQuoteContext()

        closes, snapshots = momentum.fetch_live_market_data(
            context,
            [("US.QQQ", 3)],
            0,
            types.SimpleNamespace(K_DAY="K_DAY"),
            types.SimpleNamespace(QFQ="QFQ"),
            date(2026, 8, 13),
        )

        self.assertEqual(context.requested_counts, [4])
        self.assertEqual(closes["US.QQQ"], [10.0, 11.0, 12.0])
        self.assertEqual(snapshots["US.QQQ"]["price"], 123.0)
        self.assertEqual(snapshots["US.QQQ"]["bar_date"], "2026-08-13")


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

            states = {
                market: momentum.LiveState(
                    Path(raw_dir) / f"state-live-{market.lower()}.json",
                    market_legs(market),
                )
                for market in ("US", "CN")
            }
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
                states[event["market"]].leg_state(leg)["selected_symbol"],
                expected,
            )
            self.assertEqual(
                states[event["market"]].leg_state(leg)["last_rotation_date"],
                "2026-08-14",
            )
        self.assertEqual(states["US"].last_snapshot["type"], "SIGNAL")
        self.assertEqual(states["CN"].last_snapshot["type"], "SIGNAL")
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

            states = {
                market: momentum.LiveState(
                    Path(raw_dir) / f"state-live-{market.lower()}.json",
                    market_legs(market),
                )
                for market in ("US", "CN")
            }

        self.assertEqual(notifier.events, [])
        self.assertEqual(states["US"].last_snapshot["type"], "IDLE")
        self.assertEqual(states["CN"].last_snapshot["type"], "IDLE")
        for market, state in states.items():
            self.assertEqual(state.last_snapshot["market"], market)

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
                Path(raw_dir) / "state-live-cn.json",
                market_legs("CN"),
            )
            context = FakeQuoteContext.instances[0]
            us_state_exists = (Path(raw_dir) / "state-live-us.json").exists()

        self.assertEqual(
            {event["leg"] for event in notifier.events},
            {"CN-A", "CN-B"},
        )
        self.assertEqual(len(context.subscriptions), 1)
        self.assertFalse(us_state_exists)
        self.assertEqual(state.leg_state("CN-A")["selected_symbol"], "SZ.159941")


if __name__ == "__main__":
    unittest.main()
