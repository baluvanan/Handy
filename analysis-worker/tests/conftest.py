"""Shared fixtures. Databases are built from ``fixtures/handy_schema.sql``,
which a Rust test regenerates from Handy's real migrations."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from handy_worker import audio
from handy_worker.paths import AppIdentity, Paths

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_SQL = FIXTURES / "handy_schema.sql"


def make_history_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> list[float]:
    n = int(seconds * audio.TARGET_RATE)
    return [amp * math.sin(2 * math.pi * freq * i / audio.TARGET_RATE) for i in range(n)]


class HandyFixture:
    """A fake Handy app-data folder with a real-schema history.db."""

    def __init__(self, root: Path) -> None:
        self.paths = Paths(
            identity=AppIdentity("com.example.handy-test", "Handy Test"),
            app_data=root / "app-data",
            log_dir=root / "logs",
        )
        make_history_db(self.paths.history_db)
        self.paths.dataset_audio_dir.mkdir(parents=True, exist_ok=True)
        self.paths.recordings_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.paths.history_db)

    def add_dictation(
        self,
        created_at: int,
        raw_text: str = "so I think we should ship it",
        *,
        seconds: float = 1.0,
        with_audio: bool = True,
        in_history: bool = False,
    ) -> int:
        file_name = f"handy-{created_at}.wav"
        audio_path = self.paths.dataset_audio_dir / file_name
        if with_audio:
            audio.write_wav(audio_path, tone(seconds))
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO dictation_log (created_at, audio_file, audio_path, model_id, raw_asr_text,"
            " rule_cleaned_text, final_text, speech_audio_ms, vad_segments, outcome)"
            " VALUES (?, ?, ?, 'parakeet-tdt-0.6b-v3', ?, ?, ?, ?, '[]', 'typed')",
            (
                created_at,
                file_name,
                str(audio_path),
                raw_text,
                raw_text,
                raw_text,
                int(seconds * 1000),
            ),
        )
        entry_id = int(cur.lastrowid or 0)
        if in_history:
            conn.execute(
                "INSERT INTO transcription_history (file_name, timestamp, saved, title,"
                " transcription_text) VALUES (?, ?, 0, 't', ?)",
                (file_name, created_at, raw_text),
            )
        conn.commit()
        conn.close()
        return entry_id


@pytest.fixture
def handy(tmp_path: Path) -> HandyFixture:
    return HandyFixture(tmp_path)


@pytest.fixture
def wav_factory(tmp_path: Path) -> Callable[[str, float], Path]:
    def make(name: str, seconds: float = 1.0) -> Path:
        p = tmp_path / name
        audio.write_wav(p, tone(seconds))
        return p

    return make
