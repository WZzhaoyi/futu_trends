import configparser
import io
import unittest
from unittest import mock
from contextlib import redirect_stdout

import notification_engine.engine as engine_module
import notification_engine.webhook as webhook_module
from notification_engine.engine import NotificationEngine, _feishu_cell_value_to_text
from notification_engine.webhook import WebhookNotifier


class FeishuCellValueToTextTest(unittest.TestCase):
    def test_cell_values_become_text(self):
        for values, expected in (([], ""), ([[]], ""), ([[None]], ""), ([["existing"]], "existing"), ([[123]], "123")):
            with self.subTest(values=values):
                self.assertEqual(_feishu_cell_value_to_text(values), expected)


class NotificationTimeoutTest(unittest.TestCase):
    @staticmethod
    def _email_engine(receivers=None):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.mail_port = 465
        engine.mail_host = "smtp.example.com"
        engine.sender = "sender@example.com"
        engine.mail_pass = "secret"
        engine.receivers = receivers or ["receiver@example.com"]
        return engine

    @staticmethod
    def _telegram_engine(*, status=200, payload=None):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.TELEGRAM_BOT_TOKEN = "secret-token"
        engine.TELEGRAM_CHAT_ID = "secret-chat"
        engine.PROXIES = {}
        engine.SESSION = mock.Mock()
        engine.SESSION.post.return_value.status_code = status
        engine.SESSION.post.return_value.json.return_value = payload or {
            "ok": True,
            "result": {"message_id": 123},
        }
        return engine

    def test_email_uses_network_timeout(self):
        engine = self._email_engine()

        with mock.patch.object(
            engine_module.smtplib,
            "SMTP_SSL",
        ) as smtp_ssl, mock.patch("builtins.print"):
            result = engine.send_email("subject", "message")

        self.assertTrue(result)
        smtp_ssl.assert_called_once_with(
            "smtp.example.com",
            465,
            timeout=engine_module.NOTIFICATION_NETWORK_TIMEOUT,
        )

    def test_email_timeout_is_best_effort(self):
        engine = self._email_engine()

        with mock.patch.object(
            engine_module.smtplib,
            "SMTP_SSL",
            side_effect=TimeoutError,
        ), self.assertLogs("notification_engine.delivery_log", level="ERROR") as logs:
            result = engine.send_email("subject", "message")

        self.assertFalse(result)
        self.assertIn("exception=TimeoutError", logs.output[0])

    def test_email_reports_partial_recipient_rejection(self):
        engine = self._email_engine(
            ["ok@example.com", "rejected@example.com"]
        )
        smtp = mock.Mock()
        smtp.sendmail.return_value = {"rejected@example.com": (550, "rejected")}

        with mock.patch.object(
            engine_module.smtplib,
            "SMTP_SSL",
            return_value=smtp,
        ), self.assertLogs("notification_engine.delivery_log", level="ERROR") as logs:
            result = engine.send_email("subject", "message")

        self.assertFalse(result)
        self.assertIn("recipient_count=2 refused_count=1", logs.output[0])
        self.assertNotIn("rejected@example.com", logs.output[0])

    def test_telegram_delivery_uses_correct_transport_and_reports_result(self):
        cases = (
            ("send_telegram_message", "sendMessage", "json", "text", "hello", 123),
            ("send_telegram_photo", "sendPhoto", "data", "photo", "https://example.com/image.jpg", 456),
        )
        for method, operation, encoding, field, value, message_id in cases:
            with self.subTest(operation=operation):
                engine = self._telegram_engine(payload={"ok": True, "result": {"message_id": message_id}})
                output = io.StringIO()
                with redirect_stdout(output):
                    getattr(engine, method)(value)
                engine.SESSION.post.assert_called_once()
                request = engine.SESSION.post.call_args
                self.assertTrue(request.args[0].endswith("/" + operation))
                self.assertGreater(request.kwargs["timeout"], 0)
                self.assertEqual(request.kwargs[encoding]["chat_id"], "secret-chat")
                self.assertEqual(request.kwargs[encoding][field], value)
                for detail in ("channel=telegram", "operation=" + operation, "ok=true", f"message_id={message_id}"):
                    self.assertIn(detail, output.getvalue())

    def test_telegram_logs_rejected_http_response_without_credentials(self):
        engine = self._telegram_engine(
            status=400,
            payload={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
        )

        with self.assertLogs("notification_engine.delivery_log", level="ERROR") as logs:
            engine.send_telegram_message("message", "https://example.com")

        self.assertIn(
            "Notification: channel=telegram operation=sendMessage ok=false "
            "status=400 error_code=400 error=Bad Request: chat not found",
            logs.output[0],
        )
        self.assertNotIn("secret-token", logs.output[0])
        self.assertNotIn("secret-chat", logs.output[0])

    def test_telegram_media_group_falls_back_when_api_reports_failure(self):
        engine = self._telegram_engine(
            payload={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request",
            }
        )
        engine.send_telegram_photo = mock.Mock()
        pic_urls = [
            "https://example.com/one.jpg",
            "https://example.com/two.jpg",
        ]

        with self.assertLogs("notification_engine.delivery_log", level="ERROR"):
            engine.send_telegram_photos(pic_urls)

        self.assertEqual(
            engine.send_telegram_photo.call_args_list,
            [mock.call(pic_urls[0]), mock.call(pic_urls[1])],
        )

    def test_futu_group_propagates_sync_failure(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.futu_keyword = ["signal"]
        engine.host = "127.0.0.1"
        engine.port = 11111

        with mock.patch.object(
            engine_module,
            "sync_futu_group",
            return_value=False,
        ), self.assertLogs("notification_engine.delivery_log", level="ERROR") as logs:
            result = engine.send_futu_message(
                ["US.TEST"],
                ["signal"],
                [2.0],
                [1.0],
            )

        self.assertFalse(result)
        self.assertIn(
            "channel=futu_group operation=sync ok=false",
            logs.output[0],
        )

    def test_feishu_does_not_retry_business_errors(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.PROXIES = {}
        engine.SESSION = mock.Mock()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"code": 999, "msg": "rejected"}
        engine.SESSION.request.return_value = response

        with self.assertRaises(RuntimeError), mock.patch.object(
            engine_module.time,
            "sleep",
        ) as sleep:
            engine._safe_feishu_request("GET", "https://example.com")

        engine.SESSION.request.assert_called_once()
        sleep.assert_not_called()

    def test_feishu_retries_transient_http_status(self):
        engine = NotificationEngine.__new__(NotificationEngine)
        engine.PROXIES = {}
        engine.SESSION = mock.Mock()
        unavailable = mock.Mock(status_code=503)
        success = mock.Mock(status_code=200)
        success.json.return_value = {"code": 0, "data": {}}
        engine.SESSION.request.side_effect = [unavailable, success]

        with mock.patch.object(engine_module.time, "sleep") as sleep:
            result = engine._safe_feishu_request("GET", "https://example.com")

        self.assertEqual(result, {"code": 0, "data": {}})
        self.assertEqual(engine.SESSION.request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_google_request_uses_client_retry_policy(self):
        request = mock.Mock()
        execute_func = mock.Mock(return_value=request)
        request.execute.return_value = {"updatedCells": 1}

        result = NotificationEngine._execute_google_request(
            execute_func,
            spreadsheetId="sheet",
        )

        self.assertEqual(result, {"updatedCells": 1})
        request.execute.assert_called_once_with(num_retries=2)


class WebhookNotifierTest(unittest.TestCase):
    @staticmethod
    def _notifier() -> WebhookNotifier:
        config = configparser.ConfigParser()
        config["CONFIG"] = {
            "WEBHOOK_URL": "https://secret.example/hooks?token=secret",
        }
        return WebhookNotifier(config)

    def test_success_returns_and_reports_delivery_id(self):
        response = mock.Mock(
            status_code=200,
            ok=True,
            text='{"ok": true}',
        )
        response.json.return_value = {"ok": True, "runId": "run-123"}

        with mock.patch.object(
            webhook_module.requests,
            "post",
            return_value=response,
        ), redirect_stdout(io.StringIO()) as output:
            result = self._notifier().send("message")

        self.assertTrue(result.ok)
        self.assertEqual(result.run_id, "run-123")
        for detail in ("channel=webhook", "operation=send", "ok=true", "run_id=run-123"):
            self.assertIn(detail, output.getvalue())

    def test_failure_log_omits_url_and_raw_response(self):
        response = mock.Mock(
            status_code=502,
            ok=False,
            text='{"ok": false}',
        )
        response.json.return_value = {
            "ok": False,
            "error": "upstream rejected",
            "credential": "response-secret",
        }

        with mock.patch.object(
            webhook_module.requests,
            "post",
            return_value=response,
        ), self.assertLogs("notification_engine.delivery_log", level="ERROR") as logs:
            result = self._notifier().send("message")

        self.assertFalse(result.ok)
        self.assertIn("status=502 error=upstream rejected", logs.output[0])
        self.assertNotIn("secret.example", logs.output[0])
        self.assertNotIn("response-secret", logs.output[0])


if __name__ == "__main__":
    unittest.main()
