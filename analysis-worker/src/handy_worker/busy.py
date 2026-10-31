"""Handy's "recording active" flag, and the worker's stop signal.

Handy writes ``<app data>/handy-busy.json`` when a recording starts and
keeps it while it records, transcribes, pastes or runs a background
transcription job; it deletes the file when it is done, and refreshes
``heartbeat`` every few seconds while busy. A crashed Handy can never leave
the worker blocked: the flag counts only while Handy's PID is alive and the
heartbeat is fresh.

The worker watches the flag from a thread (a ``stat`` every 100 ms) so the
model's per-step stop check is just reading an ``Event``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: A flag whose heartbeat is older than this is stale (Handy refreshes it
#: every 10 s while busy).
STALE_AFTER_S = 45.0
POLL_S = 0.1


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


@dataclass(frozen=True)
class BusyState:
    active: bool
    reason: str
    state: str | None = None
    pid: int | None = None
    age_s: float | None = None


def read_flag(path: Path, now: float | None = None) -> BusyState:
    """Whether Handy is busy right now, judged from the flag file."""
    now = time.time() if now is None else now
    try:
        st = path.stat()
    except FileNotFoundError:
        return BusyState(False, "no flag")
    except OSError as e:
        # Can't tell: be conservative, dictation must never be degraded.
        return BusyState(True, f"flag unreadable ({e})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        return BusyState(False, "no flag")
    except (OSError, ValueError):
        # Half-written or garbage: trust it only while it is fresh.
        age = now - st.st_mtime
        return BusyState(age <= STALE_AFTER_S, "flag unparsable", age_s=age)
    pid = data.get("pid")
    heartbeat = data.get("heartbeat", data.get("since"))
    state = data.get("state")
    age = now - float(heartbeat) if isinstance(heartbeat, int | float) else now - st.st_mtime
    if isinstance(pid, int) and not pid_alive(pid):
        return BusyState(False, "stale: Handy is not running", str(state), pid, age)
    if age > STALE_AFTER_S:
        return BusyState(False, "stale: heartbeat too old", str(state), pid, age)
    return BusyState(True, f"Handy is {state or 'busy'}", str(state), pid, age)


class StopSignal:
    """Combines every reason to stop into one cheap ``should_stop()``.

    ``busy`` is refreshed by a watcher thread; ``extra`` checks (deadline,
    power, user activity, SIGTERM) are evaluated at most every
    ``extra_every_s`` so the per-decoding-step call stays cheap."""

    def __init__(
        self,
        flag_path: Path,
        extra: Callable[[], str | None] | None = None,
        extra_every_s: float = 2.0,
        poll_s: float = POLL_S,
    ) -> None:
        self.flag_path = flag_path
        self.extra = extra
        self.extra_every_s = extra_every_s
        self.poll_s = poll_s
        self.busy = threading.Event()
        self.busy_reason = ""
        self.stop_reason: str | None = None
        self._last_extra = 0.0
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> StopSignal:
        self._refresh()
        self._thread = threading.Thread(target=self._watch, name="busy-flag", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _refresh(self) -> None:
        state = read_flag(self.flag_path)
        if state.active:
            self.busy_reason = state.reason
            self.busy.set()
        else:
            self.busy.clear()

    def _watch(self) -> None:
        while not self._closed.wait(self.poll_s):
            self._refresh()

    def request_stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason

    def should_stop(self) -> bool:
        if self.busy.is_set() or self.stop_reason is not None:
            return True
        if self.extra is not None:
            now = time.monotonic()
            if now - self._last_extra >= self.extra_every_s:
                self._last_extra = now
                reason = self.extra()
                if reason:
                    self.request_stop(reason)
                    return True
        return False
