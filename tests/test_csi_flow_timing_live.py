import argparse
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "csi_flow_timing.py"
)
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("csi_flow_timing_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
timing = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = timing
SPEC.loader.exec_module(timing)


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


class LiveEventNotifierTest(unittest.TestCase):
    def test_only_actions_and_errors_are_sent_and_duplicates_are_throttled(self):
        engine = FakeNotificationEngine()
        notifier = timing.LiveEventNotifier(engine)
        base = {
            "symbol": "SH.000902",
            "emitted_at": "2026-07-01T10:30:00",
        }
        notifier.notify({**base, "type": "SIGNAL", "action": "NONE"})
        action = {
            **base,
            "type": "SIGNAL",
            "action": "BUY",
            "bar_key": "2026-07-01 10:30",
            "position_before": 0,
            "feature": {"z30": 1.0, "z60": 1.25},
            "threshold": {"month": "2026-07"},
            "window_months": 9,
            "strength_n": 9,
            "strength": {
                "ranking": [
                    {"rank": 1, "symbol": "SH.510500", "score": 0.01}
                ],
            },
        }
        notifier.notify(action)
        notifier.notify(action)
        notifier.notify({**base, "type": "ERROR", "message": "feed failed"})
        notifier.notify({**base, "type": "READY", "position": 0})
        notifier.close()

        self.assertEqual(len(engine.webhooks), 2)
        self.assertEqual(len(engine.telegrams), 2)
        self.assertEqual(len(engine.emails), 2)
        self.assertIn("[M1]", engine.webhooks[0])
        self.assertIn("SH.000902 BUY", engine.webhooks[0])
        self.assertIn("SH.510500", engine.webhooks[0])
        self.assertIn("feed failed", engine.webhooks[1])

    def test_lifecycle_notifications_are_opt_in(self):
        engine = FakeNotificationEngine()
        notifier = timing.LiveEventNotifier(engine, notify_lifecycle=True)
        notifier.notify(
            {
                "symbol": "SH.000902",
                "type": "READY",
                "position": 0,
                "emitted_at": "2026-07-01T09:00:00",
            }
        )
        notifier.close()
        self.assertEqual(len(engine.webhooks), 1)
        self.assertIn("READY", engine.webhooks[0])


class StrategyDefinitionTest(unittest.TestCase):
    def test_frozen_m1_definition_and_cli_defaults(self):
        self.assertEqual(timing.STRATEGY, "m1")
        self.assertEqual(
            timing.VERSION,
            "M1-LF-held-downside-exact-grid-strength-t1-defer-v2",
        )
        self.assertEqual(timing.PUBLISH_SCHEMA_VERSION, 3)
        self.assertEqual(timing.WINDOW_MONTHS, 9)
        self.assertEqual(timing.T1_SELL_MODE, "defer-next-open")
        self.assertEqual(timing.STRENGTH_N, 9)
        self.assertEqual(timing.GRID_POINTS, 8008)
        self.assertEqual(
            timing.GRID_POINTS,
            len(timing.ENTRY_Z30_GRID)
            * len(timing.ENTRY_Z60_GRID)
            * len(timing.EXIT_Z30_GRID)
            * len(timing.EXIT_Z60_GRID),
        )

        parser = timing.build_parser()
        fetch_args = parser.parse_args(
            [
                "fetch-bars",
                "--as-of",
                "2026-07-01",
                "--output",
                "/tmp/bars.json",
            ]
        )
        self.assertEqual(fetch_args.futu_time_convention, "end")
        self.assertEqual(fetch_args.window_months, 9)
        self.assertFalse(hasattr(fetch_args, "host"))
        self.assertFalse(hasattr(fetch_args, "port"))
        live_args = parser.parse_args(
            [
                "live",
                "--symbol",
                "SH.000902",
                "--runtime-dir",
                "/tmp/csi-flow",
            ]
        )
        self.assertEqual(live_args.futu_time_convention, "end")
        self.assertEqual(live_args.window_months, 9)
        self.assertEqual(live_args.strength_n, 9)
        self.assertEqual(live_args.notification_mode, "position-aware")
        self.assertEqual(live_args.t1_sell_mode, "defer-next-open")
        self.assertFalse(hasattr(live_args, "host"))
        self.assertFalse(hasattr(live_args, "port"))

        configured = parser.parse_args(
            [
                "live",
                "--symbol",
                "SH.000902",
                "--runtime-dir",
                "/tmp/csi-flow",
                "--window-months",
                "10",
                "--n",
                "11",
                "--notification-mode",
                "position-independent",
                "--t1-sell-mode",
                "ignore-same-day",
            ]
        )
        self.assertEqual(configured.window_months, 10)
        self.assertEqual(configured.strength_n, 11)
        self.assertEqual(configured.notification_mode, "position-independent")
        self.assertEqual(configured.t1_sell_mode, "ignore-same-day")


class DeferredT1StrategyTest(unittest.TestCase):
    def setUp(self):
        self.bars = [
            timing.Bar(
                key=key,
                open=price,
                high=price,
                low=price,
                close=price,
                symbol="SH.000902",
            )
            for key, price in (
                ("2026-07-01 10:30", 100.0),
                ("2026-07-01 10:45", 101.0),
                ("2026-07-01 14:00", 99.0),
                ("2026-07-02 09:45", 98.0),
            )
        ]
        self.features = [
            timing.Feature(1.0, 1.0, 0.0, 0.0, 0.1),
            None,
            timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1),
            None,
        ]
        self.candidate = timing.Candidate(0.0, 0.0, 0.0, 0.0)
        self.provider = timing.ThresholdProvider(
            {
                "2026-07": timing.Threshold(
                    month="2026-07",
                    entry_z30=0.0,
                    entry_z60=0.0,
                    exit_z30=0.0,
                    exit_z60=0.0,
                )
            }
        )

    def test_scalar_calibration_uses_next_session_open(self):
        deferred = timing.evaluate_candidate(
            self.bars,
            self.features,
            self.candidate,
            0.0,
            "defer-next-open",
        )
        legacy = timing.evaluate_candidate(
            self.bars,
            self.features,
            self.candidate,
            0.0,
            "ignore-same-day",
        )
        self.assertEqual((deferred.buys, deferred.sells), (1, 1))
        self.assertEqual((legacy.buys, legacy.sells), (1, 0))
        self.assertAlmostEqual(deferred.strategy_return, 98.0 / 101.0 - 1)

    def test_backtest_actions_record_original_signal_and_next_open(self):
        actions = timing.generate_actions(
            self.bars,
            self.features,
            self.provider,
            "2026-07-01",
            "2026-07-02",
            "defer-next-open",
        )
        self.assertEqual([action.side for action in actions], ["BUY", "SELL"])
        self.assertEqual(actions[-1].signal_key, "2026-07-01 14:00")
        self.assertEqual(actions[-1].execution_key, "2026-07-02 09:45")
        self.assertEqual(actions[-1].execution_price, 98.0)


