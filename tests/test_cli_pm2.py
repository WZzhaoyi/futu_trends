import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.pm2_service import (
    PM2ConfigError,
    build_service_spec,
    main,
    resolve_pm2_invocation,
)


class PM2ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = Path(self.temp_dir.name) / "signal api.ini"
        self.config.write_text("[CONFIG]\n", encoding="utf-8")

    def ecosystem_apps(self, path, environment):
        node = shutil.which("node")
        self.assertIsNotNone(node, "PM2 ecosystem 契约测试需要 Node.js")
        result = subprocess.run(
            [node, "-e", "console.log(JSON.stringify(require(process.argv[1]).apps))", str(path)],
            env={"PATH": os.environ.get("PATH", ""), "TZ": "Asia/Shanghai", **environment},
            capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(result.stdout)

    @patch("cli.pm2_service.run_pm2", return_value=7)
    def test_cli_settings_reach_real_ecosystems(self, run_pm2):
        runtime = str(Path(self.temp_dir.name) / "runtime with spaces")
        strategy = Path(self.temp_dir.name) / "strategy.json"
        strategy.write_text("{}\n", encoding="utf-8")
        cases = (
            ("order-engine", "start", "order_engine/__main__.py", {}),
            ("signal-api", "restart", "gui/backend/api.py", {"--port": "18001"}),
            ("csi-flow", "start", "market_analysis/csi_flow_timing.py", {
                "--runtime-dir": runtime, "--symbol": "SH.000300",
                "--initial-position": "long", "--entry-date": "2026-09-01",
                "--notification-mode": "position-independent",
                "--window-months": "10", "--t1-sell-mode": "ignore-same-day",
            }),
            ("etf-premium", "start", "market_analysis/etf_premium_rate.py", {
                "--runtime-dir": runtime, "--symbol": "510300",
                "--initial-position": "low", "--strategy-file": str(strategy),
                "--cache-dir": str(Path(self.temp_dir.name) / "cache"),
                "--interval": "17.0", "--nav-refresh": "123.0",
                "--max-nav-age": "7", "--max-quote-age": "89.0", "--max-errors": "3",
            }),
            ("momentum-rotation", "restart", "market_analysis/momentum_rotation_strategy.py", {
                "--runtime-dir": runtime,
            }),
        )
        records = []
        for service, action, script, options in cases:
            with self.subTest(service=service):
                supplied = {"--config": str(self.config), **options}
                argv = [service, action] + [part for pair in supplied.items() for part in pair]
                run_pm2.reset_mock()
                self.assertEqual(main(argv), 7)
                run_pm2.assert_called_once()
                command, environment = run_pm2.call_args.args
                self.assertEqual(command[0], "start")
                self.assertIn("--update-env", command)
                apps = self.ecosystem_apps(command[1], environment)
                selected = command[command.index("--only") + 1].split(",")
                self.assertCountEqual(selected, [app["name"] for app in apps])
                self.assertEqual(len(apps), 2 if service == "momentum-rotation" else 1)
                markets = []
                for app in apps:
                    self.assertEqual(Path(app["script"]), Path(__file__).resolve().parents[1] / script)
                    args = app["args"]
                    if service in {"csi-flow", "etf-premium", "momentum-rotation"}:
                        self.assertEqual(args[0], "live")
                        args = args[1:]
                    delivered = dict(zip(args[::2], args[1::2]))
                    for flag, value in supplied.items():
                        self.assertEqual(delivered.get(flag), value, flag)
                    markets.append(delivered.get("--markets"))
                if service == "momentum-rotation":
                    self.assertCountEqual(markets, ["CN", "US"])
                records.append({"service": service, "command": command, "apps": apps})
        if output := os.environ.get("FUTU_TEST_ARTIFACT_DIR"):
            path = Path(output) / "pm2-ecosystems.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    def test_identity_does_not_dereference_config_symlink(self):
        link = Path(self.temp_dir.name) / "linked.ini"
        try:
            link.symlink_to(self.config)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        spec = build_service_spec("order-engine", str(link))
        target = build_service_spec("order-engine", str(self.config))
        app, = self.ecosystem_apps(spec.ecosystem_path, spec.environment)
        self.assertEqual(spec.config_path, link.absolute())
        self.assertNotEqual(spec.instance_name, target.instance_name)
        self.assertEqual(app["name"], spec.instance_name)
        self.assertEqual(app["args"], ["--config", str(link)])

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_status_targets_configured_instance(self, run_pm2):
        spec = build_service_spec("signal-api", str(self.config), port=18001)
        self.assertEqual(main([
            "signal-api", "status", "--config", str(self.config), "--port", "18001",
        ]), 0)
        self.assertEqual(run_pm2.call_args.args[0], ["describe", spec.instance_name])

    def test_invalid_settings_rejected(self):
        runtime = str(Path(self.temp_dir.name) / "runtime")
        cases = (
            ("order-engine", str(self.config) + ".missing", {}, "配置文件不存在"),
            ("signal-api", str(self.config), {"port": 65536}, "端口超出范围"),
            ("csi-flow", str(self.config), {"runtime_dir": "relative/path"}, "绝对路径"),
            ("csi-flow", str(self.config), {
                "runtime_dir": runtime, "initial_position": "long",
            }, "entry-date"),
            ("etf-premium", str(self.config), {
                "runtime_dir": "relative/path", "symbol": "159941", "initial_position": "base",
            }, "绝对路径"),
            ("momentum-rotation", str(self.config), {
                "runtime_dir": runtime, "live_mode": "custom",
            }, "mode 只能是 live"),
            ("etf-premium", str(self.config), {
                "runtime_dir": runtime, "symbol": "SZ.159941", "initial_position": "base",
            }, "6位基金代码"),
        )
        for service, config, options, reason in cases:
            with self.subTest(service=service, options=options):
                with self.assertRaisesRegex(PM2ConfigError, reason):
                    build_service_spec(service, config, **options)

    def test_unix_pm2_override(self):
        invocation = resolve_pm2_invocation(
            ["status"], environ={"PM2_BIN": "/custom/pm2"}, platform="darwin",
        )
        self.assertEqual(invocation, ["/custom/pm2", "status"])

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_save_needs_no_config(self, run_pm2_mock):
        self.assertEqual(main(["save"]), 0)
        run_pm2_mock.assert_called_once_with(["save"])


if __name__ == "__main__":
    unittest.main()
