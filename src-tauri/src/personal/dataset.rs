//! Raw transcript logging: one row per dictation with every intermediate
//! text, timing and context, as a foundation for fine-tuning and speech
//! analysis.
//!
//! Storage is a `dictation_log` table inside the existing `history.db`, with
//! its **own** versioned migrations (tracked in `handy_personal_schema`)
//! instead of upstream's `user_version` sequence, so upstream adding a
//! history migration can never collide with ours. Rows are independent of
//! history retention: the WAV is hard-linked (or copied) into
//! `<app data>/dataset/audio/`, so trimming History never deletes dataset
//! audio.
//!
//! Hooks: `CaptureSink` (speech timeline), `TranscriptionManager::complete`
//! (raw text), `actions.rs` (context at stop, record after paste),
//! `lib.rs`/`cli.rs` (`--export-dataset`).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tauri::AppHandle;

use super::timeline::{Segment, SpeechTimeline};
use crate::language_detection::LanguageDecision;

/// What the ASR stage produced before any cleanup.
#[derive(Clone, Debug, Default)]
pub struct AsrTrace {
    /// Model output before custom-word correction, filler removal, script
    /// conversion or normalization.
    pub raw_text: String,
    /// Time spent in that rule-based cleanup.
    pub cleanup_ms: u64,
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

/// Name under which our schema version is tracked.
const SCHEMA_NAME: &str = "dictation_log";

/// Applied in order; index + 1 is the schema version after each. Only ever
/// append to this list.
pub const MIGRATIONS: &[&str] = &[
    // v1: the log itself.
    "CREATE TABLE IF NOT EXISTS dictation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        audio_file TEXT,
        audio_path TEXT,
        model_id TEXT,
        language TEXT,
        raw_asr_text TEXT NOT NULL,
        rule_cleaned_text TEXT NOT NULL,
        llm_text TEXT,
        final_text TEXT NOT NULL,
        post_process_requested INTEGER NOT NULL DEFAULT 0,
        llm_fallback_reason TEXT,
        verbatim INTEGER NOT NULL DEFAULT 0,
        app_bundle_id TEXT,
        recording_ms INTEGER,
        speech_audio_ms INTEGER,
        vad_segments TEXT NOT NULL DEFAULT '[]',
        asr_ms INTEGER,
        cleanup_ms INTEGER,
        llm_ms INTEGER,
        paste_ms INTEGER,
        total_ms INTEGER,
        outcome TEXT
    );",
    // v2: date-range queries for export/analysis.
    "CREATE INDEX IF NOT EXISTS idx_dictation_log_created_at ON dictation_log(created_at);",
];

fn schema_version(conn: &Connection) -> Result<usize> {
    conn.execute(
        "CREATE TABLE IF NOT EXISTS handy_personal_schema (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        )",
        [],
    )?;
    let version: Option<i64> = conn
        .query_row(
            "SELECT version FROM handy_personal_schema WHERE name = ?1",
            [SCHEMA_NAME],
            |row| row.get(0),
        )
        .optional()?;
    Ok(version.unwrap_or(0).max(0) as usize)
}

/// Brings the dataset schema up to date, one transaction per step. Returns
/// the version reached. A database from a newer build (higher version) is
/// left alone: we only ever add columns/tables, and queries name columns.
pub fn migrate(conn: &mut Connection, migrations: &[&str]) -> Result<usize> {
    let mut version = schema_version(conn)?;
    while version < migrations.len() {
        let tx = conn.transaction()?;
        tx.execute_batch(migrations[version])
            .with_context(|| format!("dataset migration v{}", version + 1))?;
        version += 1;
        tx.execute(
            "INSERT INTO handy_personal_schema (name, version) VALUES (?1, ?2)
             ON CONFLICT(name) DO UPDATE SET version = excluded.version",
            params![SCHEMA_NAME, version as i64],
        )?;
        tx.commit()?;
        log::info!("Dataset schema migrated to v{version}");
    }
    Ok(version)
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

/// Per-stage latency, record → ASR → cleanup → LLM → paste.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Latency {
    /// Length of the recording itself (key down → key up, pre-VAD).
    pub recording_ms: Option<u64>,
    /// Model decoding (stop → raw text), excluding rule cleanup.
    pub asr_ms: Option<u64>,
    /// Rule-based cleanup (custom words, fillers, normalization).
    pub cleanup_ms: Option<u64>,
    /// LLM post-processing, including a timed-out attempt.
    pub llm_ms: Option<u64>,
    /// Pasting into the target app.
    pub paste_ms: Option<u64>,
    /// Stop → paste finished.
    pub total_ms: Option<u64>,
}

