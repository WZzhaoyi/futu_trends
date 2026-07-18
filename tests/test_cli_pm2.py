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
        self.assertEqual(
            build_pm2_args("start", spec),
            ["start", str(spec.ecosystem_path), "--only", spec.instance_name, "--update-env"],
        )
        self.assertEqual(build_pm2_args("status", spec), ["describe", spec.instance_name])

    def test_missing_config_and_invalid_port_fail(self):
        with self.assertRaises(PM2ConfigError):
            build_service_spec("order-engine", str(self.config) + ".missing")
        with self.assertRaises(PM2ConfigError):
            build_service_spec("signal-api", str(self.config), port=65536)

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
        self.assertEqual(args[0], "restart")
        self.assertEqual(env["SIGNAL_API_PORT"], "9001")

    @patch("cli.pm2_service.run_pm2", return_value=0)
    def test_save_needs_no_config(self, run_pm2_mock):
        self.assertEqual(main(["save"]), 0)
        run_pm2_mock.assert_called_once_with(["save"])


if __name__ == "__main__":
    unittest.main()