class CoreTransitionTest(unittest.TestCase):
    def test_close_decision_covers_buy_sell_and_t1_defer(self):
        threshold = timing.Candidate(0.0, 0.0, 0.0, 0.0)
        buy = timing.Feature(1.0, 1.0, 0.0, 0.0, 0.1)
        sell = timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1)

        self.assertEqual(
            timing.decide_at_close(0, False, "10:30", buy, threshold),
            (True, False, "BUY", False),
        )
        self.assertEqual(
            timing.decide_at_close(1, True, "10:00", sell, threshold),
            (False, True, "SELL", False),
        )
        self.assertEqual(
            timing.decide_at_close(1, False, "10:00", sell, threshold),
            (False, True, None, True),
        )
    def test_batch_and_latest_feature_use_the_same_formula(self):
        bars = [
            timing.Bar(
                key=f"2026-07-{1 + index // 16:02d} 10:{index % 4 * 15:02d}",
                open=100 + index,
                high=100 + index,
                low=100 + index,
                close=100 + index,
            )
            for index in range(timing.VOLATILITY_BARS + 8)
        ]
        self.assertEqual(
            timing.latest_feature(bars),
            timing.build_features(bars)[-1],
        )

    def test_numpy_optimizer_matches_standard_library_fallback(self):
        bars = []
        features = []
        for cycle in range(5):
            buy_day = 1 + cycle * 2
            sell_day = buy_day + 1
            rows = (
                (
                    buy_day,
                    "10:30",
                    100,
                    100,
                    timing.Feature(10, 10, 0, 0, 0.1),
                ),
                (buy_day, "10:45", 100, 99, None),
                (
                    sell_day,
                    "10:00",
                    103,
                    103,
                    timing.Feature(-10, -10, 0, 0, 0.1),
                ),
                (sell_day, "10:15", 103, 103, None),
            )
            for day, clock, opening, close, feature in rows:
                bars.append(
                    timing.Bar(
                        key=f"2026-01-{day:02d} {clock}",
                        open=opening,
                        high=max(opening, close),
                        low=min(opening, close),
                        close=close,
                    )
                )
                features.append(feature)

        vectorized = timing.optimize_exhaustive_vectorized(bars, features)
        with mock.patch.object(timing, "np", None):
            fallback = timing.optimize_exhaustive_vectorized(bars, features)
        self.assertEqual(vectorized, fallback)


