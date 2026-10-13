"""Serialize cooperative sheet writers in this checkout across threads/processes."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import time


@contextmanager
def sheet_write_lock(provider, workbook):
    directory = Path(__file__).resolve().parents[1] / '.cache' / 'sheet-locks'
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f'{provider}:{workbook}'.encode()).hexdigest()
    with (directory / f'{key}.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'\0')
                handle.flush()
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + 180
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Sheet writer lock timed out') from None
                time.sleep(.1)
        try:
            yield
        finally:
            release()
