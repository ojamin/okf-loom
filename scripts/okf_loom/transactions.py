"""Reentrant advisory transactions shared by threads, instances and processes.

Locks coordinate Loom writers. An unrelated editor that ignores advisory locks
can still race a replacement; callers must retain optimistic revision checks.
Lock files live in the ignored session directory, never in content metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os
import threading
from weakref import WeakValueDictionary

_registry_guard = threading.Lock()
_locks: WeakValueDictionary = WeakValueDictionary()
_local = threading.local()


@contextmanager
def file_transaction(path: Path):
    key = str(path.resolve())
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - Windows CI
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                if os.name == "nt":  # pragma: no cover
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def serialized(method):
    """Serialize a Studio operation, including nested operations."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.transaction():
            return method(self, *args, **kwargs)
    return wrapped
