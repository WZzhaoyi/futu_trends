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
        self.assertTrue(worker.submit(1))
        self.assertTrue(started.wait(timeout=1.0))
        self.assertTrue(worker.submit(2))
        self.assertFalse(worker.submit(3))
        self.assertFalse(worker.close(timeout=0.0))

        release.set()
        self.assertTrue(finished.wait(timeout=1.0))
        self.assertTrue(worker.close(timeout=1.0))
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

    def test_write_json_atomic_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "nested" / "state.json"
            live_runtime.write_json_atomic(path, {"message": "完成", "value": 1})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"message": "完成", "value": 1},
            )
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


if __name__ == "__main__":
    unittest.main()
