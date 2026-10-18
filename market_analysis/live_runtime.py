"""Shared standard-library runtime helpers for market-analysis live services."""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, TypeVar, cast


_Task = TypeVar("_Task")
_STOP_TASK = object()


class BackgroundWorker(Generic[_Task]):
    """Run bounded best-effort tasks on one daemon thread.

    ``close(timeout)`` returns whether every accepted task finished before the deadline.
    """

    def __init__(
        self,
        handler: Callable[[_Task], None],
        *,
        name: str,
        maxsize: int = 100,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._handler = handler
        self._on_error = on_error
        self._closed = False
        self._queue: queue.Queue[_Task | object] = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._thread.start()

    def submit(self, task: _Task) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            return False
        return True

    def pending(self) -> int:
        """Best-effort count of accepted tasks not yet fully handled."""
        unfinished = self._queue.qsize()
        if self._closed and unfinished:
            unfinished -= 1  # the stop marker itself is not a task
        return unfinished

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is _STOP_TASK:
                    return
                self._handler(cast(_Task, task))
            except Exception as exc:  # keep one failed task from stopping the worker
                if self._on_error is not None:
                    self._on_error(exc)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 30.0) -> bool:
        """Stop the worker and drain the queue; returns whether it drained in time."""
        self._closed = True
        try:
            self._queue.put_nowait(_STOP_TASK)
        except queue.Full:
            return False
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


@contextmanager
def runtime_file_lock(path: Path) -> Iterator[None]:
    """Hold one OS-released lock for the lifetime of a live instance."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError(
                f"已有 live 实例占用运行目录: {path.parent}"
            ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def graceful_stop_event() -> Iterator[threading.Event]:
    """Convert SIGINT/SIGTERM into a cooperative stop and restore handlers."""
    stopped = threading.Event()

    def stop_handler(_signum: int, _frame: Any) -> None:
        stopped.set()

    old_sigint = signal.signal(signal.SIGINT, stop_handler)
    old_sigterm = signal.signal(signal.SIGTERM, stop_handler)
    try:
        yield stopped
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


def close_futu_context(context: Any) -> None:
    """Close a Futu context without masking the active failure path."""
    if context is None:
        return
    try:
        context.close()
    except Exception as exc:  # noqa: BLE001 - SDKs expose varied errors
        print(f"警告: 关闭 Futu context 失败: {exc}", file=sys.stderr, flush=True)


@contextmanager
def managed_futu_context(factory: Callable[..., Any], *, host: str, port: int) -> Iterator[Any]:
    """Open one Futu context and close it on every exit path without masking errors."""
    context = factory(host=host, port=port)
    try:
        yield context
    finally:
        close_futu_context(context)


class EventNotifier:
    """Deliver trading signals, errors and optional lifecycle events off the Futu callback thread.

    Strategy-specific text comes from ``format_event(event) -> (subject, message)``; the same
    event within ``throttle_seconds`` is dropped. ``sheet_strategy`` enables the compact
    shared-sheet record from ``sheet_notifications``.
    """

    LIFECYCLE = frozenset({"READY", "STOPPED", "THRESHOLD_READY"})

    def __init__(
        self,
        engine: Any,
        format_event: Callable[[dict[str, Any]], tuple[str, str]],
        *,
        name: str,
        sheet_strategy: str | None = None,
        notify_lifecycle: bool = False,
        throttle_seconds: float = 300.0,
        link: str = "https://www.futunn.com/",
        maxsize: int = 100,
    ) -> None:
        self.engine = engine
        self.format_event = format_event
        self.sheet_strategy = sheet_strategy
        self.notify_lifecycle = notify_lifecycle
        self.throttle_seconds = throttle_seconds
        self.link = link
        self.last_enqueued: dict[str, float] = {}
        self.worker = BackgroundWorker[dict[str, Any]](
            self._deliver,
            name=name,
            maxsize=maxsize,
            on_error=lambda exc: print(f"通知处理失败: {exc}", file=sys.stderr, flush=True),
        )

    def wanted(self, event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind == "ERROR":
            return True
        if kind == "SIGNAL":
            return event.get("action") in {"BUY", "SELL"}
        return self.notify_lifecycle and kind in self.LIFECYCLE

    def notify(self, event: dict[str, Any]) -> None:
        if not self.wanted(event):
            return
        now = time.monotonic()
        key = "|".join(str(event.get(name, "")) for name in ("type", "action", "bar_key", "message"))
        if now - self.last_enqueued.get(key, -math.inf) < self.throttle_seconds:
            return
        self.last_enqueued[key] = now
        from sheet_notifications import queued_sheet_event

        if not self.worker.submit(queued_sheet_event(event)):
            print(f"通知队列已满，丢弃事件: {key}", file=sys.stderr, flush=True)

    def _deliver(self, event: dict[str, Any]) -> None:
        subject, message = self.format_event(event)
        senders = (
            self.engine.send_webhook,
            lambda text: self.engine.send_telegram_message(text, self.link),
            lambda text: self.engine.send_email(subject, text),
        )
        for send in senders:
            try:
                send(message)
            except Exception as exc:  # noqa: BLE001 - one channel must not block the others
                print(f"通知发送失败: {exc}", file=sys.stderr, flush=True)
        if self.sheet_strategy:
            from sheet_notifications import send_sheet_signal

            send_sheet_signal(self.engine, self.sheet_strategy, event)

    def close(self, timeout: float = 30.0) -> bool:
        """Flush queued notifications; return False when the timeout cut them short."""
        if not self.worker.close(timeout):
            print(
                f"通知未排空（剩余 {self.worker.pending()} 条），退出将丢弃未投递渠道",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True