class StrengthRankingTest(unittest.TestCase):
    def bar(self, symbol, index, close):
        key = f"2026-07-01 {9 + (index // 4):02d}:{(index % 4) * 15:02d}"
        return timing.Bar(
            key=key,
            open=close,
            high=close,
            low=close,
            close=close,
            symbol=symbol,
        )

    def test_regression_return_matches_fitted_log_price_move(self):
        self.assertAlmostEqual(
            timing.regression_return_score([1.0, 2.0, 4.0]),
            3.0,
        )

    def test_rank_uses_common_completed_bars_and_ignores_future(self):
        paths = {
            "SH.510500": [1.00, 1.01, 1.02, 1.03, 1.04],
            "SH.510050": [1.00, 0.99, 0.98, 0.97, 10.00],
            "SH.510300": [1.00, 1.00, 1.00, 1.00, 1.00],
            "SH.512100": [1.00, 1.005, 1.01, 1.015, 1.02],
        }
        bars = {
            symbol: [
                self.bar(symbol, index, close)
                for index, close in enumerate(closes)
            ]
            for symbol, closes in paths.items()
        }
        signal_key = bars["SH.510500"][3].key
        result = timing.rank_strength(bars, signal_key, 4)

        self.assertEqual(result["observation_end"], signal_key)
        self.assertEqual(
            [item["symbol"] for item in result["ranking"]],
            ["SH.510500", "SH.512100", "SH.510300", "SH.510050"],
        )

    def test_live_buy_signal_only_hints_ranking_and_fixed_n(self):
        class Provider:
            def for_date(self, value):
                return timing.Threshold(
                    month=value[:7],
                    entry_z30=0.0,
                    entry_z60=0.0,
                    exit_z30=-1.0,
                    exit_z60=-1.0,
                )

        with tempfile.TemporaryDirectory() as raw_dir:
            state = timing.LiveState(Path(raw_dir) / "state.json", "flat", None)
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                Provider(),
                state,
                "end",
                window_months=10,
                strength_n=4,
                event_callback=events.append,
            )
            engine.strength_bars = {
                symbol: [
                    self.bar(symbol, index, close)
                    for index, close in enumerate(closes[:4])
                ]
                for symbol, closes in {
                    "SH.510500": [1.00, 1.01, 1.02, 1.03],
                    "SH.510050": [1.00, 0.99, 0.98, 0.97],
                    "SH.510300": [1.00, 1.00, 1.00, 1.00],
                    "SH.512100": [1.00, 1.005, 1.01, 1.015],
                }.items()
            }
            signal_bar = timing.Bar(
                key="2026-07-01 10:30",
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                symbol="SH.000902",
            )
            with mock.patch.object(
                timing,
                "latest_feature",
                return_value=timing.Feature(1.0, 1.0, 0.0, 0.0, 0.1),
            ):
                engine.finalize(signal_bar)

        self.assertEqual(
            state.pending,
            {
                "side": "BUY",
                "signal_key": "2026-07-01 10:30",
                "calibration_month": "2026-07",
            },
        )
        signal = [event for event in events if event["type"] == "SIGNAL"][-1]
        self.assertEqual(signal["strength_n"], 4)
        self.assertEqual(signal["window_months"], 10)
        self.assertEqual(
            signal["strength"]["ranking"][0]["symbol"],
            "SH.510500",
        )


