"""``handy-worker run``: process the whole queue, newest first.

Idempotent: every trigger (nightly launchd run, login catch-up, Handy's idle
trigger, the "Run now" button) calls the same thing, and a missed run simply
leaves more for the next one.

Order of a run:

1. Take the single-run lock (another run → exit quietly).
2. Read Handy's schema and the list of dictations, read-only, then close
   that connection. Unknown schema → stop and report.
3. Reconcile the queue; nothing pending → done.
4. Check run conditions (battery, Low Power Mode, thermal, disk, memory,
   load). Not met → skip; the next trigger tries again.
5. Keep the Mac awake on mains power, load the model, and transcribe each
   entry, checking before every entry and at every decoding step whether to
   yield (Handy recording/transcribing) or stop (deadline, power, user back,
   SIGTERM). Each entry is committed as soon as it is done.
6. Always: unload the model, let the Mac sleep, record the outcome, release
   the lock, and raise a notification only if the service needs attention.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import FrameType
from typing import Any

from handy_worker import __version__, audio, busy, conditions, handy_db
from handy_worker import results_db as rdb
from handy_worker.config import WorkerConfig
from handy_worker.lock import LockHeldError, RunLock
from handy_worker.paths import Paths
from handy_worker.transcribe import transcribe_audio
from handy_worker.transcribers.base import ModelUnavailableError, Transcriber, Yielded

log = logging.getLogger(__name__)

# Triggers.
SCHEDULE = "schedule"
LOGIN = "login"
IDLE = "idle"
MANUAL = "manual"
LAUNCHD = "launchd"  # resolved to SCHEDULE or LOGIN by the time of day

# Outcomes.
OK = "ok"  # queue drained (or nothing to do)
PARTIAL = "partial"  # stopped early (deadline, yield, conditions); resumes later
SKIPPED = "skipped"  # did not start: conditions not met / already running
BLOCKED = "blocked"  # needs attention: unknown schema, model missing, ...
ERROR = "error"  # entries failed, or the run crashed

ATTENTION_KEY = "attention"
NOTIFIED_KEY = "notified"
#: Notify when this many runs in a row ended in error/blocked.
NOTIFY_AFTER_FAILED_RUNS = 3


@dataclass
class RunResult:
    outcome: str
    message: str = ""
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    yields: int = 0
    gave_up: list[int] = field(default_factory=list)
    queue_before: int = 0
    queue_after: int = 0
    audio_s: float = 0.0
    processing_s: float = 0.0
    run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Clock:
    """Injectable time for tests."""

    now: Callable[[], datetime] = datetime.now
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def resolve_trigger(trigger: str, config: WorkerConfig, now: datetime) -> str:
    """launchd doesn't say why it started us: at the scheduled time (or on
    wake after sleeping through it, before the stop-by time) it's the
    nightly run; otherwise it's RunAtLoad at login."""
    if trigger != LAUNCHD:
        return trigger
    scheduled = now.replace(
        hour=config.schedule_hour, minute=config.schedule_minute, second=0, microsecond=0
    )
    if scheduled > now:
        scheduled -= timedelta(days=1)
    stop_by = scheduled.replace(hour=config.stop_by_hour, minute=config.stop_by_minute)
    if stop_by <= scheduled:
        stop_by += timedelta(days=1)
    return SCHEDULE if now - scheduled < timedelta(minutes=10) or now < stop_by else LOGIN


def deadline_for(trigger: str, config: WorkerConfig, now: datetime) -> datetime | None:
    """Nightly runs stop by the configured time. Other triggers have no
    clock deadline: idle runs stop when the user is back, and every run
    yields to dictation."""
    if trigger != SCHEDULE or not config.time_budget_enabled:
        return None
    stop = now.replace(
        hour=config.stop_by_hour, minute=config.stop_by_minute, second=0, microsecond=0
    )
    if stop <= now:
        stop += timedelta(days=1)
    return stop


