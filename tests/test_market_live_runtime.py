import importlib.util
import io
import json
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "market_analysis" / "live_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("market_live_runtime_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
live_runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live_runtime
SPEC.loader.exec_module(live_runtime)


class LiveRuntimeTest(unittest.TestCase):
    def test_background_worker_is_bounded_and_drains_before_stopping(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        processed = []

        def handle(value: int) -> None:
            started.set()
            release.wait(timeout=1.0)
            processed.append(value)
            if len(processed) == 2:
                finished.set()

        worker = live_runtime.BackgroundWorker[int](
            handle,
            name="test-background-worker",
            maxsize=1,
        )
        self.addCleanup(lambda: worker.close(timeout=1.0))
        self.addCleanup(release.set)
        self.assertTrue(worker.submit(1))
        self.assertTrue(started.wait(timeout=1.0))
        self.assertTrue(worker.submit(2))
        self.assertFalse(worker.submit(3))
        self.assertFalse(worker.close(timeout=0.0))

        release.set()
        self.assertTrue(finished.wait(timeout=1.0))
        self.assertTrue(worker.close(timeout=1.0))
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(processed, [1, 2])

    def test_background_worker_reports_error_and_continues(self) -> None:
        processed = []
        errors = []

        def handle(value: int) -> None:
            if value == 1:
                raise RuntimeError("failed task")
            processed.append(value)

        worker = live_runtime.BackgroundWorker[int](
            handle,
            name="test-background-worker-errors",
            on_error=errors.append,
        )
        self.assertTrue(worker.submit(1))
        self.assertTrue(worker.submit(2))
        self.assertTrue(worker.close(timeout=1.0))

        self.assertEqual(processed, [2])
        self.assertEqual([str(error) for error in errors], ["failed task"])

    def test_runtime_file_lock_blocks_competing_owner_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "live.lock"

            with live_runtime.runtime_file_lock(path):
                with self.assertRaisesRegex(RuntimeError, "已有 live 实例"):
                    with live_runtime.runtime_file_lock(path):
                        pass

            with live_runtime.runtime_file_lock(path):
                pass

    def test_write_json_atomic_preserves_old_document_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "nested" / "state.json"
            old = {"message": "旧状态", "value": 1}
            new = {"message": "完成", "value": 2}
            live_runtime.write_json_atomic(path, old)
            original = path.read_bytes()

            with patch.object(live_runtime.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    live_runtime.write_json_atomic(path, new)
            self.assertEqual(path.read_bytes(), original)

            live_runtime.write_json_atomic(path, new)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), new)
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_graceful_stop_event_sets_event_and_restores_handlers(self) -> None:
        old_handlers = {
            signal.SIGINT: object(),
            signal.SIGTERM: object(),
        }
        installed_handlers = dict(old_handlers)

        def fake_signal(signum: int, handler: object) -> object:
            previous = installed_handlers[signum]
            installed_handlers[signum] = handler
            return previous

        with patch.object(live_runtime.signal, "signal", side_effect=fake_signal):
            with live_runtime.graceful_stop_event() as stopped:
                self.assertFalse(stopped.is_set())
                installed_handlers[signal.SIGTERM](signal.SIGTERM, None)
                self.assertTrue(stopped.is_set())

        self.assertIs(installed_handlers[signal.SIGINT], old_handlers[signal.SIGINT])
        self.assertIs(installed_handlers[signal.SIGTERM], old_handlers[signal.SIGTERM])

    def test_close_futu_context_is_best_effort(self) -> None:
        class Context:
            def __init__(self, error: Exception | None = None) -> None:
                self.error = error
                self.closed = False

            def close(self) -> None:
                self.closed = True
                if self.error is not None:
                    raise self.error

        context = Context()
        live_runtime.close_futu_context(context)
        self.assertTrue(context.closed)

        stderr = io.StringIO()
        failing_context = Context(RuntimeError("close failed"))
        with redirect_stderr(stderr):
            live_runtime.close_futu_context(failing_context)
        self.assertTrue(failing_context.closed)
        self.assertIn("关闭 Futu context 失败: close failed", stderr.getvalue())

        live_runtime.close_futu_context(None)

    def test_managed_futu_context_closes_on_error(self) -> None:
        closed = []

        class Context:
            def __init__(self, host: str, port: int) -> None:
                self.args = (host, port)

            def close(self) -> None:
                closed.append(self.args)

        with self.assertRaises(RuntimeError):
            with live_runtime.managed_futu_context(Context, host="h", port=1):
                raise RuntimeError("boom")
        self.assertEqual(closed, [("h", 1)])

    def test_event_notifier_filters_throttles_and_records_sheet(self) -> None:
        class Engine:
            def __init__(self) -> None:
                self.webhooks: list[str] = []
                self.emails: list[tuple[str, str]] = []
                self.sheets: list[str] = []

            def send_webhook(self, message: str) -> None:
                self.webhooks.append(message)

            def send_telegram_message(self, message: str, link: str) -> None:
                raise RuntimeError("telegram down")

            def send_email(self, subject: str, message: str) -> None:
                self.emails.append((subject, message))

            def send_google_sheet_message(self, message: str, sheet_date: object) -> None:
                self.sheets.append(message)

            def send_feishu_sheet_message(self, message: str, sheet_date: object) -> None:
                pass

        sys.path.insert(0, str(MODULE_PATH.parent))
        engine = Engine()
        notifier = live_runtime.EventNotifier(
            engine, lambda e: (f"M3 {e.get('action', e['type'])}", f"text {e.get('bar_key', '')}"),
            name="test-notifier", sheet_strategy="m3",
        )
        base = {"symbol": "SH.000902", "strategy": "m3", "tag": "M3"}
        buy = {**base, "type": "SIGNAL", "action": "BUY", "bar_key": "2026-05-06 10:30:00",
               "position_text": "0→1", "reason": "动量启动（M）",
               "strength": {"ranking": [{"symbol": "SH.510500"}]}}
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            notifier.notify({**base, "type": "SIGNAL", "action": "NONE", "bar_key": "x"})
            notifier.notify(buy)
            notifier.notify(buy)                                  # throttled
            notifier.notify({**base, "type": "READY"})           # lifecycle is opt-in
            notifier.notify({**base, "type": "ERROR", "message": "feed"})
            notifier.close()
        self.assertEqual(len(engine.webhooks), 2)
        self.assertEqual(engine.emails[0][0], "M3 BUY")
        self.assertIn("telegram down", stderr.getvalue())
        self.assertEqual(len(engine.sheets), 1)
        self.assertIn("SH.000902", engine.sheets[0])

if __name__ == "__main__":
    unittest.main()
