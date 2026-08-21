import sys
import types
import unittest
from unittest import mock


def _install_import_stubs():
    requests_html = types.ModuleType("requests_html")
    requests_html.HTMLSession = object
    sys.modules.setdefault("requests_html", requests_html)

    google = types.ModuleType("google")
    google_oauth2 = types.ModuleType("google.oauth2")
    google_service_account = types.ModuleType("google.oauth2.service_account")
    google_service_account.Credentials = object
    google_oauth2.service_account = google_service_account
    google.oauth2 = google_oauth2
    sys.modules.setdefault("google", google)
    sys.modules.setdefault("google.oauth2", google_oauth2)
    sys.modules.setdefault("google.oauth2.service_account", google_service_account)

    googleapiclient = types.ModuleType("googleapiclient")
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")
    googleapiclient_discovery.build = lambda *args, **kwargs: None
    googleapiclient.discovery = googleapiclient_discovery
    sys.modules.setdefault("googleapiclient", googleapiclient)
    sys.modules.setdefault("googleapiclient.discovery", googleapiclient_discovery)

    httplib2 = types.ModuleType("httplib2")
    httplib2.Http = object
    httplib2.ProxyInfo = object
    httplib2.socks = types.SimpleNamespace(PROXY_TYPE_HTTP=0)
    sys.modules.setdefault("httplib2", httplib2)

    google_auth_httplib2 = types.ModuleType("google_auth_httplib2")
    google_auth_httplib2.AuthorizedHttp = object
    sys.modules.setdefault("google_auth_httplib2", google_auth_httplib2)

    futu_group = types.ModuleType("futu_group")
    futu_group.sync_futu_group = lambda *args, **kwargs: None
    sys.modules.setdefault("futu_group", futu_group)


_install_import_stubs()

import notification_engine.engine as engine_module
from notification_engine.engine import NotificationEngine, _feishu_cell_value_to_text


class FeishuCellValueToTextTest(unittest.TestCase):
    def test_empty_response_is_empty_text(self):
        self.assertEqual(_feishu_cell_value_to_text([]), "")
        self.assertEqual(_feishu_cell_value_to_text([[]]), "")

    def test_none_cell_is_empty_text(self):
        self.assertEqual(_feishu_cell_value_to_text([[None]]), "")

    def test_non_empty_cell_is_text(self):
        self.assertEqual(_feishu_cell_value_to_text([["existing"]]), "existing")
        self.assertEqual(_feishu_cell_value_to_text([[123]]), "123")


class NotificationTimeoutTest(unittest.TestCase):
    def test_email_uses_network_timeout(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.mail_port = 465
        engine.mail_host = "smtp.example.com"
        engine.sender = "sender@example.com"
        engine.mail_pass = "secret"
        engine.receivers = ["receiver@example.com"]

        with mock.patch.object(engine_module.smtplib, "SMTP_SSL") as smtp_ssl:
            engine.send_email("subject", "message")

        smtp_ssl.assert_called_once_with(
            "smtp.example.com",
            465,
            timeout=engine_module.NOTIFICATION_NETWORK_TIMEOUT,
        )

    def test_email_timeout_is_best_effort(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.mail_port = 465
        engine.mail_host = "smtp.example.com"
        engine.sender = "sender@example.com"
        engine.mail_pass = "secret"
        engine.receivers = ["receiver@example.com"]

        with mock.patch.object(
            engine_module.smtplib,
            "SMTP_SSL",
            side_effect=TimeoutError,
        ), self.assertLogs(engine_module.logger, level="ERROR") as logs:
            engine.send_email("subject", "message")

        self.assertIn("Failed to connect", logs.output[0])

    def test_telegram_uses_network_timeout(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.TELEGRAM_BOT_TOKEN = "token"
        engine.TELEGRAM_CHAT_ID = "chat"
        engine.PROXIES = {}
        engine.SESSION = mock.Mock()

        engine.send_telegram_message("message", "https://example.com")

        self.assertEqual(
            engine.SESSION.post.call_args.kwargs["timeout"],
            engine_module.NOTIFICATION_NETWORK_TIMEOUT,
        )

    def test_telegram_photo_requests_use_network_timeout(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.TELEGRAM_BOT_TOKEN = "token"
        engine.TELEGRAM_CHAT_ID = "chat"
        engine.PROXIES = {}
        engine.SESSION = mock.Mock()
        engine.SESSION.post.return_value.status_code = 200
        engine.plog = mock.Mock()

        engine.send_telegram_photo("https://example.com/image.jpg")
        self.assertEqual(
            engine.SESSION.post.call_args.kwargs["timeout"],
            engine_module.NOTIFICATION_NETWORK_TIMEOUT,
        )

        engine.send_telegram_photos(["https://example.com/image.jpg"])
        self.assertEqual(
            engine.SESSION.post.call_args.kwargs["timeout"],
            engine_module.NOTIFICATION_NETWORK_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
