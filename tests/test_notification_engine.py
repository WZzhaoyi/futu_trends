import configparser
import io
import json
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
        config["CONFIG"] = {"WEBHOOK_TARGETS": "hook"}
        config["WEBHOOK.hook"] = {
            "url": "https://secret.example/hooks?token=secret",
            "signing": "none",
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


class WebhookTargetsTest(unittest.TestCase):
    """多目标投递、HMAC-SHA256 V2 签名与目标级配置。"""

    HERMES = "https://hermes.example/api/webhook"
    OPENCLAW = "https://hook.example/hooks/agent"

    @staticmethod
    def _config() -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.read_string(
            """
            [CONFIG]
            WEBHOOK_TARGETS = hermes, openclaw

            [WEBHOOK.hermes]
            url = https://hermes.example/api/webhook
            signing = hmac-sha256-v2
            secret = s3cr3t
            event_id_field = event_id
            timeout_seconds = 8

            [WEBHOOK.hermes.headers]
            X-Tenant = team-quant

            [WEBHOOK.hermes.payload]
            channel    = qqbot
            event_type = signal_hook

            [WEBHOOK.openclaw]
            url = https://hook.example/hooks/agent
            signing = none

            [WEBHOOK.openclaw.headers]
            x-openclaw-token = hook-token

            [WEBHOOK.openclaw.payload]
            to = c2c:someone
            """
        )
        return config

    @staticmethod
    def _response(*, status=200, payload=None):
        response = mock.Mock(status_code=status, ok=status < 400)
        response.json.return_value = payload if payload is not None else {}
        response.text = json.dumps(payload) if payload is not None else ""
        return response

    @staticmethod
    def _single_target(**options) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config["CONFIG"] = {"WEBHOOK_TARGETS": "hermes"}
        config["WEBHOOK.hermes"] = {
            "url": WebhookTargetsTest.HERMES,
            "signing": "hmac-sha256-v2",
            "secret": "s3cr3t",
            **options,
        }
        return config

    def test_hmac_v2_signs_the_exact_body_bytes(self):
        notifier = WebhookNotifier(self._single_target(event_id_field="event_id"))
        response = self._response(payload={"ok": True, "runId": "run-1"})

        with mock.patch.object(
            webhook_module.requests, "post", return_value=response
        ) as post, mock.patch.object(
            webhook_module.time, "time", return_value=1790483599
        ), redirect_stdout(io.StringIO()) as log_output:
            result = notifier.send("策略信号：顶背离")

        self.assertTrue(result.ok)
        headers = post.call_args.kwargs["headers"]
        body = post.call_args.kwargs["data"]
        self.assertNotIn("json", post.call_args.kwargs)
        self.assertEqual(headers["X-Webhook-Timestamp"], "1790483599")
        self.assertEqual(
            headers["X-Webhook-Signature-V2"],
            webhook_module.sign_hmac_sha256_v2("s3cr3t", "1790483599", body),
        )
        # X-Request-ID = event_id，与 body 里的 event_id 同值
        event_id = headers["X-Request-ID"]
        self.assertEqual(result.request_id, event_id)
        self.assertEqual(json.loads(body.decode("utf-8"))["event_id"], event_id)
        # 中文按原始 UTF-8 发送，签名对象就是实发字节
        self.assertIn("策略信号：顶背离".encode("utf-8"), body)
        self.assertEqual(json.loads(body.decode("utf-8"))["event_id"], event_id)
        # 日志行带上 request_id，便于拿接收端的 delivery id 反查
        self.assertIn(f"request_id={headers['X-Request-ID']}", log_output.getvalue())

    def test_event_id_is_deterministic_for_the_same_content(self):
        sent = []

        def fake_post(url, **kwargs):
            sent.append(kwargs["headers"]["X-Request-ID"])
            return self._response(payload={"ok": True})

        with mock.patch.object(
            webhook_module.requests, "post", side_effect=fake_post
        ), redirect_stdout(io.StringIO()):
            notifier = WebhookNotifier(self._single_target())
            notifier.send("同一条信号")
            notifier.send("同一条信号")
            notifier.send("另一条信号")

        # 同日同内容重复投递得到同一个 event_id（接收端可去重），不同内容则不同
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(len(sent[0]), 32)
        self.assertNotEqual(sent[0], sent[2])

    def test_event_id_does_not_depend_on_the_date(self):
        sent = []

        def fake_post(url, **kwargs):
            sent.append(kwargs["headers"]["X-Request-ID"])
            return self._response(payload={"ok": True})

        with mock.patch.object(
            webhook_module.requests, "post", side_effect=fake_post
        ), mock.patch.object(
            webhook_module.time, "time", side_effect=[1790517224.0, 1790780000.0]
        ), redirect_stdout(io.StringIO()):
            notifier = WebhookNotifier(self._single_target())
            notifier.send("同一条信号")
            notifier.send("同一条信号")

        # 相隔数天、内容相同 → 仍是同一个 event_id
        self.assertEqual(sent[0], sent[1])

    def test_event_id_follows_event_type(self):
        sent = []

        def fake_post(url, **kwargs):
            sent.append(kwargs["headers"]["X-Request-ID"])
            return self._response(payload={"ok": True})

        notifier = WebhookNotifier(self._single_target(payload_json='{"event_type": "signal_cn"}'))
        other = WebhookNotifier(self._single_target(payload_json='{"event_type": "signal_us"}'))
        with mock.patch.object(
            webhook_module.requests, "post", side_effect=fake_post
        ), redirect_stdout(io.StringIO()):
            notifier.send("同一条信号")
            other.send("同一条信号")

        self.assertNotEqual(sent[0], sent[1])

    def test_targets_deliver_independently(self):
        notifier = WebhookNotifier(self._config())
        responses = {
            self.HERMES: self._response(payload={"status": "accepted"}),
            self.OPENCLAW: self._response(status=502, payload={"error": "upstream rejected"}),
        }

        with mock.patch.object(
            webhook_module.requests, "post",
            side_effect=lambda url, **kwargs: responses[url],
        ) as post, self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs, redirect_stdout(io.StringIO()):
            result = notifier.send("信号")

        self.assertEqual(post.call_count, 2)
        self.assertFalse(result.ok)
        self.assertTrue(result.targets["hermes"].ok)
        # event_type 参与 event_id 计算：带 event_type 的目标与不带的算出的 id 不同
        self.assertEqual(len(result.targets["hermes"].request_id), 32)
        self.assertNotEqual(
            result.targets["hermes"].request_id, result.targets["openclaw"].request_id
        )
        self.assertEqual(result.targets["openclaw"].error, "upstream rejected")
        self.assertEqual(result.error, "upstream rejected")

        calls = {call.args[0]: call.kwargs for call in post.call_args_list}
        self.assertEqual(calls[self.HERMES]["headers"]["x-tenant"], "team-quant")
        self.assertEqual(calls[self.HERMES]["timeout"], 8.0)
        self.assertEqual(calls[self.OPENCLAW]["timeout"], 30.0)
        self.assertEqual(
            json.loads(calls[self.HERMES]["data"].decode("utf-8"))["channel"], "qqbot"
        )
        self.assertEqual(
            json.loads(calls[self.OPENCLAW]["data"].decode("utf-8"))["to"], "c2c:someone"
        )
        # 失败日志只带目标名与状态，不带地址或密钥
        self.assertIn("target=openclaw", logs.output[0])
        self.assertNotIn("s3cr3t", logs.output[0])
        self.assertNotIn("hook.example", logs.output[0])

    def test_missing_signing_is_rejected_instead_of_sent_unsigned(self):
        config = self._single_target(signing="")

        with mock.patch.object(
            webhook_module.requests, "post"
        ) as post, self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs:
            notifier = WebhookNotifier(config)
            result = notifier.send("信号")

        self.assertEqual(notifier.target_names, [])
        self.assertFalse(result.ok)
        post.assert_not_called()
        self.assertIn("missing signing", logs.output[0])

    def test_unsupported_signing_is_rejected_instead_of_sent_unsigned(self):
        config = self._single_target(signing="hmac-sha256", secret="s3cr3t")

        with mock.patch.object(
            webhook_module.requests, "post"
        ) as post, self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs:
            notifier = WebhookNotifier(config)
            result = notifier.send("信号")

        self.assertEqual(notifier.target_names, [])
        self.assertFalse(result.ok)
        post.assert_not_called()
        self.assertIn("unsupported signing", logs.output[0])

    def test_signed_target_without_secret_is_not_delivered(self):
        config = self._single_target(secret="")

        with mock.patch.object(
            webhook_module.requests, "post"
        ) as post, self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs:
            notifier = WebhookNotifier(config)
            result = notifier.send("信号")

        self.assertEqual(notifier.target_names, [])
        self.assertFalse(result.ok)
        post.assert_not_called()
        self.assertIn("missing secret", logs.output[0])

    def test_payload_json_keeps_exact_case_and_type(self):
        config = self._single_target(
            payload_json=json.dumps(
                {"wakeMode": "now", "deliver": True, "timeoutSeconds": 30}
            )
        )
        config["WEBHOOK.hermes.payload"] = {"channel": "qqbot", "wakemode": "later"}
        response = self._response(payload={"status": "accepted"})

        with mock.patch.object(
            webhook_module.requests, "post", return_value=response
        ) as post, redirect_stdout(io.StringIO()):
            result = WebhookNotifier(config).send("信号")

        self.assertTrue(result.ok)
        payload = json.loads(post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["wakeMode"], "now")
        self.assertIs(payload["deliver"], True)
        self.assertEqual(payload["timeoutSeconds"], 30)
        self.assertEqual(payload["channel"], "qqbot")
        self.assertNotIn("wakemode", payload)
        self.assertEqual(payload["message"], "信号")

    def test_transport_error_is_reported_per_target(self):
        def fake_post(url, **kwargs):
            if url == self.HERMES:
                return self._response(payload={"ok": True})
            raise webhook_module.requests.ConnectionError("connection refused")

        with mock.patch.object(
            webhook_module.requests, "post", side_effect=fake_post
        ), self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs, redirect_stdout(io.StringIO()):
            result = WebhookNotifier(self._config()).send("信号")

        self.assertFalse(result.ok)
        self.assertTrue(result.targets["hermes"].ok)
        self.assertEqual(result.targets["openclaw"].error, "ConnectionError")
        self.assertIn("target=openclaw", logs.output[0])
        self.assertIn("exception=ConnectionError", logs.output[0])
        self.assertNotIn("hook.example", logs.output[0])
        self.assertNotIn("connection refused", logs.output[0])

    def test_missing_webhook_targets_is_reported(self):
        config = configparser.ConfigParser()
        config["CONFIG"] = {"PROXY": ""}

        with self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs:
            notifier = WebhookNotifier(config)
            result = notifier.send("信号")

        self.assertEqual(notifier.target_names, [])
        self.assertFalse(result.ok)
        self.assertIn("missing WEBHOOK_TARGETS", logs.output[0])

    def test_missing_url_is_rejected(self):
        config = self._single_target(url="")

        with mock.patch.object(
            webhook_module.requests, "post"
        ) as post, self.assertLogs(
            "notification_engine.delivery_log", level="ERROR"
        ) as logs:
            notifier = WebhookNotifier(config)
            result = notifier.send("信号")

        self.assertEqual(notifier.target_names, [])
        self.assertFalse(result.ok)
        post.assert_not_called()
        self.assertIn("missing url", logs.output[0])

    def test_run_id_is_reported_from_response_json(self):
        response = self._response(payload={"ok": True, "runId": "run-1"})

        with mock.patch.object(
            webhook_module.requests, "post", return_value=response
        ), redirect_stdout(io.StringIO()):
            result = WebhookNotifier(self._single_target()).send("信号")

        self.assertTrue(result.ok)
        self.assertEqual(result.run_id, "run-1")


if __name__ == "__main__":
    unittest.main()
