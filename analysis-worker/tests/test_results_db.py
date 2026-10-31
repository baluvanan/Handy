from __future__ import annotations

from pathlib import Path

from handy_worker import results_db as rdb
from handy_worker.handy_db import Candidate
from handy_worker.transcribe import DoneChunk
from handy_worker.transcribers.base import ChunkResult, ModelInfo, Word

INFO = ModelInfo("fake", "fake/verbatim", "rev1", "cpu")


def cand(i: int, ts: int) -> Candidate:
    return Candidate(i, ts, f"handy-{ts}.wav", None, "parakeet", "x", 1000)


def open_db(tmp_path: Path) -> rdb.ResultsDb:
    return rdb.ResultsDb.open(tmp_path / "verbatim" / "verbatim.db")


def success(db: rdb.ResultsDb, entry_id: int, text: str = "um hi", dur: float = 2.0) -> None:
    words = [Word(w, i * 0.5, i * 0.5 + 0.4, 0.9) for i, w in enumerate(text.split())]
    db.record_success(
        entry_id,
        text=text,
        words=words,
        info=INFO,
        language="en",
        audio_duration_s=dur,
        processing_s=0.5,
        audio_sha256="abc",
        worker_version="test",
    )


def test_migrates_once_and_is_readable_read_only(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    assert db.version() == rdb.SCHEMA_VERSION
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    # Handy opens it like this.
    import sqlite3

    from handy_worker.handy_db import ro_uri

    ro = sqlite3.connect(ro_uri(tmp_path / "verbatim" / "verbatim.db"), uri=True)
    assert ro.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    ro.close()
    db = open_db(tmp_path)
    assert db.version() == rdb.SCHEMA_VERSION
    db.close()


def test_state_machine(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.sync_queue([cand(1, 100), cand(2, 200), cand(3, 300)])
    assert [q.entry_id for q in db.pending()] == [3, 2, 1]  # newest first

    success(db, 3)
    assert db.status_of(3)["status"] == rdb.DONE  # type: ignore[index]
    t = db.transcript(3)
    assert t is not None and t["verified"] == 1 and t["model_revision"] == "rev1"

    assert db.record_failure(2, "boom", max_attempts=3) is False
    assert db.status_of(2)["status"] == rdb.PENDING  # type: ignore[index]
    assert db.record_failure(2, "boom", max_attempts=3) is False
    assert db.record_failure(2, "boom", max_attempts=3) is True
    s = db.status_of(2)
    assert s is not None and s["status"] == rdb.FAILED and s["attempts"] == 3

    db.record_skip(1, rdb.SKIP_MISSING_AUDIO)
    assert db.pending() == []
    assert db.counts() == {"pending": 0, "done": 1, "failed": 1, "skipped": 1, "total": 3}

    assert db.retry(2) is True
    s = db.status_of(2)
    assert s is not None and s["status"] == rdb.PENDING and s["attempts"] == 0
    assert db.retry(3) is False  # done entries stay done


def test_retry_all_failed(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.sync_queue([cand(1, 100), cand(2, 200)])
    for _ in range(3):
        db.record_failure(1, "x", 3)
    db.record_skip(2, rdb.SKIP_UNREADABLE, "bad header")
    assert db.retry_all_failed() == 2
    assert {q.entry_id for q in db.pending()} == {1, 2}


def test_deleted_entries_are_skipped_but_transcripts_kept(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.sync_queue([cand(1, 100), cand(2, 200)])
    success(db, 1)
    stats = db.sync_queue([])  # Handy no longer has either
    assert stats.deleted == 1
    assert db.status_of(2)["skip_reason"] == rdb.SKIP_DELETED  # type: ignore[index]
    assert db.status_of(1)["status"] == rdb.DONE  # type: ignore[index]
    assert db.transcript(1) is not None


def test_missing_audio_is_requeued_when_it_returns(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    c = cand(1, 100)
    db.sync_queue([c])
    db.record_skip(1, rdb.SKIP_MISSING_AUDIO)
    assert db.sync_queue([c], audio_exists=lambda _: False).requeued == 0
    assert db.sync_queue([c], audio_exists=lambda _: True).requeued == 1
    assert [q.entry_id for q in db.pending()] == [1]


def test_checkpoints_resume_and_reset_when_audio_changes(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.sync_queue([cand(1, 100)])
    chunk = DoneChunk(0, 0.0, ChunkResult("hi", [Word("hi", 0, 0.3, 0.8)]))
    db.save_checkpoint(1, "sha-a", chunk)
    loaded = db.load_checkpoint(1, "sha-a")
    assert len(loaded) == 1 and loaded[0].result.words[0].text == "hi"
    assert db.load_checkpoint(1, "sha-b") == []
    assert db.load_checkpoint(1, "sha-a") == []  # cleared


def test_verification_rules() -> None:
    w = [Word("a", 0.0, 0.5, 0.9), Word("b", 0.6, 1.0, 0.8)]
    assert rdb.verify_transcript("a b", w, 1.2) is None
    assert rdb.verify_transcript("", [], 1.0) is None  # short silence
    assert "no text" in (rdb.verify_transcript("", [], 10.0) or "")
    assert "word timings" in (rdb.verify_transcript("a b", [], 1.2) or "")
    assert "past the end" in (rdb.verify_transcript("a", [Word("a", 0, 9.0)], 1.0) or "")
    assert "confidence" in (rdb.verify_transcript("a", [Word("a", 0, 0.5, 1.5)], 1.0) or "")


def test_suspicious_empty_transcript_is_stored_unverified(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.sync_queue([cand(1, 100)])
    success(db, 1, text="", dur=20.0)
    t = db.transcript(1)
    assert t is not None and t["verified"] == 0 and t["verify_error"]


def test_abandoned_runs_are_closed(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    db.start_run("manual", "test")
    assert db.close_abandoned_runs() == 1
    assert db.recent_runs()[0]["outcome"] == "crashed"