/// One dictation as stored and exported (one JSONL line).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct DictationRecord {
    pub id: i64,
    /// Unix seconds.
    pub created_at: i64,
    /// The matching History entry, while History still has it.
    pub history_id: Option<i64>,
    /// File name in History's recordings dir (`handy-<ts>.wav`).
    pub audio_file: Option<String>,
    /// Absolute path of the dataset's own copy/hard link of the WAV.
    pub audio_path: Option<String>,
    pub model_id: Option<String>,
    pub language: Option<String>,
    pub raw_asr_text: String,
    pub rule_cleaned_text: String,
    pub llm_text: Option<String>,
    pub final_text: String,
    pub post_process_requested: bool,
    /// Why the LLM result was not used (`timeout`, `unreachable`, ...).
    pub llm_fallback_reason: Option<String>,
    pub verbatim: bool,
    pub app_bundle_id: Option<String>,
    /// Kept (speech) audio length — what the WAV holds.
    pub speech_audio_ms: Option<u64>,
    /// Speech spans in recording time (pauses are the gaps).
    pub vad_segments: Vec<Segment>,
    pub latency: Latency,
    /// `typed`, `empty`, `failed` or `blocked`.
    pub outcome: Option<String>,
}

fn opt_u64(v: Option<i64>) -> Option<u64> {
    v.map(|v| v.max(0) as u64)
}

fn opt_i64(v: Option<u64>) -> Option<i64> {
    v.map(|v| v.min(i64::MAX as u64) as i64)
}

const RECORD_COLUMNS: &str = "d.id, d.created_at, d.audio_file, d.audio_path, d.model_id, \
    d.language, d.raw_asr_text, d.rule_cleaned_text, d.llm_text, d.final_text, \
    d.post_process_requested, d.llm_fallback_reason, d.verbatim, d.app_bundle_id, \
    d.recording_ms, d.speech_audio_ms, d.vad_segments, d.asr_ms, d.cleanup_ms, d.llm_ms, \
    d.paste_ms, d.total_ms, d.outcome";

fn map_record(
    row: &rusqlite::Row<'_>,
    history_id: Option<i64>,
) -> rusqlite::Result<DictationRecord> {
    let segments: String = row.get("vad_segments")?;
    Ok(DictationRecord {
        id: row.get("id")?,
        created_at: row.get("created_at")?,
        history_id,
        audio_file: row.get("audio_file")?,
        audio_path: row.get("audio_path")?,
        model_id: row.get("model_id")?,
        language: row.get("language")?,
        raw_asr_text: row.get("raw_asr_text")?,
        rule_cleaned_text: row.get("rule_cleaned_text")?,
        llm_text: row.get("llm_text")?,
        final_text: row.get("final_text")?,
        post_process_requested: row.get("post_process_requested")?,
        llm_fallback_reason: row.get("llm_fallback_reason")?,
        verbatim: row.get("verbatim")?,
        app_bundle_id: row.get("app_bundle_id")?,
        speech_audio_ms: opt_u64(row.get("speech_audio_ms")?),
        vad_segments: serde_json::from_str(&segments).unwrap_or_default(),
        latency: Latency {
            recording_ms: opt_u64(row.get("recording_ms")?),
            asr_ms: opt_u64(row.get("asr_ms")?),
            cleanup_ms: opt_u64(row.get("cleanup_ms")?),
            llm_ms: opt_u64(row.get("llm_ms")?),
            paste_ms: opt_u64(row.get("paste_ms")?),
            total_ms: opt_u64(row.get("total_ms")?),
        },
        outcome: row.get("outcome")?,
    })
}

/// The dataset table plus its audio directory.
pub struct DatasetStore {
    conn: Connection,
    audio_dir: PathBuf,
}

impl DatasetStore {
    /// Opens (and migrates) the dataset inside Handy's app data dir.
    pub fn open(app_data_dir: &Path) -> Result<Self> {
        Self::open_at(
            &app_data_dir.join("history.db"),
            app_data_dir.join("dataset").join("audio"),
        )
    }

