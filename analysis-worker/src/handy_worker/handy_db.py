"""Read-only access to Handy's ``history.db``.

Rules (non-negotiable #1 and #2):

- The database is opened with a read-only URI (``mode=ro``) plus
  ``PRAGMA query_only``, so no statement can write, migrate or delete.
- A busy timeout makes a read wait briefly behind Handy's writes instead of
  failing, and the connection runs in autocommit mode, so no read transaction
  is ever held open between statements.
- Callers fetch what they need and **close the connection before any
  transcription starts** (``read_snapshot`` does exactly that).

What the worker processes: rows of the fork's ``dictation_log`` table (one
per dictation, with its audio hard-linked into ``dataset/audio/``). Upstream's
``transcription_history`` rows are pruned by History retention (default:
keep the last 5), so keying on them would lose almost every recording; the
dictation log is never pruned. The matching History id is looked up by file
name when History still has it.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: Upstream History schema (``PRAGMA user_version``) versions this worker
#: has been tested against. A newer one stops the worker until it is updated.
MAX_KNOWN_HISTORY_VERSION = 4
#: The fork's ``dictation_log`` schema (``handy_personal_schema``).
MAX_KNOWN_DICTATION_LOG_VERSION = 2

REQUIRED_DICTATION_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "audio_file",
        "audio_path",
        "model_id",
        "raw_asr_text",
        "speech_audio_ms",
        "vad_segments",
    }
)
REQUIRED_HISTORY_COLUMNS = frozenset({"id", "file_name"})

DEFAULT_BUSY_TIMEOUT_MS = 2000


class HandyDbError(RuntimeError):
    """history.db is missing or cannot be read."""


class UnknownSchemaError(HandyDbError):
    """history.db has a schema this worker does not know. Never guess."""


@dataclass(frozen=True)
class SchemaInfo:
    history_version: int
    dictation_log_version: int
    history_columns: frozenset[str]
    dictation_columns: frozenset[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "history_user_version": self.history_version,
            "dictation_log_version": self.dictation_log_version,
            "max_known_history_version": MAX_KNOWN_HISTORY_VERSION,
            "max_known_dictation_log_version": MAX_KNOWN_DICTATION_LOG_VERSION,
        }


@dataclass(frozen=True)
class Candidate:
    """One dictation that may need a verbatim transcript."""

    entry_id: int
    created_at: int
    audio_file: str | None
    audio_path: str | None
    model_id: str | None
    raw_asr_text: str
    speech_audio_ms: int | None
    history_id: int | None = None


@dataclass(frozen=True)
class Snapshot:
    schema: SchemaInfo
    candidates: list[Candidate]


def ro_uri(path: Path) -> str:
    """``file:`` URI that opens ``path`` read-only."""
    quoted = urllib.parse.quote(str(path.resolve()))
    return f"file:{quoted}?mode=ro"


@contextmanager
def connect_ro(
    path: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """A read-only, autocommit connection that is always closed on exit."""
    if not path.exists():
        raise HandyDbError(f"Handy's database was not found at {path}")
    try:
        conn = sqlite3.connect(
            ro_uri(path),
            uri=True,
            timeout=busy_timeout_ms / 1000,
            isolation_level=None,  # autocommit: no implicit transactions
            check_same_thread=False,
        )
    except sqlite3.Error as e:
        raise HandyDbError(f"Cannot open {path} read-only: {e}") from e
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA query_only = 1")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    return frozenset(str(r[0]) for r in rows)


def read_schema(conn: sqlite3.Connection) -> SchemaInfo:
    history_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    has_personal = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='handy_personal_schema'"
    ).fetchone()[0]
    dictation_version = 0
    if has_personal:
        row = conn.execute(
            "SELECT version FROM handy_personal_schema WHERE name = 'dictation_log'"
        ).fetchone()
        dictation_version = int(row[0]) if row else 0
    return SchemaInfo(
        history_version=history_version,
        dictation_log_version=dictation_version,
        history_columns=_columns(conn, "transcription_history"),
        dictation_columns=_columns(conn, "dictation_log"),
    )


def check_schema(schema: SchemaInfo) -> None:
    """Raises ``UnknownSchemaError`` unless this worker knows the schema."""
    problems: list[str] = []
    if schema.history_version > MAX_KNOWN_HISTORY_VERSION:
        problems.append(
            f"History schema v{schema.history_version} is newer than the "
            f"v{MAX_KNOWN_HISTORY_VERSION} this worker knows"
        )
    if schema.dictation_log_version == 0 or not schema.dictation_columns:
        problems.append("No dictation log yet (turn on dataset logging in Handy and dictate once)")
    elif schema.dictation_log_version > MAX_KNOWN_DICTATION_LOG_VERSION:
        problems.append(
            f"Dictation log schema v{schema.dictation_log_version} is newer than the "
            f"v{MAX_KNOWN_DICTATION_LOG_VERSION} this worker knows"
        )
    else:
        missing = REQUIRED_DICTATION_COLUMNS - schema.dictation_columns
        if missing:
            problems.append(f"dictation_log lacks columns: {', '.join(sorted(missing))}")
    if schema.history_columns and REQUIRED_HISTORY_COLUMNS - schema.history_columns:
        problems.append("transcription_history lacks its id/file_name columns")
    if problems:
        raise UnknownSchemaError(
            "; ".join(problems) + ". Update the worker (pull the fork and re-sync) "
            "before it reads this database."
        )


def _fetch(conn: sqlite3.Connection, schema: SchemaInfo, limit: int | None) -> list[Candidate]:
    link = (
        "(SELECT h.id FROM transcription_history h WHERE h.file_name = d.audio_file "
        "ORDER BY h.id DESC LIMIT 1)"
        if schema.history_columns
        else "NULL"
    )
    sql = (
        "SELECT d.id, d.created_at, d.audio_file, d.audio_path, d.model_id, "
        f"d.raw_asr_text, d.speech_audio_ms, {link} AS history_id "
        "FROM dictation_log d "
        "WHERE d.audio_file IS NOT NULL OR d.audio_path IS NOT NULL "
        "ORDER BY d.created_at DESC, d.id DESC"
    )
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    rows = conn.execute(sql, params).fetchall()
    return [
        Candidate(
            entry_id=int(r["id"]),
            created_at=int(r["created_at"]),
            audio_file=r["audio_file"],
            audio_path=r["audio_path"],
            model_id=r["model_id"],
            raw_asr_text=r["raw_asr_text"] or "",
            speech_audio_ms=r["speech_audio_ms"],
            history_id=r["history_id"],
        )
        for r in rows
    ]


def read_snapshot(
    path: Path,
    limit: int | None = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Snapshot:
    """Checks the schema and lists every dictation with audio, newest first.
    The connection is closed before this returns."""
    with connect_ro(path, busy_timeout_ms) as conn:
        schema = read_schema(conn)
        check_schema(schema)
        candidates = _fetch(conn, schema, limit)
    return Snapshot(schema=schema, candidates=candidates)


def read_schema_only(path: Path) -> SchemaInfo:
    with connect_ro(path) as conn:
        return read_schema(conn)


def entry_exists(path: Path, entry_id: int) -> bool:
    """Whether the dictation still exists (short read-only check)."""
    with connect_ro(path) as conn:
        row = conn.execute("SELECT 1 FROM dictation_log WHERE id = ?", (entry_id,)).fetchone()
        return row is not None


def audio_locations(candidate: Candidate, app_data: Path) -> list[Path]:
    """Where the entry's audio may be, best first: the dataset's own copy
    (survives History retention), then History's recordings folder."""
    paths: list[Path] = []
    if candidate.audio_path:
        paths.append(Path(candidate.audio_path))
    if candidate.audio_file:
        paths.append(app_data / "dataset" / "audio" / candidate.audio_file)
        paths.append(app_data / "recordings" / candidate.audio_file)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def find_audio(candidate: Candidate, app_data: Path) -> Path | None:
    for p in audio_locations(candidate, app_data):
        if p.is_file():
            return p
    return None