class PositionIndependentSignalNotificationTest(unittest.TestCase):
    class Provider:
        def __init__(self, *, buy=False, sell=False):
            self.buy = buy
            self.sell = sell

        def for_date(self, value):
            return timing.Threshold(
                month=value[:7],
                entry_z30=0.0 if self.buy else 10.0,
                entry_z60=0.0 if self.buy else 10.0,
                exit_z30=0.0 if self.sell else -10.0,
                exit_z60=0.0 if self.sell else -10.0,
            )

    def bar(self, key):
        return timing.Bar(
            key=key,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            symbol="SH.000902",
        )

    def test_sell_notifies_while_flat_without_changing_position(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state = timing.LiveState(Path(raw_dir) / "state.json", "flat", None)
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                state,
                "end",
                notification_mode="position-independent",
                event_callback=events.append,
            )
            with mock.patch.object(
                timing,
                "latest_feature",
                return_value=timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1),
            ):
                engine.finalize(self.bar("2026-07-01 10:00"))

            signal = [event for event in events if event["type"] == "SIGNAL"][-1]
            self.assertEqual(signal["action"], "SELL")
            self.assertEqual(signal["position_before"], 0)
            self.assertIsNone(state.pending)
            self.assertEqual(state.position, 0)

    def test_position_aware_mode_keeps_original_position_filter(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state = timing.LiveState(Path(raw_dir) / "state.json", "flat", None)
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                state,
                "end",
                notification_mode="position-aware",
                event_callback=events.append,
            )
            with mock.patch.object(
                timing,
                "latest_feature",
                return_value=timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1),
            ):
                engine.finalize(self.bar("2026-07-01 10:00"))

            signal = [event for event in events if event["type"] == "SIGNAL"][-1]
            self.assertEqual(signal["action"], "NONE")
            self.assertEqual(state.signal_notification_dates, {})

    def test_t1_sell_waits_for_next_open_then_notifies_and_syncs_state(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state_path = Path(raw_dir) / "state.json"
            state = timing.LiveState(state_path, "long", "2026-07-01")
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                state,
                "end",
                notification_mode="position-aware",
                event_callback=events.append,
            )
            feature = timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1)
            with mock.patch.object(timing, "latest_feature", return_value=feature):
                engine.finalize(self.bar("2026-07-01 10:00"))
                engine.finalize(self.bar("2026-07-01 10:30"))

            signals = [
                event for event in events if event["type"] == "SIGNAL"
            ]
            self.assertEqual(
                [event["action"] for event in signals],
                ["NONE", "NONE"],
            )
            self.assertFalse(signals[0]["t1_sellable"])
            self.assertIsNone(state.pending)
            self.assertEqual(state.position, 1)
            self.assertEqual(
                state.deferred_t1_sell["signal_key"],
                "2026-07-01 10:00",
            )
            self.assertNotIn("SELL", state.signal_notification_dates)

            restarted_state = timing.LiveState(state_path, "flat", None)
            restarted_events = []
            restarted = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                restarted_state,
                "end",
                notification_mode="position-aware",
                event_callback=restarted_events.append,
            )
            restarted.execute_deferred_t1_sell(
                self.bar("2026-07-02 09:45")
            )

            opening_signal = [
                event
                for event in restarted_events
                if event["type"] == "SIGNAL"
            ][-1]
            self.assertEqual(opening_signal["action"], "SELL")
            self.assertEqual(opening_signal["execution"], "CURRENT_BAR_OPEN")
            self.assertTrue(opening_signal["t1_sellable"])
            self.assertIsNone(restarted_state.pending)
            self.assertIsNone(restarted_state.deferred_t1_sell)
            self.assertEqual(restarted_state.position, 0)
            self.assertIsNone(restarted_state.entry_date)
            self.assertEqual(
                restarted_state.signal_notification_dates["SELL"],
                "2026-07-02",
            )

    def test_position_independent_t1_sell_also_waits_for_next_open(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state = timing.LiveState(
                Path(raw_dir) / "state.json", "long", "2026-07-01"
            )
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                state,
                "end",
                notification_mode="position-independent",
                event_callback=events.append,
            )
            feature = timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1)
            with mock.patch.object(timing, "latest_feature", return_value=feature):
                engine.finalize(self.bar("2026-07-01 10:00"))

            self.assertEqual(events[-1]["action"], "NONE")
            self.assertIsNotNone(state.deferred_t1_sell)

            engine.execute_deferred_t1_sell(self.bar("2026-07-02 09:45"))
            opening_signal = [
                event for event in events if event["type"] == "SIGNAL"
            ][-1]
            self.assertEqual(opening_signal["action"], "SELL")
            self.assertEqual(state.position, 0)
            self.assertIsNone(state.deferred_t1_sell)

    def test_buy_notifies_while_long_without_replacing_position(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state = timing.LiveState(
                Path(raw_dir) / "state.json", "long", "2026-06-30"
            )
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(buy=True),
                state,
                "end",
                notification_mode="position-independent",
                event_callback=events.append,
            )
            ranking = {"ranking": [{"rank": 1, "symbol": "SH.510500", "score": 0.01}]}
            with (
                mock.patch.object(
                    timing,
                    "latest_feature",
                    return_value=timing.Feature(1.0, 1.0, 0.0, 0.0, 0.1),
                ),
                mock.patch.object(engine, "current_strength", return_value=ranking),
            ):
                engine.finalize(self.bar("2026-07-01 10:30"))

            signal = [event for event in events if event["type"] == "SIGNAL"][-1]
            self.assertEqual(signal["action"], "BUY")
            self.assertEqual(signal["strength"], ranking)
            self.assertIsNone(state.pending)
            self.assertEqual(state.position, 1)

    def test_same_side_notifies_once_per_day_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state_path = Path(raw_dir) / "state.json"
            state = timing.LiveState(state_path, "flat", None)
            events = []
            engine = timing.LiveSignalEngine(
                "SH.000902",
                self.Provider(sell=True),
                state,
                "end",
                notification_mode="position-independent",
                event_callback=events.append,
            )
            feature = timing.Feature(-1.0, -1.0, 0.0, 0.0, 0.1)
            with mock.patch.object(timing, "latest_feature", return_value=feature):
                engine.finalize(self.bar("2026-07-01 10:00"))
                engine.finalize(self.bar("2026-07-01 10:30"))

                restarted_state = timing.LiveState(state_path, "flat", None)
                restarted_events = []
                restarted = timing.LiveSignalEngine(
                    "SH.000902",
                    self.Provider(sell=True),
                    restarted_state,
                    "end",
                    notification_mode="position-independent",
                    event_callback=restarted_events.append,
                )
                restarted.finalize(self.bar("2026-07-01 14:00"))
                restarted.finalize(self.bar("2026-07-02 10:00"))

            self.assertEqual(
                [event["action"] for event in events if event["type"] == "SIGNAL"],
                ["SELL", "NONE"],
            )
            self.assertEqual(
                [
                    event["action"]
                    for event in restarted_events
                    if event["type"] == "SIGNAL"
                ],
                ["NONE", "SELL"],
            )
            stored = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["signal_notification_dates"]["SELL"], "2026-07-02")


