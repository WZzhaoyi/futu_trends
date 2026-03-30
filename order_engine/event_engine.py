"""
轻量事件引擎，替代 vnpy 的 EventEngine。
线程安全的发布/订阅系统。
"""
import threading
from collections import defaultdict
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Callable

# 事件类型常量
EVENT_TICK = "eTick."
EVENT_LOG = "eLog"
EVENT_ORDER = "eOrder."
EVENT_TRADE = "eTrade."
EVENT_ACCOUNT = "eAccount."
EVENT_POSITION = "ePosition."
EVENT_TIMER = "eTimer."
EVENT_CONTRACT = "eContract."


@dataclass
class Event:
    type: str
    data: Any = None


class EventEngine:
    """轻量事件引擎：queue + consumer thread + timer thread"""

    def __init__(self, interval: int = 1):
        self._queue: Queue = Queue()
        self._active = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._timer = threading.Thread(target=self._run_timer, daemon=True)
        self._interval = interval
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def start(self):
        self._active = True
        self._thread.start()
        self._timer.start()

    def stop(self):
        self._active = False

    def register(self, event_type: str, handler: Callable):
        self._handlers[event_type].append(handler)

    def unregister(self, event_type: str, handler: Callable):
        handler_list = self._handlers.get(event_type)
        if handler_list and handler in handler_list:
            handler_list.remove(handler)

    def put(self, event: Event):
        self._queue.put(event)

    def _run(self):
        while self._active:
            try:
                event = self._queue.get(timeout=1)
                self._process(event)
            except Empty:
                pass

    def _process(self, event: Event):
        for handler in self._handlers.get(event.type, []):
            try:
                handler(event)
            except Exception as e:
                print(f"EventEngine handler error: {e}")

    def _run_timer(self):
        import time
        while self._active:
            time.sleep(self._interval)
            event = Event(type=EVENT_TIMER)
            self.put(event)