class Runner:
    def __init__(
        self,
        paths: Paths,
        config: WorkerConfig,
        transcriber_factory: Callable[[], Transcriber],
        *,
        probe: conditions.SystemProbe | None = None,
        clock: Clock | None = None,
        notifier: Callable[[str, str], None] | None = None,
        keep_awake: Callable[[], Callable[[], None]] | None = None,
        install_signal_handlers: bool = False,
    ) -> None:
        self.paths = paths
        self.config = config
        self.transcriber_factory = transcriber_factory
        self.probe = probe or conditions.default_probe()
        self.clock = clock or Clock()
        self.notifier = notifier
        self.keep_awake = keep_awake
        self.install_signal_handlers = install_signal_handlers
        self._signal_reason: str | None = None

    # -- entry point --------------------------------------------------------

    def run(self, trigger: str) -> RunResult:
        trigger = resolve_trigger(trigger, self.config, self.clock.now())
        try:
            lock = RunLock(self.paths.lock_file, trigger).acquire()
        except LockHeldError as e:
            log.info("Not starting: %s", e)
            return RunResult(SKIPPED, str(e))
        if lock.recovered:
            log.warning("Recovered a stale lock left by PID %s", lock.recovered.get("pid"))
        db = rdb.ResultsDb.open(self.paths.results_db)
        try:
            db.close_abandoned_runs()
            run_id = db.start_run(trigger, __version__)
            result = RunResult(ERROR, "the run did not finish")
            try:
                result = self._run(db, trigger)
            except Exception as e:
                log.exception("Run crashed")
                result = RunResult(ERROR, f"crashed: {e}")
            finally:
                result.run_id = run_id
                db.finish_run(
                    run_id,
                    outcome=result.outcome,
                    message=result.message,
                    processed=result.processed,
                    failed=result.failed,
                    skipped=result.skipped,
                    yields=result.yields,
                    queue_before=result.queue_before,
                    queue_after=result.queue_after,
                    audio_s=round(result.audio_s, 2),
                    processing_s=round(result.processing_s, 2),
                )
                self._update_attention(db, result)
            log.info(
                "Run %s (%s) ended: %s — %s [done %d, failed %d, skipped %d, left %d]",
                run_id,
                trigger,
                result.outcome,
                result.message,
                result.processed,
                result.failed,
                result.skipped,
                result.queue_after,
            )
            return result
        finally:
            db.close()
            lock.release()

    # -- the run ------------------------------------------------------------

    def _run(self, db: rdb.ResultsDb, trigger: str) -> RunResult:
        try:
            snapshot = handy_db.read_snapshot(self.paths.history_db)  # closed on return
        except handy_db.UnknownSchemaError as e:
            return RunResult(BLOCKED, f"unknown Handy database schema: {e}")
        except handy_db.HandyDbError as e:
            return RunResult(BLOCKED, str(e))

        def audio_exists(c: handy_db.Candidate) -> bool:
            return handy_db.find_audio(c, self.paths.app_data) is not None

        sync = db.sync_queue(snapshot.candidates, audio_exists)
        if sync.added or sync.deleted or sync.requeued:
            log.info(
                "Queue: %d new, %d no longer in Handy, %d re-queued",
                sync.added,
                sync.deleted,
                sync.requeued,
            )
        queue = db.pending()
        result = RunResult(OK, "", queue_before=len(queue))
        if not queue:
            result.message = "nothing to do"
            return result

        verdict = conditions.check_start(self.config, self.probe, self.paths.worker_data)
        if not verdict.ok:
            result.outcome = SKIPPED
            result.message = "; ".join(verdict.reasons)
            result.queue_after = len(queue)
            return result

        by_id = {c.entry_id: c for c in snapshot.candidates}
        started = self.clock.now()
        deadline = deadline_for(trigger, self.config, started)
        stop = busy.StopSignal(
            self.paths.busy_flag, extra=lambda: self._extra_stop_reason(trigger, deadline)
        ).start()
        release_awake = self._keep_awake()
        previous_handlers = self._install_signals(stop)
        transcriber = self.transcriber_factory()
        try:
            self._wait_until_not_busy(stop, result)
            if stop.stop_reason:
                result.outcome = PARTIAL
                result.message = stop.stop_reason
            else:
                transcriber.load()
                self._process(db, transcriber, queue, by_id, stop, result)
        except ModelUnavailableError as e:
            result.outcome = BLOCKED
            result.message = f"model unavailable: {e}"
        finally:
            transcriber.unload()  # free the memory the moment we're done
            stop.close()
            release_awake()
            self._restore_signals(previous_handlers)
        result.queue_after = len(db.pending())
        return result

    def _process(
        self,
        db: rdb.ResultsDb,
        transcriber: Transcriber,
        queue: list[rdb.QueueItem],
        by_id: dict[int, handy_db.Candidate],
        stop: busy.StopSignal,
        result: RunResult,
    ) -> None:
        index = 0
        while index < len(queue):
            item = queue[index]
            if stop.stop_reason is None and stop.busy.is_set():
                self._wait_until_not_busy(stop, result)
            if stop.stop_reason:
                result.outcome = PARTIAL
                result.message = stop.stop_reason
                return
            between = self._between_entries_reason()
            if between:
                result.outcome = PARTIAL
                result.message = between
                return
            cand = by_id.get(item.entry_id)
            outcome = self._process_one(db, transcriber, item, cand, stop, result)
            if outcome == "yielded":
                result.yields += 1
                continue  # same entry again after the pause (resumes from its checkpoint)
            if outcome == "stopped":
                result.outcome = PARTIAL
                result.message = stop.stop_reason or "stopped"
                return
            index += 1
        if result.failed and not result.processed:
            result.outcome = ERROR
            result.message = f"{result.failed} entr{'y' if result.failed == 1 else 'ies'} failed"
        elif result.outcome == OK:
            result.message = f"transcribed {result.processed}" + (
                f", {result.failed} failed" if result.failed else ""
            )

    def _process_one(
        self,
        db: rdb.ResultsDb,
        transcriber: Transcriber,
        item: rdb.QueueItem,
        cand: handy_db.Candidate | None,
        stop: busy.StopSignal,
        result: RunResult,
    ) -> str:
        entry_id = item.entry_id
        try:
            still_there = handy_db.entry_exists(self.paths.history_db, entry_id)
        except handy_db.HandyDbError:
            still_there = cand is not None
        if cand is None or not still_there:
            db.record_skip(entry_id, rdb.SKIP_DELETED)
            result.skipped += 1
            return "skipped"
        path = handy_db.find_audio(cand, self.paths.app_data)
        if path is None:
            db.record_skip(entry_id, rdb.SKIP_MISSING_AUDIO, "audio file not found")
            result.skipped += 1
            return "skipped"
        try:
            sha = rdb.sha256_file(path)
            recording = audio.load(path)
        except (audio.AudioError, OSError) as e:
            db.record_skip(entry_id, rdb.SKIP_UNREADABLE, str(e))
            result.skipped += 1
            return "skipped"

        done = db.load_checkpoint(entry_id, sha)
        t0 = self.clock.monotonic()
        try:
            full = transcribe_audio(
                transcriber,
                recording,
                stop.should_stop,
                done=done,
                on_chunk=lambda chunk, _total: db.save_checkpoint(entry_id, sha, chunk),
            )
        except Yielded:
            if stop.stop_reason is None and stop.busy.is_set():
                log.info("Entry %d: yielding to Handy (%s)", entry_id, stop.busy_reason)
                return "yielded"
            return "stopped"
        except ModelUnavailableError:
            raise
        except Exception as e:
            log.warning("Entry %d failed: %s", entry_id, e, exc_info=True)
            if db.record_failure(entry_id, f"{type(e).__name__}: {e}", self.config.max_attempts):
                result.gave_up.append(entry_id)
            result.failed += 1
            return "failed"
        elapsed = self.clock.monotonic() - t0
        db.record_success(
            entry_id,
            text=full.text,
            words=full.words,
            info=transcriber.info(),
            language=self.config.language,
            audio_duration_s=recording.duration_s,
            processing_s=elapsed,
            audio_sha256=sha,
            worker_version=__version__,
        )
        result.processed += 1
        result.audio_s += recording.duration_s
        result.processing_s += elapsed
        return "done"

    # -- stop conditions ------------------------------------------------------

    def _extra_stop_reason(self, trigger: str, deadline: datetime | None) -> str | None:
        if self._signal_reason:
            return self._signal_reason
        if deadline is not None and self.clock.now() >= deadline:
            return f"time budget reached ({deadline:%H:%M})"
        reason = conditions.check_continue(self.config, self.probe)
        if reason:
            return reason
        if trigger == IDLE:
            idle = self.probe.user_idle_seconds()
            if idle is not None and idle < self.config.user_active_within_s:
                return "you're back at the Mac"
        return None

    def _between_entries_reason(self) -> str | None:
        mem = self.probe.free_memory_bytes()
        # The loaded model itself uses memory, so only stop on real pressure.
        if mem is not None and mem < self.config.min_free_memory_gb * conditions.GB / 2:
            return "memory is running low"
        disk = self.probe.free_disk_bytes(self.paths.worker_data)
        if disk is not None and disk < self.config.min_free_disk_gb * conditions.GB:
            return "disk space is running low"
        return None

    def _wait_until_not_busy(self, stop: busy.StopSignal, result: RunResult) -> None:
        """Pause while Handy records or transcribes, then wait a cooldown so
        back-to-back dictations aren't interleaved with worker bursts. Gives
        up after ``max_pause_s`` (the run ends; the next trigger resumes)."""
        if not stop.busy.is_set():
            return
        began = self.clock.monotonic()
        quiet_since: float | None = None
        while True:
            now = self.clock.monotonic()
            if stop.busy.is_set():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= self.config.resume_cooldown_s:
                return
            if now - began >= self.config.max_pause_s:
                stop.request_stop("Handy stayed busy; will resume on the next run")
                return
            if stop.stop_reason is not None:
                return
            extra = self._extra_stop_reason(MANUAL, None)
            if extra:
                stop.request_stop(extra)
                return
            self.clock.sleep(0.25)

    # -- plumbing -------------------------------------------------------------

    def _keep_awake(self) -> Callable[[], None]:
        if self.probe.on_battery():
            return lambda: None  # only prevent sleep on mains power
        if self.keep_awake is not None:
            return self.keep_awake()
        from handy_worker.system import Caffeinate

        c = Caffeinate()
        c.start()
        return c.stop

    def _install_signals(self, stop: busy.StopSignal) -> dict[int, Any]:
        if not self.install_signal_handlers:
            return {}

        def handler(signum: int, _frame: FrameType | None) -> None:
            self._signal_reason = f"stopped by signal {signal.Signals(signum).name}"
            stop.request_stop(self._signal_reason)

        previous: dict[int, Any] = {}
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[sig] = signal.signal(sig, handler)
        return previous

    def _restore_signals(self, previous: dict[int, Any]) -> None:
        for sig, h in previous.items():
            signal.signal(sig, h)

    def _update_attention(self, db: rdb.ResultsDb, result: RunResult) -> None:
        """Remember what needs attention (for status and Handy) and notify
        once per new problem. Never notifies on success."""
        problems: list[str] = []
        recent = db.recent_runs(NOTIFY_AFTER_FAILED_RUNS)
        bad = [r for r in recent if r.get("outcome") in (ERROR, BLOCKED)]
        if result.outcome == BLOCKED:
            problems.append(result.message)
        elif len(recent) >= NOTIFY_AFTER_FAILED_RUNS and len(bad) == len(recent):
            problems.append(f"the last {len(recent)} runs failed: {result.message}")
        if result.gave_up:
            ids = ", ".join(str(i) for i in result.gave_up)
            problems.append(
                f"gave up on {len(result.gave_up)} recording(s) after "
                f"{self.config.max_attempts} attempts (entries {ids})"
            )
        if result.outcome in (OK, PARTIAL) and not result.gave_up:
            db.set_meta(ATTENTION_KEY, None)
            db.set_meta(NOTIFIED_KEY, None)
            return
        if not problems:
            return
        text = "; ".join(problems)
        db.set_meta(ATTENTION_KEY, text)
        if self.config.notify and self.notifier is not None and db.get_meta(NOTIFIED_KEY) != text:
            self.notifier("Verbatim analysis needs attention", text)
            db.set_meta(NOTIFIED_KEY, text)


def file_notifier(app_name: str) -> Callable[[str, str], None]:
    from handy_worker.system import notify

    return lambda title, body: notify(title, body, app_name)
