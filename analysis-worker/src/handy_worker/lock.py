"""Single-run lock: two runs never overlap.

``fcntl.flock`` on ``verbatim/worker.lock`` is the real lock (the kernel
drops it when the process dies, so a crash can't wedge the queue); the file
also records the holder's PID, start time and trigger for ``status``. A lock
file left behind by a dead process is detected and taken over.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any


class LockHeldError(RuntimeError):
    def __init__(self, holder: dict[str, Any] | None) -> None:
        self.holder = holder
        pid = holder.get("pid") if holder else "?"
        super().__init__(f"another run is in progress (PID {pid})")


@dataclass
class LockInfo:
    held: bool
    holder: dict[str, Any] | None
    stale: bool


def read_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def inspect(path: Path) -> LockInfo:
    """Is a run in progress? (Never blocks, never takes the lock.)"""
    if not path.exists():
        return LockInfo(False, None, False)
    holder = read_holder(path)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return LockInfo(False, holder, False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return LockInfo(True, holder, False)
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        # A clean release empties the file; a record without a lock holder
        # means that run died.
        return LockInfo(False, holder, holder is not None)
    finally:
        os.close(fd)


class RunLock:
    def __init__(self, path: Path, trigger: str) -> None:
        self.path = path
        self.trigger = trigger
        self._file: IO[str] | None = None
        #: The record a crashed run left behind, if any.
        self.recovered: dict[str, Any] | None = None

    def acquire(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the run
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise LockHeldError(read_holder(self.path)) from e
            raise
        previous = read_holder(self.path)
        if previous and previous.get("pid") != os.getpid():
            # The file outlived its process: a crash or a kill -9.
            self.recovered = previous
        f.seek(0)
        f.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "started_at": int(time.time()),
                "trigger": self.trigger,
                "host": socket.gethostname(),
            },
            f,
        )
        f.flush()
        os.fsync(f.fileno())
        self._file = f
        return self

    def release(self) -> None:
        f = self._file
        if f is None:
            return
        self._file = None
        try:
            f.seek(0)
            f.truncate()
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
