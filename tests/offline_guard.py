"""测试期网络闸门：非本地连接一律拦截，保证测试不会真的发出通知。

测试里的通知路径本来就在边界上被替身隔离，这个闸门是最后一道保险：
万一日后有人写出直连 webhook/Telegram/邮件的用例，它会立刻失败而不是把消息发出去。

整仓跑一遍（在仓库根目录）：

    conda run --no-capture-output -n futu_trends python -m tests.offline_guard

或直接当脚本跑：

    conda run --no-capture-output -n futu_trends python tests/offline_guard.py

也可以只用上下文管理器包住某个用例，bin 里看到的就是该用例的全部外发尝试：

    with offline() as blocked:
        ...
    self.assertEqual(blocked, [])
"""

from __future__ import annotations

import socket
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
REPO_ROOT = Path(__file__).resolve().parents[1]

_blocked: list[tuple[Any, ...]] = []
_session: list[tuple[Any, ...]] | None = None
_installed = False
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(address: Any) -> bool:
    return isinstance(address, tuple) and str(address[0]) in LOOPBACK


def _guard(address: Any) -> None:
    if _is_local(address):
        return
    target = tuple(address)
    _blocked.append(target)
    if _session is not None:
        _session.append(target)
    raise AssertionError(f"测试里出现真实外发连接，已拦截: {target}")


def _connect(self, address):
    _guard(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _guard(address)
    return _real_connect_ex(self, address)


def install() -> None:
    global _installed
    if _installed:
        return
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    _installed = True


def uninstall() -> None:
    global _installed
    if not _installed:
        return
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    _installed = False


@contextmanager
def offline() -> Iterator[list[tuple[Any, ...]]]:
    """拦截非本地连接；产出一个实时列表，装着本次块内被拦截的目标。"""
    global _session
    previous = _session
    _session = []
    install()
    try:
        yield _session
    finally:
        uninstall()
        _session = previous


def run_suite(pattern: str = "test_*.py", verbosity: int = 2) -> int:
    """在闸门下跑完整测试套件，返回退出码。"""
    sys.path.insert(0, str(REPO_ROOT))
    with offline() as blocked:
        suite = unittest.TestLoader().discover(str(REPO_ROOT / "tests"), pattern=pattern)
        result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

    print()
    if blocked:
        print(f"闸门拦截了 {len(blocked)} 次外发连接: {sorted(set(blocked))}")
    else:
        print("闸门拦截了 0 次外发连接：本次测试没有向任何外部地址发起连接。")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_suite())