    pub fn open_at(db_path: &Path, audio_dir: PathBuf) -> Result<Self> {
        let mut conn = Connection::open(db_path)?;
        migrate(&mut conn, MIGRATIONS)?;
        Ok(Self { conn, audio_dir })
    }

    pub fn audio_dir(&self) -> &Path {
        &self.audio_dir
    }

    /// Hard-links (or copies) a recording into the dataset's audio dir, so
    /// History retention cannot delete it. Returns the dataset path.
    pub fn retain_audio(&self, source: &Path) -> Result<PathBuf> {
        fs::create_dir_all(&self.audio_dir)?;
        let name = source
            .file_name()
            .context("recording path has no file name")?;
        let dest = self.audio_dir.join(name);
        if !dest.exists() && fs::hard_link(source, &dest).is_err() {
            fs::copy(source, &dest)?;
        }
        Ok(dest)
    }

    pub fn insert(&self, r: &DictationRecord) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO dictation_log (
                created_at, audio_file, audio_path, model_id, language,
                raw_asr_text, rule_cleaned_text, llm_text, final_text,
                post_process_requested, llm_fallback_reason, verbatim, app_bundle_id,
                recording_ms, speech_audio_ms, vad_segments,
                asr_ms, cleanup_ms, llm_ms, paste_ms, total_ms, outcome
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                      ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22)",
            params![
                r.created_at,
                r.audio_file,
                r.audio_path,
                r.model_id,
                r.language,
                r.raw_asr_text,
                r.rule_cleaned_text,
                r.llm_text,
                r.final_text,
                r.post_process_requested,
                r.llm_fallback_reason,
                r.verbatim,
                r.app_bundle_id,
                opt_i64(r.latency.recording_ms),
                opt_i64(r.speech_audio_ms),
                serde_json::to_string(&r.vad_segments)?,
                opt_i64(r.latency.asr_ms),
                opt_i64(r.latency.cleanup_ms),
                opt_i64(r.latency.llm_ms),
                opt_i64(r.latency.paste_ms),
                opt_i64(r.latency.total_ms),
                r.outcome,
            ],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    fn has_history_table(&self) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT COUNT(*) > 0 FROM sqlite_master WHERE type = 'table' AND name = 'transcription_history'",
            [],
            |row| row.get(0),
        )?)
    }

    /// Every record created at or after `since` (unix seconds), oldest first.
    pub fn records(&self, since: Option<i64>) -> Result<Vec<DictationRecord>> {
        let link_history = self.has_history_table()?;
        let sql = if link_history {
            format!(
                "SELECT {RECORD_COLUMNS}, \
                 (SELECT h.id FROM transcription_history h WHERE h.file_name = d.audio_file \
                  ORDER BY h.id DESC LIMIT 1) AS history_id \
                 FROM dictation_log d WHERE d.created_at >= ?1 ORDER BY d.created_at, d.id"
            )
        } else {
            format!(
                "SELECT {RECORD_COLUMNS}, NULL AS history_id \
                 FROM dictation_log d WHERE d.created_at >= ?1 ORDER BY d.created_at, d.id"
            )
        };
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map([since.unwrap_or(i64::MIN)], |row| {
            let history_id: Option<i64> = row.get("history_id")?;
            map_record(row, history_id)
        })?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    /// Distinct frontmost-app bundle ids, most recently used first.
    pub fn recent_app_ids(&self, limit: usize) -> Result<Vec<String>> {
        let mut stmt = self.conn.prepare(
            "SELECT app_bundle_id FROM dictation_log WHERE app_bundle_id IS NOT NULL
             GROUP BY app_bundle_id ORDER BY MAX(created_at) DESC LIMIT ?1",
        )?;
        let ids = stmt.query_map([limit as i64], |row| row.get(0))?;
        Ok(ids.collect::<rusqlite::Result<Vec<String>>>()?)
    }

    pub fn count(&self) -> Result<i64> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM dictation_log", [], |row| row.get(0))?)
    }
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

/// One exported line: the record plus whether its audio is still on disk.
#[derive(Serialize)]
struct ExportLine<'a> {
    #[serde(flatten)]
    record: &'a DictationRecord,
    audio_exists: bool,
}

