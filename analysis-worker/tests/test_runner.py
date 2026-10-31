"""The queue runner: catch-up, retries, yielding, read-only access, schema."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from handy_worker import results_db as rdb
from handy_worker import runner
from handy_worker.audio import Audio
from handy_worker.config import WorkerConfig
from handy_worker.lock import RunLock
from handy_worker.transcribers.fake import FakeTranscriber
from tests.conftest import HandyFixture
from tests.fakes import FakeProbe, write_busy_flag

FAST = WorkerConfig(resume_cooldown_s=0.0, max_pause_s=5.0, transcriber="fake")


def make_runner(
    handy: HandyFixture,
    transcriber: FakeTranscriber | None = None,
    *,
    config: WorkerConfig = FAST,
    probe: FakeProbe | None = None,
    now: datetime | None = None,
    notes: list[tuple[str, str]] | None = None,
) -> tuple[runner.Runner, FakeTranscriber]:
    t = transcriber or FakeTranscriber()
    clock = runner.Clock(now=(lambda: now) if now else datetime.now)
    r = runner.Runner(
        handy.paths,
        config,
        lambda: t,
        probe=probe or FakeProbe(),
        clock=clock,
        notifier=(lambda a, b: notes.append((a, b))) if notes is not None else None,
        keep_awake=lambda: lambda: None,
        stop_check_interval_s=0.5,
    )
    return r, t


def results(handy: HandyFixture) -> rdb.ResultsDb:
    return rdb.ResultsDb.open(handy.paths.results_db)


def test_processes_everything_newest_first_and_unloads(handy: HandyFixture) -> None:
    for ts in (100, 200, 300):
        handy.add_dictation(ts)
    order: list[float] = []
    t = FakeTranscriber()
    original = t.transcribe_chunk

    def spy(audio: Audio, should_stop: Any) -> Any:
        order.append(audio.duration_s)
        return original(audio, should_stop)

    t.transcribe_chunk = spy  # type: ignore[method-assign]
    r, _ = make_runner(handy, t)
    res = r.run(runner.MANUAL)
    assert res.outcome == runner.OK and res.processed == 3 and res.queue_after == 0
    assert not t.loaded  # model unloaded at the end
    db = results(handy)
    done = db.conn.execute("SELECT entry_id FROM transcripts ORDER BY processed_at").fetchall()
    assert len(done) == 3
    assert db.recent_runs()[0]["outcome"] == runner.OK
    # Second run: nothing to do, model never loaded.
    t2 = FakeTranscriber()
    r2, _ = make_runner(handy, t2)
    assert r2.run(runner.MANUAL).message == "nothing to do"
    assert t2.load_count == 0


def test_catch_up_after_missed_runs(handy: HandyFixture) -> None:
    # A week of dictations while the Mac was off: one run does them all.
    day = 86_400
    ids = [handy.add_dictation(1_700_000_000 + d * day + k) for d in range(7) for k in range(2)]
    r, _ = make_runner(handy)
    res = r.run(runner.LOGIN)
    assert res.processed == len(ids) and res.queue_before == len(ids)
    db = results(handy)
    assert db.counts()["done"] == len(ids)


def test_retries_then_gives_up_and_manual_retry_works(handy: HandyFixture) -> None:
    good = handy.add_dictation(100)
    bad = handy.add_dictation(200, seconds=2.0)
    flaky = FakeTranscriber(fail=lambda a: "decoder exploded" if a.duration_s > 1.5 else None)
    notes: list[tuple[str, str]] = []
    for attempt in range(1, 4):
        r, _ = make_runner(handy, flaky, notes=notes)
        res = r.run(runner.MANUAL)
        assert res.failed == 1
        s = results(handy).status_of(bad)
        assert s is not None and s["attempts"] == attempt
    s = results(handy).status_of(bad)
    assert s is not None and s["status"] == rdb.FAILED and "exploded" in s["last_error"]
    assert results(handy).status_of(good)["status"] == rdb.DONE  # type: ignore[index]
    # Given up once → one notification, not one per run.
    assert len(notes) == 1 and "gave up" in notes[0][1]
    r, _ = make_runner(handy, flaky, notes=notes)
    assert r.run(runner.MANUAL).message == "nothing to do"

    assert results(handy).retry(bad)
    r, _ = make_runner(handy, FakeTranscriber(), notes=notes)
    assert r.run(runner.MANUAL).processed == 1


def test_missing_and_unreadable_audio_are_skipped_not_failed(handy: HandyFixture) -> None:
    gone = handy.add_dictation(100, with_audio=False)
    broken = handy.add_dictation(200)
    (handy.paths.dataset_audio_dir / "handy-200.wav").write_bytes(b"RIFF\0\0\0\0WAVEjunk")
    r, _ = make_runner(handy)
    res = r.run(runner.MANUAL)
    assert res.skipped == 2 and res.failed == 0
    db = results(handy)
    assert db.status_of(gone)["skip_reason"] == rdb.SKIP_MISSING_AUDIO  # type: ignore[index]
    assert db.status_of(broken)["skip_reason"] == rdb.SKIP_UNREADABLE  # type: ignore[index]


def test_entry_deleted_from_handy_is_skipped(handy: HandyFixture) -> None:
    keep = handy.add_dictation(100)
    drop = handy.add_dictation(200)
    r, _ = make_runner(handy)
    # Delete between the snapshot and processing.
    t = FakeTranscriber()

    def delete_then_load() -> FakeTranscriber:
        conn = handy.connect()
        conn.execute("DELETE FROM dictation_log WHERE id = ?", (drop,))
        conn.commit()
        conn.close()
        return t

    r.transcriber_factory = delete_then_load
    res = r.run(runner.MANUAL)
    assert res.processed == 1 and res.skipped == 1
    db = results(handy)
    assert db.status_of(drop)["skip_reason"] == rdb.SKIP_DELETED  # type: ignore[index]
    assert db.status_of(keep)["status"] == rdb.DONE  # type: ignore[index]


def test_yields_within_a_second_when_recording_starts_and_resumes(
    handy: HandyFixture,
) -> None:
    handy.add_dictation(100, seconds=1.0)
    handy.add_dictation(200, seconds=1.0)
    flag = handy.paths.busy_flag
    seen: dict[str, float] = {}

    def on_step(step: int) -> None:
        if step == 5 and "flag_at" not in seen:
            write_busy_flag(flag)
            seen["flag_at"] = time.monotonic()

            def finish_dictation() -> None:
                time.sleep(1.5)
                flag.unlink()

            threading.Thread(target=finish_dictation, daemon=True).start()

    t = FakeTranscriber(work_s=3.0, on_step=on_step)
    original = t.transcribe_chunk

    def timed(audio: Audio, should_stop: Any) -> Any:
        def stop() -> bool:
            s = should_stop()
            if s and "flag_at" in seen and "stopped_at" not in seen:
                seen["stopped_at"] = time.monotonic()
            return bool(s)

        return original(audio, stop)

    t.transcribe_chunk = timed  # type: ignore[method-assign]
    r, _ = make_runner(handy, t)
    res = r.run(runner.MANUAL)
    assert seen["stopped_at"] - seen["flag_at"] < 1.0  # non-negotiable #1
    assert res.yields >= 1
    assert res.outcome == runner.OK and res.processed == 2


def test_stops_after_max_pause_and_keeps_entry_pending(handy: HandyFixture) -> None:
    entry = handy.add_dictation(100)
    write_busy_flag(handy.paths.busy_flag)  # Handy busy the whole time
    cfg = WorkerConfig(resume_cooldown_s=0, max_pause_s=0.5, transcriber="fake")
    r, t = make_runner(handy, config=cfg)
    res = r.run(runner.MANUAL)
    assert res.outcome == runner.PARTIAL and "busy" in res.message
    assert t.chunks_done == 0
    assert results(handy).status_of(entry)["status"] == rdb.PENDING  # type: ignore[index]


def test_stale_busy_flag_from_a_dead_handy_is_ignored(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    write_busy_flag(handy.paths.busy_flag, pid=2**22 + 12345)  # no such process
    r, _ = make_runner(handy)
    assert r.run(runner.MANUAL).processed == 1


def test_history_db_is_released_before_transcription(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    before = handy.paths.history_db.read_bytes()
    checked: list[bool] = []

    def on_step(_: int) -> None:
        # Handy must be able to take an exclusive write lock mid-transcription.
        conn = sqlite3.connect(handy.paths.history_db, timeout=0)
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        conn.close()
        checked.append(True)

    r, _ = make_runner(handy, FakeTranscriber(on_step=on_step))
    assert r.run(runner.MANUAL).processed == 1
    assert checked
    assert handy.paths.history_db.read_bytes() == before  # never written


def test_all_history_db_connections_are_read_only(
    handy: HandyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    handy.add_dictation(100)
    uris: list[str] = []
    real = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if "history.db" in str(database):
            uris.append(str(database))
            assert kwargs.get("uri") is True
        conn: sqlite3.Connection = real(database, *args, **kwargs)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    r, _ = make_runner(handy)
    r.run(runner.MANUAL)
    assert uris and all(u.endswith("?mode=ro") for u in uris)


def test_unknown_schema_blocks_and_notifies_once(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    conn = handy.connect()
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    notes: list[tuple[str, str]] = []
    for _ in range(2):
        r, t = make_runner(handy, notes=notes)
        res = r.run(runner.SCHEDULE)
        assert res.outcome == runner.BLOCKED and "schema" in res.message
        assert t.load_count == 0
    assert len(notes) == 1
    assert "schema" in (results(handy).get_meta(runner.ATTENTION_KEY) or "")


def test_model_missing_blocks(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    r, _ = make_runner(handy, FakeTranscriber(available=False))
    res = r.run(runner.MANUAL)
    assert res.outcome == runner.BLOCKED and "model" in res.message


def test_skips_on_battery_and_low_resources(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    for probe, word in [
        (FakeProbe(battery=True), "battery"),
        (FakeProbe(low_power=True), "Low Power"),
        (FakeProbe(thermal=True), "thermal"),
        (FakeProbe(disk=1024), "disk"),
        (FakeProbe(memory=1024), "memory"),
        (FakeProbe(load=5.0), "busy"),
    ]:
        r, t = make_runner(handy, probe=probe)
        res = r.run(runner.SCHEDULE)
        assert res.outcome == runner.SKIPPED and word in res.message
        assert t.load_count == 0
    cfg = WorkerConfig(skip_on_battery=False, transcriber="fake")
    r, _ = make_runner(handy, probe=FakeProbe(battery=True), config=cfg)
    assert r.run(runner.MANUAL).processed == 1


def test_time_budget_stops_a_nightly_run(handy: HandyFixture) -> None:
    for ts in (100, 200):
        handy.add_dictation(ts)
    late = datetime(2026, 10, 31, 6, 59, 59)
    times = iter([late] * 3 + [datetime(2026, 10, 31, 7, 0, 1)] * 100)
    t = FakeTranscriber(work_s=2.5)  # long enough for the extra check to run
    r = runner.Runner(
        handy.paths,
        FAST,
        lambda: t,
        probe=FakeProbe(),
        clock=runner.Clock(now=lambda: next(times)),
        keep_awake=lambda: lambda: None,
        stop_check_interval_s=0.5,
    )
    res = r.run(runner.SCHEDULE)
    assert res.outcome == runner.PARTIAL and "time budget" in res.message
    assert res.queue_after >= 1


def test_idle_run_stops_when_the_user_returns(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    probe = FakeProbe(idle=5.0)  # input 5 s ago
    r, _ = make_runner(handy, FakeTranscriber(work_s=2.5), probe=probe)
    res = r.run(runner.IDLE)
    assert res.outcome == runner.PARTIAL and "back" in res.message


def test_switching_to_battery_mid_run_stops(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    probe = FakeProbe()

    def on_step(step: int) -> None:
        if step == 2:
            probe.battery = True

    r, _ = make_runner(handy, FakeTranscriber(work_s=2.5, on_step=on_step), probe=probe)
    res = r.run(runner.MANUAL)
    assert res.outcome == runner.PARTIAL and "battery" in res.message


def test_a_second_run_does_not_overlap(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    with RunLock(handy.paths.lock_file, "manual"):
        r, t = make_runner(handy)
        res = r.run(runner.MANUAL)
    assert res.outcome == runner.SKIPPED and "in progress" in res.message
    assert t.load_count == 0


def test_stale_lock_from_a_crashed_run_is_taken_over(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    handy.paths.worker_data.mkdir(parents=True, exist_ok=True)
    handy.paths.lock_file.write_text('{"pid": 999999, "started_at": 1, "trigger": "schedule"}')
    r, _ = make_runner(handy)
    assert r.run(runner.MANUAL).processed == 1


def test_three_failed_runs_notify(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    notes: list[tuple[str, str]] = []
    cfg = WorkerConfig(max_attempts=10, transcriber="fake")
    for _ in range(3):
        r, _ = make_runner(handy, FakeTranscriber(fail=lambda a: "nope"), config=cfg, notes=notes)
        assert r.run(runner.MANUAL).outcome == runner.ERROR
    assert len(notes) == 1 and "last 3 runs failed" in notes[0][1]
    # Success clears the attention flag.
    r, _ = make_runner(handy, config=cfg, notes=notes)
    assert r.run(runner.MANUAL).outcome == runner.OK
    assert results(handy).get_meta(runner.ATTENTION_KEY) is None


def test_launchd_trigger_resolution() -> None:
    cfg = WorkerConfig(schedule_hour=2, stop_by_hour=7)
    assert runner.resolve_trigger("launchd", cfg, datetime(2026, 1, 1, 2, 0, 5)) == runner.SCHEDULE
    # Woke at 05:00 after sleeping through 02:00: still the nightly window.
    assert runner.resolve_trigger("launchd", cfg, datetime(2026, 1, 1, 5, 0)) == runner.SCHEDULE
    assert runner.resolve_trigger("launchd", cfg, datetime(2026, 1, 1, 9, 30)) == runner.LOGIN
    assert runner.resolve_trigger("manual", cfg, datetime(2026, 1, 1, 2, 0)) == runner.MANUAL


def test_deadline_only_for_nightly_runs() -> None:
    cfg = WorkerConfig(stop_by_hour=7)
    start = datetime(2026, 1, 1, 2, 0)
    assert runner.deadline_for(runner.SCHEDULE, cfg, start) == datetime(2026, 1, 1, 7, 0)
    assert runner.deadline_for(runner.MANUAL, cfg, start) is None
    off = WorkerConfig(time_budget_enabled=False)
    assert runner.deadline_for(runner.SCHEDULE, off, start) is None


def test_long_entry_resumes_from_its_checkpoint(handy: HandyFixture, tmp_path: Path) -> None:
    entry = handy.add_dictation(100, seconds=60.0)  # 3 chunks
    flag = handy.paths.busy_flag
    cfg = WorkerConfig(resume_cooldown_s=0, max_pause_s=0.3, transcriber="fake")
    t = FakeTranscriber()
    calls = {"n": 0}
    original = t.transcribe_chunk

    def chunk(audio: Audio, should_stop: Any) -> Any:
        calls["n"] += 1
        out = original(audio, should_stop)
        if calls["n"] == 1:
            write_busy_flag(flag)  # dictation starts after chunk 1
            time.sleep(0.3)
        return out

    t.transcribe_chunk = chunk  # type: ignore[method-assign]
    r, _ = make_runner(handy, t, config=cfg)
    res = r.run(runner.MANUAL)
    assert res.outcome == runner.PARTIAL
    db = results(handy)
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM chunk_progress WHERE entry_id = ?", (entry,)
        ).fetchone()[0]
        == 1
    )
    flag.unlink()
    t2 = FakeTranscriber()
    r2, _ = make_runner(handy, t2, config=cfg)
    assert r2.run(runner.MANUAL).processed == 1
    assert t2.chunks_done == 2  # only the remaining chunks
