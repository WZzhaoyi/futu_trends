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

    def test_scalar_calibration_uses_next_session_open(self):
        deferred = timing.evaluate_candidate(
            self.bars,
            self.features,
            self.candidate,
            0.0,
        )
        self.assertEqual((deferred.buys, deferred.sells), (1, 1))
        self.assertAlmostEqual(deferred.strategy_return, 98.0 / 101.0 - 1)

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



class SnapshotTest(unittest.TestCase):
    def write(self, directory, **changes):
        payload = {
            "symbol": "SH.000902", "autype": "NONE", "bar_time_convention": "start",
            "bars": [
                {"code": "SH.000902", "time_key": "2026-07-01 09:30:00", "open": 1, "high": 2, "low": 1, "close": 2},
                {"code": "SH.000902", "time_key": "2026-07-01 09:45:00", "open": 2, "high": 3, "low": 2, "close": 3,
                 "volume": 5, "turnover": 7},
            ],
            **changes,
        }
        path = Path(directory) / "bars.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_start_convention_is_shifted_to_bar_end(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            bars = timing.load_bars(self.write(raw_dir), symbol="000902.SH")
        self.assertEqual([bar.key for bar in bars], ["2026-07-01 09:45", "2026-07-01 10:00"])
        self.assertEqual((bars[1].close, bars[1].volume, bars[1].turnover), (3.0, 5.0, 7.0))

    def test_other_symbol_adjustment_order_or_price_is_rejected(self):
        bad_order = [{"time_key": "2026-07-01 09:45:00", "open": 1, "high": 1, "low": 1, "close": 1}] * 2
        bad_price = [{"time_key": "2026-07-01 09:45:00", "open": 1, "high": 1, "low": 0, "close": 1}]
        cases = ({"symbol": "SH.000300"}, {"autype": "QFQ"}, {"bars": bad_order}, {"bars": bad_price})
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as raw_dir:
                with self.assertRaises(ValueError):
                    timing.read_snapshot(self.write(raw_dir, **changes), symbol="SH.000902")

class LiveRuntimeTest(unittest.TestCase):
    def test_runtime_paths_are_fixed_and_require_absolute_root(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            timing.LiveRuntimePaths.from_argument("relative/runtime")

        runtime = timing.LiveRuntimePaths.from_argument("/tmp/csi-flow")
        self.assertEqual(runtime.bars_file, Path("/tmp/csi-flow/calibration_bars.json"))
        self.assertEqual(runtime.thresholds_dir, Path("/tmp/csi-flow/thresholds"))
        self.assertEqual(runtime.state_file, Path("/tmp/csi-flow/state.json"))

    def test_market_month_uses_china_timezone(self):
        value = datetime(2026, 6, 30, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(
            timing.current_market_month(value).isoformat(),
            "2026-07-01",
        )


class PublicationIdentityTest(unittest.TestCase):
    def valid_publication(self):
        return {
            "schema_version": 3,
            "publication_kind": "monthly_threshold",
            "strategy": "m1",
            "strategy_status": timing.STRATEGY_STATUS,
            "version": "M1-LF-held-downside-exact-grid-strength-t1-defer-v2",
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
            threshold = timing.load_live_threshold(path, "SH.000902")
            self.assertEqual((threshold.month, threshold.source), ("2026-07", timing.VERSION))

            payload["strategy"] = "m0"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "阈值策略"):
                timing.load_live_threshold(path, "SH.000902")

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
    def fake_loader(self, path, symbol, window_months=timing.WINDOW_MONTHS):
        month = path.stem.removeprefix("threshold_")
        marker = len(path.read_text(encoding="utf-8"))
        return timing.Threshold(month=month, entry_z30=float(marker), entry_z60=0.0, exit_z30=0.0, exit_z60=0.0)

    def test_loads_exact_month_and_reloads_replaced_publication(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            publication = directory / "threshold_2026-07.json"
            publication.write_text("a", encoding="utf-8")
            provider = timing.ThresholdDirectoryProvider(directory, "SH.000902")
            with mock.patch.object(timing, "load_live_threshold", side_effect=self.fake_loader) as loader:
                first = provider.for_date("2026-07-02")
                cached = provider.for_date("2026-07-03")
                publication.write_text("longer", encoding="utf-8")
                reloaded = provider.for_date("2026-07-04")

            self.assertEqual(first.entry_z30, 1.0)
            self.assertEqual(cached.entry_z30, 1.0)
            self.assertEqual(reloaded.entry_z30, 6.0)
            self.assertEqual(loader.call_count, 2)

    def test_missing_month_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            directory = Path(raw_dir)
            (directory / "threshold_2026-07.json").write_text("a", encoding="utf-8")
            provider = timing.ThresholdDirectoryProvider(directory, "SH.000902")
            with self.assertRaisesRegex(ValueError, "live 自动发布"):
                provider.for_date("2026-08-03")


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


class FetchBarsTest(unittest.TestCase):
    def test_fetches_all_pages_and_publishes_input(self):
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
