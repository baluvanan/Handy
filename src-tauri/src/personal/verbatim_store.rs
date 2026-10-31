//! Read-only view of the worker's results (`<app data>/verbatim/verbatim.db`).
//!
//! The worker owns that file; Handy only ever opens it with `mode=ro`. Its
//! tables are the contract between the two (see the worker's
//! `results_db.py`). A schema version Handy doesn't know is treated as "no
//! verbatim data", never guessed at.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::Deserialize;

/// `results_db.SCHEMA_VERSION` versions this Handy can read.
pub const KNOWN_SCHEMA_VERSIONS: &[i64] = &[1];

pub fn db_path(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("verbatim").join("verbatim.db")
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct VerbatimWord {
    #[serde(rename = "w")]
    pub text: String,
    /// Seconds into the recording.
    #[serde(rename = "s")]
    pub start: f64,
    #[serde(rename = "e")]
    pub end: f64,
    #[serde(rename = "p", default)]
    pub confidence: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct VerbatimTranscript {
    pub entry_id: i64,
    pub text: String,
    pub words: Vec<VerbatimWord>,
    pub model_id: String,
    pub model_revision: Option<String>,
    pub processed_at: i64,
    pub verified: bool,
    pub audio_duration_s: f64,
}

impl VerbatimTranscript {
    /// Identifies the transcript's source, for discontinuity detection.
    pub fn source_key(&self) -> String {
        format!(
            "{}@{}",
            self.model_id,
            self.model_revision.as_deref().unwrap_or("?")
        )
    }
}

/// All verbatim transcripts by dictation-log entry id.
#[derive(Clone, Debug, Default)]
pub struct VerbatimIndex {
    pub by_entry: HashMap<i64, VerbatimTranscript>,
}

impl VerbatimIndex {
    pub fn get(&self, entry_id: i64) -> Option<&VerbatimTranscript> {
        self.by_entry.get(&entry_id)
    }
}

fn open_ro(path: &Path) -> Result<Option<Connection>> {
    if !path.exists() {
        return Ok(None);
    }
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    conn.busy_timeout(std::time::Duration::from_secs(2))?;
    let version: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if !KNOWN_SCHEMA_VERSIONS.contains(&version) {
        log::warn!(
            "verbatim.db schema v{version} is unknown to this Handy; ignoring verbatim data"
        );
        return Ok(None);
    }
    Ok(Some(conn))
}

/// Loads every transcript. Missing or unknown database → empty index.
pub fn load(app_data_dir: &Path) -> Result<VerbatimIndex> {
    let Some(conn) = open_ro(&db_path(app_data_dir))? else {
        return Ok(VerbatimIndex::default());
    };
    let mut stmt = conn.prepare(
        "SELECT entry_id, text, words, model_id, model_revision, processed_at, verified,
                audio_duration_s
         FROM transcripts",
    )?;
    let rows = stmt.query_map([], |r| {
        let words: String = r.get(2)?;
        Ok(VerbatimTranscript {
            entry_id: r.get(0)?,
            text: r.get(1)?,
            words: serde_json::from_str(&words).unwrap_or_default(),
            model_id: r.get(3)?,
            model_revision: r.get(4)?,
            processed_at: r.get(5)?,
            verified: r.get::<_, i64>(6)? != 0,
            audio_duration_s: r.get(7)?,
        })
    })?;
    let mut index = VerbatimIndex::default();
    for t in rows {
        let t = t?;
        index.by_entry.insert(t.entry_id, t);
    }
    Ok(index)
}

/// Whether the worker has a **verified** verbatim transcript for the entry
/// and considers it done. The only condition under which Handy's retention
/// may delete that entry's audio.
pub fn verified_done(app_data_dir: &Path, entry_id: i64) -> Result<bool> {
    let Some(conn) = open_ro(&db_path(app_data_dir))? else {
        return Ok(false);
    };
    let row: Option<i64> = conn
        .query_row(
            "SELECT t.verified FROM transcripts t
             JOIN entries e ON e.entry_id = t.entry_id
             WHERE t.entry_id = ?1 AND e.status = 'done'",
            [entry_id],
            |r| r.get(0),
        )
        .optional()?;
    Ok(row == Some(1))
}

/// Entry ids with a verified transcript (for previews; re-checked per entry
/// right before any deletion).
pub fn verified_done_ids(app_data_dir: &Path) -> Result<std::collections::HashSet<i64>> {
    let Some(conn) = open_ro(&db_path(app_data_dir))? else {
        return Ok(Default::default());
    };
    let mut stmt = conn.prepare(
        "SELECT t.entry_id FROM transcripts t JOIN entries e ON e.entry_id = t.entry_id
         WHERE t.verified = 1 AND e.status = 'done'",
    )?;
    let ids = stmt.query_map([], |r| r.get(0))?;
    Ok(ids.collect::<rusqlite::Result<_>>()?)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// The worker's v1 schema, as `results_db.py` creates it.
    pub const WORKER_SCHEMA_V1: &str = "
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, history_id INTEGER, audio_file TEXT,
            created_at INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, skip_reason TEXT, updated_at INTEGER NOT NULL);
        CREATE TABLE transcripts (entry_id INTEGER PRIMARY KEY, history_id INTEGER,
            audio_file TEXT, text TEXT NOT NULL, words TEXT NOT NULL, language TEXT,
            model_name TEXT NOT NULL, model_id TEXT NOT NULL, model_revision TEXT, device TEXT,
            processed_at INTEGER NOT NULL, audio_duration_s REAL NOT NULL,
            processing_s REAL NOT NULL, audio_sha256 TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0, verify_error TEXT, worker_version TEXT NOT NULL);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL,
            started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT);
        PRAGMA user_version = 1;";

    /// Writes a fake worker DB with `(entry_id, text, status, verified)`.
    pub fn fake_worker_db(app_data_dir: &Path, rows: &[(i64, &str, &str, bool)]) {
        let path = db_path(app_data_dir);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(WORKER_SCHEMA_V1).unwrap();
        for (id, text, status, verified) in rows {
            conn.execute(
                "INSERT INTO entries (entry_id, created_at, status, updated_at) VALUES (?1, 0, ?2, 0)",
                rusqlite::params![id, status],
            )
            .unwrap();
            if *status == "done" {
                let words: Vec<String> = text
                    .split_whitespace()
                    .enumerate()
                    .map(|(i, w)| {
                        format!(
                            r#"{{"w":"{w}","s":{:.2},"e":{:.2},"p":0.9}}"#,
                            i as f64 * 0.4,
                            i as f64 * 0.4 + 0.3
                        )
                    })
                    .collect();
                conn.execute(
                    "INSERT INTO transcripts (entry_id, text, words, model_name, model_id,
                        model_revision, processed_at, audio_duration_s, processing_s, audio_sha256,
                        verified, worker_version)
                     VALUES (?1, ?2, ?3, 'crisperwhisper', 'nyrahealth/CrisperWhisper', 'rev1',
                        100, 5.0, 1.0, 'sha', ?4, '0.2.0')",
                    rusqlite::params![id, text, format!("[{}]", words.join(",")), *verified as i64],
                )
                .unwrap();
            }
        }
    }

    #[test]
    fn loads_transcripts_and_verification_read_only() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load(dir.path()).unwrap().by_entry.is_empty());
        fake_worker_db(
            dir.path(),
            &[
                (1, "um so I think", "done", true),
                (2, "uh", "done", false),
                (3, "", "pending", false),
            ],
        );
        let before = std::fs::read(db_path(dir.path())).unwrap();
        let index = load(dir.path()).unwrap();
        assert_eq!(index.by_entry.len(), 2);
        let t = index.get(1).unwrap();
        assert_eq!(t.words.len(), 4);
        assert_eq!(t.words[0].text, "um");
        assert_eq!(t.source_key(), "nyrahealth/CrisperWhisper@rev1");
        assert!(verified_done(dir.path(), 1).unwrap());
        assert!(!verified_done(dir.path(), 2).unwrap());
        assert!(!verified_done(dir.path(), 3).unwrap());
        assert!(!verified_done(dir.path(), 99).unwrap());
        assert_eq!(
            verified_done_ids(dir.path()).unwrap(),
            [1].into_iter().collect()
        );
        assert_eq!(std::fs::read(db_path(dir.path())).unwrap(), before);
    }

    #[test]
    fn an_unknown_worker_schema_is_ignored() {
        let dir = tempfile::tempdir().unwrap();
        fake_worker_db(dir.path(), &[(1, "um", "done", true)]);
        let conn = Connection::open(db_path(dir.path())).unwrap();
        conn.pragma_update(None, "user_version", 99).unwrap();
        drop(conn);
        assert!(load(dir.path()).unwrap().by_entry.is_empty());
        assert!(!verified_done(dir.path(), 1).unwrap());
    }
}
