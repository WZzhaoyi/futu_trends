"""Shared standard-library runtime helpers for market-analysis live services."""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, TypeVar, cast


_Task = TypeVar("_Task")
_STOP_TASK = object()


class BackgroundWorker(Generic[_Task]):
    """Run bounded best-effort tasks on one daemon thread."""

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
        self._queue: queue.Queue[_Task | object] = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._thread.start()

    def submit(self, task: _Task) -> bool:
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            return False
        return True

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

    def close(self, timeout: float = 10.0) -> bool:
        try:
            self._queue.put_nowait(_STOP_TASK)
        except queue.Full:
            return False
        self._thread.join(timeout=timeout)
        return True


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
