import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.pm2_service import (
    PM2ConfigError,
    build_pm2_args,
    build_service_spec,
    main,
    resolve_pm2_invocation,
)


class PM2ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = Path(self.temp_dir.name) / "signal api.ini"
        self.config.write_text("[CONFIG]\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_order_engine_identity_matches_ecosystem_contract(self):
        spec = build_service_spec("order-engine", str(self.config))
        digest = hashlib.sha256(str(self.config.absolute()).encode()).hexdigest()[:8]
        self.assertEqual(spec.instance_name, f"futu-order-signal-api-{digest}")
        self.assertEqual(spec.environment, {"ORDER_ENGINE_CONFIG": str(self.config.absolute())})

    def test_signal_api_identity_includes_port(self):
        spec = build_service_spec("signal-api", str(self.config), port=18001)
        identity = f"{self.config.absolute()}\0{18001}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        self.assertEqual(spec.instance_name, f"futu-signal-api-signal-api-18001-{digest}")
        self.assertEqual(spec.environment["SIGNAL_API_PORT"], "18001")

    def test_csi_flow_identity_and_environment(self):
        runtime = Path(self.temp_dir.name) / "runtime"
        spec = build_service_spec(
            "csi-flow",
            str(self.config),
            runtime_dir=str(runtime.absolute()),
            symbol="sh.000902",
        )
        identity = "\0".join(
            (
                str(self.config.absolute()),
                str(runtime.absolute()),
                "SH.000902",
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        self.assertEqual(spec.instance_name, f"futu-csi-flow-{digest}")
        self.assertEqual(
            spec.environment,
            {
                "CSI_FLOW_CONFIG": str(self.config.absolute()),
                "CSI_FLOW_RUNTIME_DIR": str(runtime.absolute()),
                "CSI_FLOW_SYMBOL": "SH.000902",
                "CSI_FLOW_INITIAL_POSITION": "flat",
                "CSI_FLOW_NOTIFICATION_MODE": "position-aware",
                "CSI_FLOW_WINDOW_MONTHS": "9",
                "CSI_FLOW_T1_SELL_MODE": "defer-next-open",
            },
        )
        self.assertEqual(
            spec.ecosystem_path.name,
            "ecosystem.csi-flow.config.js",
        )
        self.assertTrue(spec.ecosystem_path.is_file())

    def test_etf_premium_identity_and_environment(self):
        runtime = Path(self.temp_dir.name) / "etf-runtime"
        strategy = Path(self.temp_dir.name) / "strategy.json"
        strategy.write_text("{}\n", encoding="utf-8")
        cache = Path(self.temp_dir.name) / "cache"
        spec = build_service_spec(
            "etf-premium",
            str(self.config),
            runtime_dir=str(runtime.absolute()),
            symbol="159941",
            initial_position="low",
            strategy_file=str(strategy),
            cache_dir=str(cache),
        )
        identity = "\0".join(
            (
                str(self.config.absolute()),
                str(runtime.absolute()),
                "159941",
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        self.assertEqual(
            spec.instance_name,
            f"futu-etf-premium-{digest}",
        )
        self.assertEqual(
            spec.environment["ETF_PREMIUM_RUNTIME_DIR"],
            str(runtime.absolute()),
        )
        self.assertEqual(
            spec.environment["ETF_PREMIUM_STRATEGY_FILE"],
            str(strategy.absolute()),
        )
        self.assertEqual(spec.environment["ETF_PREMIUM_INITIAL_POSITION"], "low")
        self.assertEqual(spec.environment["ETF_PREMIUM_MAX_NAV_AGE"], "14")
        self.assertEqual(
            spec.ecosystem_path.name,
            "ecosystem.etf-premium.config.js",
        )
        self.assertTrue(spec.ecosystem_path.is_file())

    def test_momentum_rotation_identity_and_environment(self):
        runtime = Path(self.temp_dir.name) / "momentum-runtime"
        spec = build_service_spec(
            "momentum-rotation",
            str(self.config),
            runtime_dir=str(runtime.absolute()),
            live_mode="live-cn",
        )
        identity = "\0".join(
            (
                str(self.config.absolute()),
                str(runtime.absolute()),
                "live-cn",
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        self.assertEqual(
            spec.instance_name,
            f"futu-momentum-rotation-{digest}",
        )
        self.assertEqual(spec.environment["MOMENTUM_ROTATION_MODE"], "live-cn")
        self.assertEqual(
            spec.environment["MOMENTUM_ROTATION_RUNTIME_DIR"],
            str(runtime.absolute()),
        )
        self.assertEqual(
            spec.environment["MOMENTUM_ROTATION_MAX_QUOTE_AGE"],
            "14400.0",
        )
        self.assertEqual(
            spec.ecosystem_path.name,
            "ecosystem.momentum-rotation.config.js",
        )
        self.assertTrue(spec.ecosystem_path.is_file())

    def test_identity_does_not_dereference_config_symlink(self):
        link = Path(self.temp_dir.name) / "linked.ini"
        try:
            link.symlink_to(self.config)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        spec = build_service_spec("order-engine", str(link))
        digest = hashlib.sha256(str(link.absolute()).encode()).hexdigest()[:8]
        self.assertEqual(spec.config_path, link.absolute())
        self.assertEqual(spec.instance_name, f"futu-order-linked-{digest}")

    def test_start_and_status_pm2_arguments(self):
        spec = build_service_spec("signal-api", str(self.config))
        expected = [
            "start",
            str(spec.ecosystem_path),
            "--only",
            spec.instance_name,
            "--update-env",
        ]
        self.assertEqual(build_pm2_args("start", spec), expected)
        self.assertEqual(build_pm2_args("restart", spec), expected)
        self.assertEqual(build_pm2_args("status", spec), ["describe", spec.instance_name])

    def test_missing_config_and_invalid_port_fail(self):
        with self.assertRaises(PM2ConfigError):
            build_service_spec("order-engine", str(self.config) + ".missing")
        with self.assertRaises(PM2ConfigError):
            build_service_spec("signal-api", str(self.config), port=65536)
        with self.assertRaises(PM2ConfigError):
            build_service_spec(
                "csi-flow",
                str(self.config),
                runtime_dir="relative/path",
            )
        with self.assertRaises(PM2ConfigError):
            build_service_spec(
                "csi-flow",
                str(self.config),
                runtime_dir=str(Path(self.temp_dir.name).absolute()),
                initial_position="long",
            )
        with self.assertRaises(PM2ConfigError):
            build_service_spec(
                "etf-premium",
                str(self.config),
                runtime_dir="relative/path",
                symbol="159941",
                initial_position="base",
            )
        with self.assertRaises(PM2ConfigError):
            build_service_spec(
                "momentum-rotation",
                str(self.config),
                runtime_dir=str(Path(self.temp_dir.name).absolute()),
                live_mode="custom",
            )
        with self.assertRaises(PM2ConfigError):
            build_service_spec(
                "etf-premium",
                str(self.config),
                runtime_dir=str(Path(self.temp_dir.name).absolute()),
                symbol="SZ.159941",
                initial_position="base",
            )

    def test_unix_pm2_override(self):
        invocation = resolve_pm2_invocation(
            ["status"], environ={"PM2_BIN": "/custom/pm2"}, platform="darwin",
        )
        self.assertEqual(invocation, ["/custom/pm2", "status"])

    @patch("cli.pm2_service.run_pm2", return_value=7)
    def test_main_passes_through_pm2_exit_code(self, run_pm2_mock):
        rc = main(["signal-api", "restart", "--config", str(self.config), "--port", "9001"])
        self.assertEqual(rc, 7)
        args, env = run_pm2_mock.call_args.args
        self.assertEqual(args[0], "start")
        self.assertEqual(env["SIGNAL_API_PORT"], "9001")

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_main_builds_csi_flow_service(self, run_pm2_mock):
        runtime = Path(self.temp_dir.name) / "runtime"
        rc = main(
            [
                "csi-flow",
                "start",
                "--config",
                str(self.config),
                "--runtime-dir",
                str(runtime.absolute()),
                "--notification-mode",
                "position-independent",
                "--window-months",
                "9",
                "--t1-sell-mode",
                "defer-next-open",
            ]
        )
        self.assertEqual(rc, 0)
        args, env = run_pm2_mock.call_args.args
        self.assertEqual(args[0], "start")
        self.assertEqual(env["CSI_FLOW_SYMBOL"], "SH.000902")
        self.assertEqual(
            env["CSI_FLOW_NOTIFICATION_MODE"],
            "position-independent",
        )
        self.assertEqual(env["CSI_FLOW_WINDOW_MONTHS"], "9")
        self.assertEqual(env["CSI_FLOW_T1_SELL_MODE"], "defer-next-open")
        self.assertEqual(
            env["CSI_FLOW_RUNTIME_DIR"],
            str(runtime.absolute()),
        )

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_main_builds_etf_premium_service(self, run_pm2_mock):
        runtime = Path(self.temp_dir.name) / "etf-runtime"
        rc = main(
            [
                "etf-premium",
                "start",
                "--config",
                str(self.config),
                "--runtime-dir",
                str(runtime.absolute()),
                "--symbol",
                "159941",
                "--initial-position",
                "base",
            ]
        )
        self.assertEqual(rc, 0)
        args, env = run_pm2_mock.call_args.args
        self.assertEqual(args[0], "start")
        self.assertEqual(env["ETF_PREMIUM_SYMBOL"], "159941")
        self.assertEqual(env["ETF_PREMIUM_INTERVAL"], "60.0")
        self.assertEqual(
            env["ETF_PREMIUM_RUNTIME_DIR"],
            str(runtime.absolute()),
        )

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_main_builds_momentum_rotation_service(self, run_pm2_mock):
        runtime = Path(self.temp_dir.name) / "momentum-runtime"
        rc = main(
            [
                "momentum-rotation",
                "start",
                "--config",
                str(self.config),
                "--runtime-dir",
                str(runtime.absolute()),
                "--mode",
                "live-cn",
            ]
        )
        self.assertEqual(rc, 0)
        args, env = run_pm2_mock.call_args.args
        self.assertEqual(args[0], "start")
        self.assertEqual(env["MOMENTUM_ROTATION_MODE"], "live-cn")
        self.assertEqual(env["MOMENTUM_ROTATION_INTERVAL"], "60.0")
        self.assertEqual(
            env["MOMENTUM_ROTATION_RUNTIME_DIR"],
            str(runtime.absolute()),
        )

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_save_needs_no_config(self, run_pm2_mock):
        self.assertEqual(main(["save"]), 0)
        run_pm2_mock.assert_called_once_with(["save"])


if __name__ == "__main__":
    unittest.main()