/// Writes `dataset.jsonl` (one record per line) into `dir`, creating it.
/// Returns the file written and the number of lines.
pub fn export_jsonl(
    store: &DatasetStore,
    dir: &Path,
    since: Option<i64>,
) -> Result<(PathBuf, usize)> {
    use std::io::Write;
    fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    let path = dir.join("dataset.jsonl");
    let mut out = std::io::BufWriter::new(fs::File::create(&path)?);
    let records = store.records(since)?;
    for record in &records {
        let audio_exists = record
            .audio_path
            .as_deref()
            .is_some_and(|p| Path::new(p).exists());
        serde_json::to_writer(
            &mut out,
            &ExportLine {
                record,
                audio_exists,
            },
        )?;
        out.write_all(b"\n")?;
    }
    out.flush()?;
    Ok((path, records.len()))
}

// ---------------------------------------------------------------------------
// Capture (hooked from actions.rs / managers/audio.rs)
// ---------------------------------------------------------------------------

static LAST_TIMELINE: Mutex<Option<SpeechTimeline>> = Mutex::new(None);

/// Called when a recording stops (`AudioRecordingManager`).
pub fn set_last_timeline(timeline: SpeechTimeline) {
    *LAST_TIMELINE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(timeline);
}

fn take_last_timeline() -> Option<SpeechTimeline> {
    LAST_TIMELINE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .take()
}

/// Context captured synchronously when the user stops dictating.
#[derive(Clone, Debug)]
pub struct StopContext {
    pub stopped_at: Instant,
    /// Bundle id of the app that will receive the paste.
    pub app_bundle_id: Option<String>,
}

impl StopContext {
    pub fn now() -> Self {
        Self {
            stopped_at: Instant::now(),
            app_bundle_id: super::focus::frontmost_app_id(),
        }
    }
}

/// What the pipeline knows once text processing is done.
pub struct Processed<'a> {
    /// Recording file name, when the WAV was saved.
    pub audio_file: Option<String>,
    pub sample_count: usize,
    pub trace: &'a AsrTrace,
    pub rule_cleaned_text: &'a str,
    pub llm_text: Option<&'a str>,
    pub llm_fallback_reason: Option<&'a str>,
    pub final_text: &'a str,
    pub language: Option<&'a LanguageDecision>,
    pub post_process_requested: bool,
    /// Wall time of `transcribe_recording` (decode + rule cleanup).
    pub transcribe_elapsed: Duration,
    /// Wall time of the post-processing step.
    pub llm_elapsed: Option<Duration>,
}

/// A record waiting for the paste to finish.
pub struct PendingDictation {
    record: DictationRecord,
    stopped_at: Instant,
}

/// Builds the pending record, or `None` when logging is off.
pub fn capture(app: &AppHandle, ctx: &StopContext, p: Processed<'_>) -> Option<PendingDictation> {
    let settings = crate::settings::get_settings(app);
    if !settings.personal.dataset_logging_enabled {
        return None;
    }
    let timeline = take_last_timeline();
    let cleanup_ms = p.trace.cleanup_ms;
    let record = DictationRecord {
        created_at: chrono::Utc::now().timestamp(),
        audio_file: p.audio_file,
        model_id: Some(settings.selected_model.clone()).filter(|m| !m.is_empty()),
        language: p.language.and_then(|l| l.language.clone()),
        raw_asr_text: p.trace.raw_text.clone(),
        rule_cleaned_text: p.rule_cleaned_text.to_string(),
        llm_text: p.llm_text.map(str::to_string),
        final_text: p.final_text.to_string(),
        post_process_requested: p.post_process_requested,
        llm_fallback_reason: p.llm_fallback_reason.map(str::to_string),
        verbatim: settings.personal.verbatim_mode,
        app_bundle_id: ctx.app_bundle_id.clone(),
        speech_audio_ms: Some(p.sample_count as u64 / 16),
        vad_segments: timeline.as_ref().map(|t| t.segments()).unwrap_or_default(),
        latency: Latency {
            recording_ms: timeline.as_ref().map(|t| t.total_ms()),
            asr_ms: Some((p.transcribe_elapsed.as_millis() as u64).saturating_sub(cleanup_ms)),
            cleanup_ms: Some(cleanup_ms),
            llm_ms: p.llm_elapsed.map(|d| d.as_millis() as u64),
            ..Default::default()
        },
        ..Default::default()
    };
    Some(PendingDictation {
        record,
        stopped_at: ctx.stopped_at,
    })
}

