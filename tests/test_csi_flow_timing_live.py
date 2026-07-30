import argparse
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "market_analysis"
    / "csi_flow_timing.py"
)
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
            "M1-LF-held-downside-rolling10-exact-grid",
        )
        self.assertEqual(timing.WINDOW_MONTHS, 10)
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
        live_args = parser.parse_args(
            [
                "live",
                "--symbol",
                "SH.000902",
                "--state-file",
                "/tmp/state.json",
                "--thresholds-file",
                "/tmp/threshold.json",
            ]
        )
        self.assertEqual(live_args.futu_time_convention, "end")


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


class ThresholdDirectoryProviderTest(unittest.TestCase):
    def fake_loader(self, path, symbol, allow_freeze):
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
            with self.assertRaisesRegex(ValueError, "月度定时任务"):
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
    def test_config_values_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "config.ini"
            path.write_text(
                "[CONFIG]\nFUTU_HOST=10.0.0.2\nFUTU_PORT=22222\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config=str(path), host=None, port=None
            )
            self.assertEqual(
                timing.live_connection(args), ("10.0.0.2", 22222)
            )
            args.host = "127.0.0.8"
            args.port = 33333
            self.assertEqual(
                timing.live_connection(args), ("127.0.0.8", 33333)
            )


class FetchBarsTest(unittest.TestCase):
    def test_fetches_all_pages_and_publishes_atomic_input(self):
        calls = []

        class FakeContext:
            def __init__(self, host, port):
                self.host = host
                self.port = port
                self.closed = False

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
                host="127.0.0.1",
                port=11111,
                config=None,
                symbol="SH.000902",
                output=str(output),
                futu_time_convention="end",
            )
            with mock.patch.dict(sys.modules, {"futu": fake_futu}):
                timing.run_fetch_bars(args)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["start"], "2025-08-01")
        self.assertEqual(payload["end"], "2026-06-30")
        self.assertEqual(payload["bar_time_convention"], "end")
        self.assertEqual(payload["autype"], "NONE")
        self.assertEqual(len(payload["bars"]), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["autype"], "NONE")
        self.assertIsNone(calls[0][1]["page_req_key"])
        self.assertEqual(calls[1][1]["page_req_key"], b"next")


if __name__ == "__main__":
    unittest.main()