class LiveRuntimeTest(unittest.TestCase):
    def test_runtime_paths_are_fixed_and_require_absolute_root(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            timing.LiveRuntimePaths.from_argument("relative/runtime")

        runtime = timing.LiveRuntimePaths.from_argument("/tmp/csi-flow")
        self.assertEqual(runtime.bars_file, Path("/tmp/csi-flow/calibration_bars.json"))
        self.assertEqual(runtime.thresholds_dir, Path("/tmp/csi-flow/thresholds"))
        self.assertEqual(runtime.state_file, Path("/tmp/csi-flow/state.json"))

    def test_runtime_lock_rejects_a_second_live_instance(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "live.lock"
            with timing.runtime_file_lock(path):
                with self.assertRaisesRegex(RuntimeError, "已有 live 实例"):
                    with timing.runtime_file_lock(path):
                        self.fail("second lock unexpectedly acquired")
            with timing.runtime_file_lock(path):
                pass

    def test_market_month_uses_china_timezone(self):
        value = datetime(2026, 6, 30, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(
            timing.current_market_month(value).isoformat(),
            "2026-07-01",
        )


class PublicationAndStateIdentityTest(unittest.TestCase):
    def valid_publication(self):
        return {
            "schema_version": timing.PUBLISH_SCHEMA_VERSION,
            "publication_kind": "monthly_threshold",
            "strategy": timing.STRATEGY,
            "strategy_status": timing.STRATEGY_STATUS,
            "version": timing.VERSION,
            "script_sha256": timing.sha256_file(MODULE_PATH),
            "symbol": "000902.SH",
            "month": "2026-07",
            "available_from": "2026-07-01",
            "window_start": "2025-09-01",
            "window_end": "2026-06-30",
            "search_method": timing.SEARCH_METHOD,
            "window_months": timing.WINDOW_MONTHS,
            "t1_sell_mode": timing.T1_SELL_MODE,
            "grid_points": timing.GRID_POINTS,
            "threshold": {
                "month": "2026-07",
                "entry_z30": 0.0,
                "entry_z60": 1.5,
                "exit_z30": -0.25,
                "exit_z60": 0.25,
                "source": timing.VERSION,
            },
            "training_evaluation": {
                "score": 1.0,
                "buys": 4,
                "sells": 4,
                "exposure": 0.5,
            },
            "constraint_strategy_return": 0.0,
            "search_evaluations": timing.GRID_POINTS,
            "data_audit": {
                "source_file_sha256": "0" * 64,
                "training_bars_sha256": "1" * 64,
            },
        }

    def test_valid_publication_loads_and_m0_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "threshold_2026-07.json"
            payload = self.valid_publication()
            path.write_text(json.dumps(payload), encoding="utf-8")
            provider = timing.load_live_threshold_provider(
                path, "SH.000902", allow_freeze=False
            )
            self.assertEqual(
                provider.for_date("2026-07-02").source,
                timing.VERSION,
            )

            payload["strategy"] = "m0"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "阈值策略"):
                timing.load_live_threshold_provider(
                    path, "SH.000902", allow_freeze=False
                )

    def test_state_is_m1_scoped_and_rejects_legacy_identity(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state_path = Path(raw_dir) / "state.json"
            state = timing.LiveState(state_path, "flat", None)
            stored = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["strategy"], "m1")
            self.assertEqual(stored["version"], timing.VERSION)
            self.assertEqual(state.position, 0)

            stored["strategy"] = "m0"
            state_path.write_text(json.dumps(stored), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "状态文件策略"):
                timing.LiveState(state_path, "flat", None)

    def test_previous_m1_state_version_migrates_without_losing_position(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            state_path = Path(raw_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "strategy": "m1",
                        "version": "M1-LF-held-downside-exact-grid-strength-v1",
                        "position": 1,
                        "entry_date": "2026-08-04",
                        "pending": None,
                    }
                ),
                encoding="utf-8",
            )
            state = timing.LiveState(state_path, "flat", None)
            stored = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(state.position, 1)
            self.assertEqual(state.entry_date, "2026-08-04")
            self.assertEqual(stored["version"], timing.VERSION)
            self.assertIsNone(stored["deferred_t1_sell"])
            self.assertEqual(stored["signal_notification_dates"], {})

    def live_args(self):
        return argparse.Namespace(
            symbol="SH.000902",
            config=None,
            futu_time_convention="end",
        )

    def test_live_threshold_skips_valid_current_publication(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = timing.LiveRuntimePaths.from_argument(raw_dir)
            runtime.prepare()
            publication = runtime.thresholds_dir / "threshold_2026-07.json"
            publication.write_text(
                json.dumps(self.valid_publication()),
                encoding="utf-8",
            )
            with (
                mock.patch.object(timing, "fetch_calibration_bars") as fetch,
                mock.patch.object(timing, "publish_calibration") as calibrate,
            ):
                month, generated = timing.ensure_live_threshold(
                    self.live_args(),
                    runtime,
                    datetime(2026, 7, 2, tzinfo=timezone.utc),
                )
            self.assertEqual(month, "2026-07")
            self.assertFalse(generated)
            fetch.assert_not_called()
            calibrate.assert_not_called()

    def test_live_threshold_fetches_and_calibrates_when_missing(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = timing.LiveRuntimePaths.from_argument(raw_dir)
            calls = []

            def fake_fetch(**values):
                calls.append(("fetch", values["as_of"], values["output"]))
                Path(values["output"]).write_text("{}", encoding="utf-8")
                return {}

            def fake_calibrate(**values):
                calls.append(
                    ("calibrate", values["as_of"], values["output"])
                )
                Path(values["output"]).parent.mkdir(
                    parents=True, exist_ok=True
                )
                Path(values["output"]).write_text(
                    json.dumps(self.valid_publication()),
                    encoding="utf-8",
                )
                return self.valid_publication()

            with (
                mock.patch.object(
                    timing,
                    "fetch_calibration_bars",
                    side_effect=fake_fetch,
                ),
                mock.patch.object(
                    timing,
                    "publish_calibration",
                    side_effect=fake_calibrate,
                ),
            ):
                month, generated = timing.ensure_live_threshold(
                    self.live_args(),
                    runtime,
                    datetime(2026, 7, 2, tzinfo=timezone.utc),
                )
            self.assertEqual(month, "2026-07")
            self.assertTrue(generated)
            self.assertEqual(
                [call[0] for call in calls],
                ["fetch", "calibrate"],
            )


class ThresholdDirectoryProviderTest(unittest.TestCase):
    def fake_loader(
        self,
        path,
        symbol,
        allow_freeze,
        window_months=timing.WINDOW_MONTHS,
        t1_sell_mode=timing.T1_SELL_MODE,
    ):
        month = path.stem.removeprefix("threshold_")
        marker = len(path.read_text(encoding="utf-8"))
        threshold = timing.Threshold(
            month=month,
            entry_z30=float(marker),
            entry_z60=0.0,
            exit_z30=0.0,
            exit_z60=0.0,
        )
        return timing.ThresholdProvider({month: threshold})

    def test_loads_exact_month_and_reloads_replaced_publication(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            publication = directory / "threshold_2026-07.json"
            publication.write_text("a", encoding="utf-8")
            provider = timing.ThresholdDirectoryProvider(
                directory, "SH.000902", allow_freeze=False
            )
            with mock.patch.object(
                timing,
                "load_live_threshold_provider",
                side_effect=self.fake_loader,
            ) as loader:
                first = provider.for_date("2026-07-02")
                cached = provider.for_date("2026-07-03")
                publication.write_text("longer", encoding="utf-8")
                reloaded = provider.for_date("2026-07-04")

            self.assertEqual(first.entry_z30, 1.0)
            self.assertEqual(cached.entry_z30, 1.0)
            self.assertEqual(reloaded.entry_z30, 6.0)
            self.assertEqual(loader.call_count, 2)

    def test_missing_month_fails_closed_or_explicitly_freezes(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            (directory / "threshold_2026-07.json").write_text(
                "a", encoding="utf-8"
            )
            strict = timing.ThresholdDirectoryProvider(
                directory, "SH.000902", allow_freeze=False
            )
            with self.assertRaisesRegex(ValueError, "live 自动发布"):
                strict.for_date("2026-08-03")

            frozen = timing.ThresholdDirectoryProvider(
                directory, "SH.000902", allow_freeze=True
            )
            with mock.patch.object(
                timing,
                "load_live_threshold_provider",
                side_effect=self.fake_loader,
            ):
                threshold = frozen.for_date("2026-08-03")
            self.assertEqual(threshold.month, "2026-08")
            self.assertEqual(threshold.source, "frozen:2026-07")


class LiveConnectionTest(unittest.TestCase):
    def test_config_values_or_defaults(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "config.ini"
            path.write_text(
                "[CONFIG]\nFUTU_HOST=10.0.0.2\nFUTU_PORT=22222\n",
                encoding="utf-8",
            )
            self.assertEqual(
                timing.resolve_live_connection(str(path)),
                ("10.0.0.2", 22222),
            )
        self.assertEqual(
            timing.resolve_live_connection(None),
            ("127.0.0.1", 11111),
        )


class LiveContextCleanupTest(unittest.TestCase):
    def test_callback_failure_exits_and_closes_context_once(self):
        contexts = []

        class FakeCurKlineHandlerBase:
            def on_recv_rsp(self, _rsp_pb):
                return -1, "callback failed"

        class FakeSysNotifyHandlerBase:
            def on_recv_rsp(self, _rsp_pb):
                return 0, ("CONN_STATUS", None, {"qot_logined": True})

        class FakeContext:
            def __init__(self, host, port):
                self.handlers = []
                self.subscribe_count = 0
                self.close_count = 0
                contexts.append(self)

            def subscribe(self, *_args, **_kwargs):
                self.subscribe_count += 1
                if self.subscribe_count == 2:
                    handler = next(
                        item
                        for item in self.handlers
                        if isinstance(item, FakeCurKlineHandlerBase)
                    )
                    handler.on_recv_rsp(None)
                return 0, "ok"

            def get_cur_kline(self, *_args, **_kwargs):
                return 0, []

            def set_handler(self, handler):
                self.handlers.append(handler)
                return 0

            def close(self):
                self.close_count += 1

        fake_futu = types.SimpleNamespace(
            AuType=types.SimpleNamespace(NONE="NONE", QFQ="QFQ"),
            CurKlineHandlerBase=FakeCurKlineHandlerBase,
            KLType=types.SimpleNamespace(K_15M="K_15M"),
            OpenQuoteContext=FakeContext,
            RET_ERROR=-1,
            RET_OK=0,
            SubType=types.SimpleNamespace(K_15M="K_15M"),
            SysNotifyHandlerBase=FakeSysNotifyHandlerBase,
            SysNotifyType=types.SimpleNamespace(CONN_STATUS="CONN_STATUS"),
        )
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = timing.LiveRuntimePaths.from_argument(raw_dir)
            runtime.prepare()
            args = argparse.Namespace(
                symbol="SH.000902",
                config=None,
                history_bars=200,
                duration=5,
                initial_position="flat",
                entry_date=None,
                futu_time_convention="end",
            )
            with (
                mock.patch.dict(sys.modules, {"futu": fake_futu}),
                mock.patch.object(
                    timing,
                    "ThresholdDirectoryProvider",
                    return_value=object(),
                ),
                mock.patch.object(
                    timing.LiveSignalEngine,
                    "bootstrap",
                    return_value=None,
                ),
                mock.patch.object(
                    timing.LiveSignalEngine,
                    "bootstrap_strength",
                    return_value=None,
                ),
                mock.patch.object(
                    timing.LiveSignalEngine,
                    "emit",
                    return_value=None,
                ),
                self.assertRaisesRegex(RuntimeError, "callback failed"),
            ):
                timing._run_live(
                    args,
                    None,
                    runtime,
                    timing.sha256_file(MODULE_PATH),
                    timing.threading.Event(),
                )
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].close_count, 1)


class FetchBarsTest(unittest.TestCase):
    def test_fetches_all_pages_and_publishes_atomic_input(self):
        calls = []
        contexts = []

        class FakeContext:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                self.closed = False
                contexts.append(self)

            def request_history_kline(self, symbol, **kwargs):
                calls.append((symbol, kwargs))
                minute = "09:30:00" if len(calls) == 1 else "09:45:00"
                page = b"next" if len(calls) == 1 else None
                return (
                    0,
                    [
                        {
                            "code": symbol,
                            "time_key": f"2026-06-30 {minute}",
                            "open": 1,
                            "high": 2,
                            "low": 1,
                            "close": 2,
                            "volume": 10,
                            "turnover": 20,
                        }
                    ],
                    page,
                )

            def close(self):
                self.closed = True

        fake_futu = types.SimpleNamespace(
            AuType=types.SimpleNamespace(NONE="NONE"),
            KLType=types.SimpleNamespace(K_15M="K_15M"),
            OpenQuoteContext=FakeContext,
            RET_OK=0,
        )
        with tempfile.TemporaryDirectory() as raw_dir:
            output = Path(raw_dir) / "bars.json"
            args = argparse.Namespace(
                as_of="2026-07-01",
                start=None,
                end=None,
                config=None,
                symbol="SH.000902",
                output=str(output),
                futu_time_convention="end",
                window_months=10,
            )
            with mock.patch.dict(sys.modules, {"futu": fake_futu}):
                timing.run_fetch_bars(args)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["start"], "2025-08-01")
        self.assertEqual(payload["end"], "2026-06-30")
        self.assertEqual(payload["bar_time_convention"], "end")
        self.assertEqual(payload["autype"], "NONE")
        self.assertEqual(payload["window_months"], 10)
        self.assertEqual(len(payload["bars"]), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["autype"], "NONE")
        self.assertIsNone(calls[0][1]["page_req_key"])
        self.assertEqual(calls[1][1]["page_req_key"], b"next")
        self.assertTrue(contexts[0].closed)

    def test_fetch_closes_context_when_futu_request_fails(self):
        contexts = []

        class FakeContext:
            def __init__(self, host, port):
                self.closed = False
                contexts.append(self)

            def request_history_kline(self, *_args, **_kwargs):
                return -1, "request failed", None

            def close(self):
                self.closed = True

        fake_futu = types.SimpleNamespace(
            AuType=types.SimpleNamespace(NONE="NONE"),
            KLType=types.SimpleNamespace(K_15M="K_15M"),
            OpenQuoteContext=FakeContext,
            RET_OK=0,
        )
        args = argparse.Namespace(
            as_of="2026-07-01",
            start=None,
            end=None,
            config=None,
            symbol="SH.000902",
            output="/tmp/not-written.json",
            futu_time_convention="end",
        )
        with (
            mock.patch.dict(sys.modules, {"futu": fake_futu}),
            self.assertRaisesRegex(RuntimeError, "request failed"),
        ):
            timing.run_fetch_bars(args)
        self.assertEqual(len(contexts), 1)
        self.assertTrue(contexts[0].closed)


if __name__ == "__main__":
    unittest.main()
