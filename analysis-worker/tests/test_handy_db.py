from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from handy_worker import handy_db
from tests.conftest import SCHEMA_SQL, HandyFixture


def test_fixture_comes_from_real_migrations() -> None:
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    assert "GENERATED from Handy's real migrations" in text
    assert (
        "CREATE TABLE dictation_log" in text or "CREATE TABLE IF NOT EXISTS dictation_log" in text
    )


def test_this_worker_supports_the_fixture_schema(handy: HandyFixture) -> None:
    # Fails when Handy's migrations move on (the Rust test regenerates the
    # fixture) until handy_db's known versions are updated deliberately.
    schema = handy_db.read_schema_only(handy.paths.history_db)
    handy_db.check_schema(schema)
    assert schema.history_version == handy_db.MAX_KNOWN_HISTORY_VERSION
    assert schema.dictation_log_version == handy_db.MAX_KNOWN_DICTATION_LOG_VERSION


def test_lists_dictations_newest_first_and_links_history(handy: HandyFixture) -> None:
    old = handy.add_dictation(100, "first")
    new = handy.add_dictation(200, "second", in_history=True)
    snap = handy_db.read_snapshot(handy.paths.history_db)
    assert [c.entry_id for c in snap.candidates] == [new, old]
    assert snap.candidates[0].raw_asr_text == "second"
    assert snap.candidates[0].history_id is not None
    assert snap.candidates[1].history_id is None


def test_connection_is_read_only(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    with (
        handy_db.connect_ro(handy.paths.history_db) as conn,
        pytest.raises(sqlite3.OperationalError),
    ):
        conn.execute("DELETE FROM dictation_log")
    with (
        handy_db.connect_ro(handy.paths.history_db) as conn,
        pytest.raises(sqlite3.OperationalError),
    ):
        conn.execute("CREATE TABLE x (a)")
    # Nothing changed.
    assert len(handy_db.read_snapshot(handy.paths.history_db).candidates) == 1


def test_the_database_file_is_never_modified(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    before = handy.paths.history_db.read_bytes()
    mtime = handy.paths.history_db.stat().st_mtime_ns
    handy_db.read_snapshot(handy.paths.history_db)
    handy_db.read_schema_only(handy.paths.history_db)
    assert handy.paths.history_db.read_bytes() == before
    assert handy.paths.history_db.stat().st_mtime_ns == mtime


def test_snapshot_releases_the_connection(handy: HandyFixture) -> None:
    handy.add_dictation(100)
    handy_db.read_snapshot(handy.paths.history_db)
    # An exclusive lock is only possible when no reader holds the file.
    writer = sqlite3.connect(handy.paths.history_db, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute(
        "INSERT INTO dictation_log (created_at, raw_asr_text, rule_cleaned_text, final_text)"
        " VALUES (1, 'a', 'a', 'a')"
    )
    writer.commit()
    writer.close()


def test_newer_history_schema_is_refused(handy: HandyFixture) -> None:
    conn = handy.connect()
    conn.execute(f"PRAGMA user_version = {handy_db.MAX_KNOWN_HISTORY_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(handy_db.UnknownSchemaError, match="newer"):
        handy_db.read_snapshot(handy.paths.history_db)


def test_newer_dictation_log_schema_is_refused(handy: HandyFixture) -> None:
    conn = handy.connect()
    conn.execute(
        "UPDATE handy_personal_schema SET version = ? WHERE name = 'dictation_log'",
        (handy_db.MAX_KNOWN_DICTATION_LOG_VERSION + 1,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(handy_db.UnknownSchemaError):
        handy_db.read_snapshot(handy.paths.history_db)


def test_missing_dictation_log_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE transcription_history (id INTEGER PRIMARY KEY, file_name TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(handy_db.UnknownSchemaError, match="dictation log"):
        handy_db.read_snapshot(db)


def test_missing_database(tmp_path: Path) -> None:
    with pytest.raises(handy_db.HandyDbError):
        handy_db.read_snapshot(tmp_path / "nope.db")


def test_audio_falls_back_to_recordings(handy: HandyFixture) -> None:
    entry = handy.add_dictation(100)
    cand = handy_db.read_snapshot(handy.paths.history_db).candidates[0]
    assert cand.entry_id == entry
    dataset_copy = handy.paths.dataset_audio_dir / "handy-100.wav"
    recordings_copy = handy.paths.recordings_dir / "handy-100.wav"
    recordings_copy.write_bytes(dataset_copy.read_bytes())
    dataset_copy.unlink()
    assert handy_db.find_audio(cand, handy.paths.app_data) == recordings_copy
    recordings_copy.unlink()
    assert handy_db.find_audio(cand, handy.paths.app_data) is None
