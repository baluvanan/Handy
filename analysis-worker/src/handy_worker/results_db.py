"""The worker's own database: ``<app data>/verbatim/verbatim.db``.

Separate from Handy's ``history.db`` (which the worker never writes), keyed
by the dictation-log entry id, with its own migrations (tracked in this
file's ``PRAGMA user_version``). Handy reads it read-only for Insights and
for retention, so the tables below are the worker↔Handy contract; bump
``SCHEMA_VERSION`` (append a migration) for any change and teach Handy the
new version in the same commit.

Queue state per entry::

    pending ──ok──▶ done
       │ ▲
     error│ │retry (manual)
       ▼ │
    pending (attempts+1) ──attempts == max──▶ failed
    pending ──audio missing / unreadable / entry deleted──▶ skipped

Every state change is its own committed transaction, so an interruption
loses at most the entry in progress (and long entries keep per-chunk
checkpoints, so not even all of that).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handy_worker.transcribe import DoneChunk
from handy_worker.transcribers.base import ChunkResult, ModelInfo, Word

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

SKIP_MISSING_AUDIO = "missing_audio"
SKIP_UNREADABLE = "unreadable_audio"
SKIP_DELETED = "deleted"

MIGRATIONS: tuple[str, ...] = (
    # v1
    """
    CREATE TABLE entries (
        entry_id INTEGER PRIMARY KEY,
        history_id INTEGER,
        audio_file TEXT,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        skip_reason TEXT,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_entries_status ON entries(status, created_at);
    CREATE TABLE transcripts (
        entry_id INTEGER PRIMARY KEY,
        history_id INTEGER,
        audio_file TEXT,
        text TEXT NOT NULL,
        words TEXT NOT NULL,
        language TEXT,
        model_name TEXT NOT NULL,
        model_id TEXT NOT NULL,
        model_revision TEXT,
        device TEXT,
        processed_at INTEGER NOT NULL,
        audio_duration_s REAL NOT NULL,
        processing_s REAL NOT NULL,
        audio_sha256 TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        verify_error TEXT,
        worker_version TEXT NOT NULL
    );
    CREATE TABLE chunk_progress (
        entry_id INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL,
        audio_sha256 TEXT NOT NULL,
        offset_s REAL NOT NULL,
        text TEXT NOT NULL,
        words TEXT NOT NULL,
        PRIMARY KEY (entry_id, chunk_index)
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        outcome TEXT,
        message TEXT,
        processed INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        yields INTEGER NOT NULL DEFAULT 0,
        queue_before INTEGER,
        queue_after INTEGER,
        audio_s REAL NOT NULL DEFAULT 0,
        processing_s REAL NOT NULL DEFAULT 0,
        worker_version TEXT,
        model_revision TEXT
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)

#: A recording at least this long that yields no text is suspicious: the
#: transcript is stored but not marked verified (so its audio is never
#: deleted by Handy's retention).
SUSPICIOUS_EMPTY_S = 3.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def words_json(words: Iterable[Word]) -> str:
    return json.dumps([w.as_dict() for w in words], separators=(",", ":"))


def words_from_json(text: str) -> list[Word]:
    return [Word.from_dict(d) for d in json.loads(text)]


@dataclass(frozen=True)
class QueueItem:
    entry_id: int
    created_at: int
    attempts: int


@dataclass(frozen=True)
class SyncStats:
    added: int
    deleted: int
    requeued: int


def verify_transcript(text: str, words: list[Word], duration_s: float) -> str | None:
    """Sanity checks before a transcript may be relied on. Returns why it
    fails, or None when it is fine."""
    if duration_s <= 0:
        return "audio has no duration"
    if not text.strip():
        if duration_s >= SUSPICIOUS_EMPTY_S:
            return f"no text for {duration_s:.1f} s of audio"
        return None
    if not words:
        return "text without word timings"
    prev_start = -1.0
    for w in words:
        if w.start < -0.05 or w.end < w.start - 0.05:
            return f"bad timing for {w.text!r}"
        if w.start + 0.5 < prev_start:
            return "word timings go backwards"
        if w.end > duration_s + 1.0:
            return f"word timing past the end of the audio ({w.end:.2f} s)"
        if w.confidence is not None and not 0.0 <= w.confidence <= 1.0:
            return "confidence out of range"
        prev_start = w.start
    return None


class ResultsDb:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    @classmethod
    def open(cls, path: Path) -> ResultsDb:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")  # Handy reads while we write
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        db = cls(conn, path)
        db.migrate()
        return db

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- schema -----------------------------------------------------------

    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        version = self.version()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} is schema v{version}, newer than this worker (v{SCHEMA_VERSION})"
            )
        while version < SCHEMA_VERSION:
            with self.tx() as c:
                for statement in _split_sql(MIGRATIONS[version]):
                    c.execute(statement)
                version += 1
                c.execute(f"PRAGMA user_version = {version}")
        return version

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str | None) -> None:
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # -- queue ------------------------------------------------------------

    def sync_queue(
        self,
        candidates: Iterable[Any],
        audio_exists: Any = None,
        now: int | None = None,
    ) -> SyncStats:
        """Reconciles the queue with Handy's current list of dictations.
        New ones become pending; entries Handy no longer has are skipped
        (a finished transcript is kept); entries skipped for missing audio
        are re-queued once their audio is back."""
        now = int(time.time()) if now is None else now
        cands = list(candidates)
        ids = {int(c.entry_id) for c in cands}
        added = deleted = requeued = 0
        with self.tx() as c:
            for cand in cands:
                cur = c.execute(
                    "INSERT OR IGNORE INTO entries (entry_id, history_id, audio_file, created_at,"
                    " status, updated_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                    (cand.entry_id, cand.history_id, cand.audio_file, cand.created_at, now),
                )
                if cur.rowcount:
                    added += 1
                else:
                    c.execute(
                        "UPDATE entries SET history_id = ?, audio_file = ? WHERE entry_id = ?"
                        " AND (history_id IS NOT ? OR audio_file IS NOT ?)",
                        (
                            cand.history_id,
                            cand.audio_file,
                            cand.entry_id,
                            cand.history_id,
                            cand.audio_file,
                        ),
                    )
            known = [
                int(r[0])
                for r in c.execute(
                    "SELECT entry_id FROM entries WHERE status IN ('pending', 'failed')"
                    " OR (status = 'skipped' AND skip_reason = ?)",
                    (SKIP_MISSING_AUDIO,),
                )
            ]
            for entry_id in known:
                if entry_id not in ids:
                    c.execute(
                        "UPDATE entries SET status = 'skipped', skip_reason = ?, updated_at = ?"
                        " WHERE entry_id = ?",
                        (SKIP_DELETED, now, entry_id),
                    )
                    deleted += 1
            if audio_exists is not None:
                by_id = {int(x.entry_id): x for x in cands}
                for row in c.execute(
                    "SELECT entry_id FROM entries WHERE status = 'skipped' AND skip_reason = ?",
                    (SKIP_MISSING_AUDIO,),
                ).fetchall():
                    cand = by_id.get(int(row[0]))
                    if cand is not None and audio_exists(cand):
                        c.execute(
                            "UPDATE entries SET status = 'pending', skip_reason = NULL,"
                            " updated_at = ? WHERE entry_id = ?",
                            (now, cand.entry_id),
                        )
                        requeued += 1
        return SyncStats(added, deleted, requeued)

    def pending(self) -> list[QueueItem]:
        """Everything still to do, newest first."""
        rows = self.conn.execute(
            "SELECT entry_id, created_at, attempts FROM entries WHERE status = 'pending'"
            " ORDER BY created_at DESC, entry_id DESC"
        ).fetchall()
        return [QueueItem(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {PENDING: 0, DONE: 0, FAILED: 0, SKIPPED: 0}
        for r in self.conn.execute("SELECT status, COUNT(*) FROM entries GROUP BY status"):
            out[str(r[0])] = int(r[1])
        out["total"] = sum(out.values())
        return out

    def status_of(self, entry_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def failures(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT entry_id, history_id, created_at, status, attempts, last_error, skip_reason,"
            " updated_at FROM entries WHERE status = 'failed'"
            " OR (status = 'pending' AND attempts > 0)"
            " OR (status = 'skipped' AND skip_reason != ?)"
            " ORDER BY updated_at DESC LIMIT ?",
            (SKIP_DELETED, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_failure(self, entry_id: int, error: str, max_attempts: int) -> bool:
        """Counts a failed attempt. Returns True when the entry is given up."""
        with self.tx() as c:
            row = c.execute(
                "SELECT attempts FROM entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            attempts = (int(row[0]) if row else 0) + 1
            gave_up = attempts >= max_attempts
            c.execute(
                "UPDATE entries SET attempts = ?, last_error = ?, status = ?, updated_at = ?"
                " WHERE entry_id = ?",
                (
                    attempts,
                    error[:2000],
                    FAILED if gave_up else PENDING,
                    int(time.time()),
                    entry_id,
                ),
            )
        return gave_up

    def record_skip(self, entry_id: int, reason: str, detail: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE entries SET status = 'skipped', skip_reason = ?, last_error = ?,"
                " updated_at = ? WHERE entry_id = ?",
                (reason, detail, int(time.time()), entry_id),
            )
            c.execute("DELETE FROM chunk_progress WHERE entry_id = ?", (entry_id,))

    def retry(self, entry_id: int) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE entries SET status = 'pending', attempts = 0, last_error = NULL,"
                " skip_reason = NULL, updated_at = ? WHERE entry_id = ? AND status != 'done'",
                (int(time.time()), entry_id),
            )
            return cur.rowcount > 0

    def retry_all_failed(self) -> int:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE entries SET status = 'pending', attempts = 0, last_error = NULL,"
                " updated_at = ? WHERE status = 'failed'"
                " OR (status = 'skipped' AND skip_reason = ?)",
                (int(time.time()), SKIP_UNREADABLE),
            )
            return cur.rowcount

    # -- checkpoints ------------------------------------------------------

    def load_checkpoint(self, entry_id: int, audio_sha256: str) -> list[DoneChunk]:
        rows = self.conn.execute(
            "SELECT chunk_index, offset_s, text, words, audio_sha256 FROM chunk_progress"
            " WHERE entry_id = ? ORDER BY chunk_index",
            (entry_id,),
        ).fetchall()
        if any(r["audio_sha256"] != audio_sha256 for r in rows):
            # The audio changed (e.g. Handy compressed it): start over.
            with self.tx() as c:
                c.execute("DELETE FROM chunk_progress WHERE entry_id = ?", (entry_id,))
            return []
        return [
            DoneChunk(
                index=int(r["chunk_index"]),
                offset=float(r["offset_s"]),
                result=ChunkResult(text=str(r["text"]), words=words_from_json(str(r["words"]))),
            )
            for r in rows
        ]

    def save_checkpoint(self, entry_id: int, audio_sha256: str, chunk: DoneChunk) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO chunk_progress (entry_id, chunk_index, audio_sha256,"
                " offset_s, text, words) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    chunk.index,
                    audio_sha256,
                    chunk.offset,
                    chunk.result.text,
                    words_json(chunk.result.words),
                ),
            )

    # -- results ----------------------------------------------------------

    def record_success(
        self,
        entry_id: int,
        *,
        text: str,
        words: list[Word],
        info: ModelInfo,
        language: str,
        audio_duration_s: float,
        processing_s: float,
        audio_sha256: str,
        worker_version: str,
        now: int | None = None,
    ) -> None:
        """Stores the transcript and marks the entry done (one transaction),
        then re-reads it and records whether it verifies."""
        now = int(time.time()) if now is None else now
        with self.tx() as c:
            row = c.execute(
                "SELECT history_id, audio_file FROM entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            c.execute(
                "INSERT OR REPLACE INTO transcripts (entry_id, history_id, audio_file, text, words,"
                " language, model_name, model_id, model_revision, device, processed_at,"
                " audio_duration_s, processing_s, audio_sha256, verified, verify_error,"
                " worker_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
                (
                    entry_id,
                    row["history_id"] if row else None,
                    row["audio_file"] if row else None,
                    text,
                    words_json(words),
                    language,
                    info.name,
                    info.model_id,
                    info.revision,
                    info.device,
                    now,
                    audio_duration_s,
                    processing_s,
                    audio_sha256,
                    worker_version,
                ),
            )
            c.execute(
                "UPDATE entries SET status = 'done', last_error = NULL, skip_reason = NULL,"
                " updated_at = ? WHERE entry_id = ?",
                (now, entry_id),
            )
            c.execute("DELETE FROM chunk_progress WHERE entry_id = ?", (entry_id,))
        self.verify(entry_id)

    def verify(self, entry_id: int) -> bool:
        """Re-reads the stored transcript (not the in-memory one) and marks it
        verified when it passes the checks."""
        row = self.conn.execute(
            "SELECT text, words, audio_duration_s FROM transcripts WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            words = words_from_json(str(row["words"]))
            problem = verify_transcript(str(row["text"]), words, float(row["audio_duration_s"]))
        except (ValueError, KeyError, TypeError) as e:
            problem = f"unreadable stored words ({e})"
        with self.tx() as c:
            c.execute(
                "UPDATE transcripts SET verified = ?, verify_error = ? WHERE entry_id = ?",
                (0 if problem else 1, problem, entry_id),
            )
        return problem is None

    def transcript(self, entry_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM transcripts WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- runs -------------------------------------------------------------

    def start_run(self, trigger: str, worker_version: str, now: int | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (trigger, started_at, worker_version) VALUES (?, ?, ?)",
                (trigger, int(time.time()) if now is None else now, worker_version),
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {
            "outcome",
            "message",
            "processed",
            "failed",
            "skipped",
            "yields",
            "queue_before",
            "queue_after",
            "audio_s",
            "processing_s",
            "model_revision",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        cols["finished_at"] = int(time.time()) if fields.get("now") is None else fields["now"]
        assignments = ", ".join(f"{k} = ?" for k in cols)
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*cols.values(), run_id))

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close_abandoned_runs(self) -> int:
        """Runs that never finished (the process was killed)."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE runs SET outcome = 'crashed', finished_at = started_at,"
                " message = 'the run ended without recording an outcome' WHERE finished_at IS NULL"
            )
            return cur.rowcount


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