impl PendingDictation {
    /// Completes and stores the record. Never fails the dictation: errors are
    /// logged only.
    pub fn finish(mut self, app: &AppHandle, outcome: &str, paste: Option<Duration>) {
        self.record.outcome = Some(outcome.to_string());
        self.record.latency.paste_ms = paste.map(|d| d.as_millis() as u64);
        self.record.latency.total_ms = Some(self.stopped_at.elapsed().as_millis() as u64);
        if let Err(e) = self.store(app) {
            log::warn!("Dataset logging failed: {e:#}");
        }
    }

    fn store(mut self, app: &AppHandle) -> Result<()> {
        let app_data_dir = crate::portable::app_data_dir(app)?;
        let store = DatasetStore::open(&app_data_dir)?;
        if let Some(file) = &self.record.audio_file {
            let source = app_data_dir.join("recordings").join(file);
            match store.retain_audio(&source) {
                Ok(path) => self.record.audio_path = Some(path.to_string_lossy().into_owned()),
                Err(e) => log::warn!("Dataset: could not keep audio {file}: {e:#}"),
            }
        }
        let id = store.insert(&self.record)?;
        log::debug!("Dataset: logged dictation {id}");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn memory_store(dir: &Path) -> DatasetStore {
        DatasetStore::open_at(&dir.join("history.db"), dir.join("audio")).unwrap()
    }

    fn sample_record(created_at: i64, text: &str) -> DictationRecord {
        DictationRecord {
            created_at,
            audio_file: Some(format!("handy-{created_at}.wav")),
            model_id: Some("whisper-small".into()),
            raw_asr_text: format!("um {text}"),
            rule_cleaned_text: text.into(),
            final_text: text.into(),
            vad_segments: vec![
                Segment {
                    start_ms: 0,
                    end_ms: 900,
                },
                Segment {
                    start_ms: 1500,
                    end_ms: 2400,
                },
            ],
            latency: Latency {
                recording_ms: Some(2500),
                asr_ms: Some(300),
                cleanup_ms: Some(2),
                paste_ms: Some(40),
                ..Default::default()
            },
            outcome: Some("typed".into()),
            ..Default::default()
        }
    }

    #[test]
    fn fresh_database_migrates_to_latest() {
        let mut conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate(&mut conn, MIGRATIONS).unwrap(), MIGRATIONS.len());
        // Idempotent.
        assert_eq!(migrate(&mut conn, MIGRATIONS).unwrap(), MIGRATIONS.len());
        let has_index: bool = conn
            .query_row(
                "SELECT COUNT(*) > 0 FROM sqlite_master WHERE name = 'idx_dictation_log_created_at'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(has_index);
    }

    #[test]
    fn upgrading_from_v1_keeps_rows() {
        let mut conn = Connection::open_in_memory().unwrap();
        assert_eq!(migrate(&mut conn, &MIGRATIONS[..1]).unwrap(), 1);
        conn.execute(
            "INSERT INTO dictation_log (created_at, raw_asr_text, rule_cleaned_text, final_text)
             VALUES (1, 'a', 'a', 'a')",
            [],
        )
        .unwrap();
        assert_eq!(migrate(&mut conn, MIGRATIONS).unwrap(), 2);
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM dictation_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn a_newer_schema_is_left_alone() {
        let mut conn = Connection::open_in_memory().unwrap();
        migrate(&mut conn, MIGRATIONS).unwrap();
        conn.execute(
            "UPDATE handy_personal_schema SET version = 99 WHERE name = ?1",
            [SCHEMA_NAME],
        )
        .unwrap();
        assert_eq!(migrate(&mut conn, MIGRATIONS).unwrap(), 99);
    }

    #[test]
    fn coexists_with_upstream_history_and_its_user_version() {
        // Upstream tracks its own migrations in user_version; ours must not
        // touch it, or released upstream builds would refuse the database.
        let mut conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE transcription_history (id INTEGER PRIMARY KEY, file_name TEXT NOT NULL);
             PRAGMA user_version = 4;",
        )
        .unwrap();
        migrate(&mut conn, MIGRATIONS).unwrap();
        let v: i32 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap();
        assert_eq!(v, 4);
    }

    #[test]
    fn insert_round_trips_every_field_and_links_history() {
        let dir = tempfile::tempdir().unwrap();
        let store = memory_store(dir.path());
        store
            .conn
            .execute_batch(
                "CREATE TABLE transcription_history (id INTEGER PRIMARY KEY, file_name TEXT NOT NULL);
                 INSERT INTO transcription_history (id, file_name) VALUES (7, 'handy-100.wav');",
            )
            .unwrap();
        let mut record = sample_record(100, "hello there");
        record.llm_text = Some("Hello there.".into());
        record.llm_fallback_reason = None;
        record.app_bundle_id = Some("com.tinyspeck.slackmacgap".into());
        record.verbatim = true;
        record.latency.llm_ms = Some(800);
        record.latency.total_ms = Some(1200);
        record.speech_audio_ms = Some(1800);
        let id = store.insert(&record).unwrap();

        let got = store.records(None).unwrap();
        assert_eq!(got.len(), 1);
        let mut expected = record;
        expected.id = id;
        expected.history_id = Some(7);
        assert_eq!(got[0], expected);
    }

    #[test]
    fn recent_app_ids_are_distinct_and_newest_first() {
        let dir = tempfile::tempdir().unwrap();
        let store = memory_store(dir.path());
        for (ts, app) in [(1, "com.a"), (2, "com.b"), (3, "com.a")] {
            let mut r = sample_record(ts, "x");
            r.app_bundle_id = Some(app.into());
            store.insert(&r).unwrap();
        }
        store.insert(&sample_record(4, "no app")).unwrap();
        assert_eq!(store.recent_app_ids(10).unwrap(), vec!["com.a", "com.b"]);
    }

    #[test]
    fn records_filter_by_since() {
        let dir = tempfile::tempdir().unwrap();
        let store = memory_store(dir.path());
        store.insert(&sample_record(100, "old")).unwrap();
        store.insert(&sample_record(200, "new")).unwrap();
        let got = store.records(Some(150)).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].final_text, "new");
        assert_eq!(store.count().unwrap(), 2);
    }

    #[test]
    fn export_writes_one_json_line_per_record() {
        let dir = tempfile::tempdir().unwrap();
        let store = memory_store(dir.path());
        // Real audio for the first record, kept via retain_audio.
        let recordings = dir.path().join("recordings");
        fs::create_dir_all(&recordings).unwrap();
        let wav = recordings.join("handy-100.wav");
        fs::write(&wav, b"RIFF....").unwrap();
        let mut first = sample_record(100, "first line");
        first.audio_path = Some(store.retain_audio(&wav).unwrap().to_string_lossy().into());
        store.insert(&first).unwrap();
        store
            .insert(&sample_record(200, "second \"quoted\"\nline"))
            .unwrap();

        let out_dir = dir.path().join("export");
        let (path, n) = export_jsonl(&store, &out_dir, None).unwrap();
        assert_eq!(n, 2);
        let text = fs::read_to_string(path).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 2);

        let v: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v["raw_asr_text"], "um first line");
        assert_eq!(v["final_text"], "first line");
        assert_eq!(v["model_id"], "whisper-small");
        assert_eq!(v["audio_exists"], true);
        assert!(v["audio_path"].as_str().unwrap().ends_with("handy-100.wav"));
        assert_eq!(v["vad_segments"][1]["start_ms"], 1500);
        assert_eq!(v["latency"]["asr_ms"], 300);
        assert_eq!(v["outcome"], "typed");

        let v: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v["final_text"], "second \"quoted\"\nline");
        assert_eq!(v["audio_exists"], false);

        // A record deserializes back from its line (training scripts can
        // rely on the schema).
        let back: DictationRecord = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(back.final_text, "second \"quoted\"\nline");
    }

    #[test]
    fn retained_audio_survives_history_deleting_its_wav() {
        let dir = tempfile::tempdir().unwrap();
        let store = memory_store(dir.path());
        let wav = dir.path().join("handy-1.wav");
        fs::write(&wav, b"RIFF").unwrap();
        let kept = store.retain_audio(&wav).unwrap();
        fs::remove_file(&wav).unwrap();
        assert_eq!(fs::read(kept).unwrap(), b"RIFF");
    }
}
