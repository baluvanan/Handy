from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from handy_worker import busy, lock
from tests.fakes import write_busy_flag


def test_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    p = tmp_path / "worker.lock"
    with lock.RunLock(p, "manual"):
        info = lock.inspect(p)
        assert info.held and info.holder and info.holder["pid"] == os.getpid()
        with pytest.raises(lock.LockHeldError):
            lock.RunLock(p, "idle").acquire()
    info = lock.inspect(p)
    assert not info.held and not info.stale
    lock.RunLock(p, "idle").acquire().release()


def test_stale_lock_is_detected_and_taken_over(tmp_path: Path) -> None:
    p = tmp_path / "worker.lock"
    p.write_text(json.dumps({"pid": 999_999, "started_at": 1, "trigger": "schedule"}))
    info = lock.inspect(p)
    assert not info.held and info.stale and info.holder and info.holder["pid"] == 999_999
    run_lock = lock.RunLock(p, "manual").acquire()
    try:
        assert run_lock.recovered is not None and run_lock.recovered["pid"] == 999_999
        assert json.loads(p.read_text())["pid"] == os.getpid()
    finally:
        run_lock.release()


def test_busy_flag_states(tmp_path: Path) -> None:
    flag = tmp_path / "handy-busy.json"
    assert not busy.read_flag(flag).active
    write_busy_flag(flag, "transcribing")
    s = busy.read_flag(flag)
    assert s.active and s.state == "transcribing"
    # Dead PID → stale.
    write_busy_flag(flag, pid=2**22 + 777)
    assert not busy.read_flag(flag).active
    # Old heartbeat → stale (a crashed Handy can't block the worker forever).
    now = time.time()
    flag.write_text(json.dumps({"pid": os.getpid(), "state": "recording", "heartbeat": now - 120}))
    assert not busy.read_flag(flag).active
    # Garbage: trusted only while fresh.
    flag.write_text("{half")
    assert busy.read_flag(flag).active
    assert not busy.read_flag(flag, now=time.time() + 600).active


def test_stop_signal_sees_the_flag_within_the_poll_interval(tmp_path: Path) -> None:
    flag = tmp_path / "handy-busy.json"
    sig = busy.StopSignal(flag).start()
    try:
        assert not sig.should_stop()
        t0 = time.monotonic()
        write_busy_flag(flag)
        while not sig.should_stop():
            assert time.monotonic() - t0 < 1.0
            time.sleep(0.01)
        flag.unlink()
        t0 = time.monotonic()
        while sig.busy.is_set():
            assert time.monotonic() - t0 < 1.0
            time.sleep(0.01)
    finally:
        sig.close()


def test_first_extra_check_runs_right_after_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # monotonic() counts from boot: a Mac that just logged in (RunAtLoad)
    # or a fresh CI runner is only seconds into it. The first check must
    # still run, or a battery/deadline stop would be missed.
    monkeypatch.setattr("handy_worker.busy.time.monotonic", lambda: 1.0)
    sig = busy.StopSignal(tmp_path / "none.json", extra=lambda: "on battery", extra_every_s=60)
    assert sig.should_stop()
    assert sig.stop_reason == "on battery"


def test_stop_signal_extra_checks_are_rate_limited(tmp_path: Path) -> None:
    calls = {"n": 0}

    def extra() -> str | None:
        calls["n"] += 1
        return None

    sig = busy.StopSignal(tmp_path / "none.json", extra=extra, extra_every_s=60)
    for _ in range(1000):
        assert not sig.should_stop()
    assert calls["n"] == 1
