//! Audio retention for the dictation archive, owned by Handy (never the
//! worker): compress old recordings losslessly, and delete very old audio
//! only once a verified verbatim transcript exists.
//!
//! Safety rules (non-negotiable: no data is ever lost):
//! - Off by default; the UI shows [`preview`] before it can be enabled.
//! - **Compression is lossless** (16-bit FLAC). The FLAC is written aside,
//!   decoded again and compared sample-for-sample with the WAV before Handy's
//!   records are switched to it, and only then is the WAV removed.
//! - **Deletion** requires: older than the delete threshold, a verbatim
//!   transcript the worker marked *verified* and *done* (re-checked right
//!   before deleting), and no History entry still showing the recording.
//!   Unprocessed or unverified audio is never deleted.
//! - Handy's records (`dictation_log.audio_file/audio_path`,
//!   `transcription_history.file_name`) are updated in one transaction, so
//!   playback and re-transcription keep working. A crash at any point leaves
//!   either the old file and records, or the new file and records (plus at
//!   worst a stray extra copy), never a record pointing at nothing.
//! - Never runs while the worker is mid-run or Handy is dictating.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use specta::Type;

use super::verbatim_store;

const DAY: i64 = 86_400;

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Type)]
#[serde(default)]
pub struct RetentionSettings {
    pub enabled: bool,
    /// Compress WAVs older than this (lossless FLAC). 0 = never.
    pub compress_after_days: u32,
    /// Delete audio older than this, only with a verified verbatim
    /// transcript. 0 = never.
    pub delete_after_days: u32,
}

impl Default for RetentionSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            compress_after_days: 30,
            delete_after_days: 180,
        }
    }
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Type)]
pub struct RetentionItem {
    pub entry_id: i64,
    pub created_at: i64,
    pub file: String,
    pub bytes: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Type)]
pub struct RetentionPreview {
    pub compress: Vec<RetentionItem>,
    pub delete: Vec<RetentionItem>,
    /// Old enough to delete, but kept: no verbatim transcript yet.
    pub kept_unprocessed: u32,
    /// Old enough to delete, but kept: the transcript did not verify.
    pub kept_unverified: u32,
    /// Old enough to delete, but kept: History still shows the recording.
    pub kept_in_history: u32,
    pub bytes_to_compress: f64,
    /// Rough: FLAC typically halves speech WAVs.
    pub estimated_bytes_saved: f64,
    pub bytes_to_delete: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Type)]
pub struct RetentionReport {
    pub compressed: u32,
    pub deleted: u32,
    pub failed: u32,
    pub bytes_freed: f64,
    pub errors: Vec<String>,
    /// History entries whose file name changed (for UI refresh).
    #[serde(skip)]
    pub history_ids_changed: Vec<i64>,
}

#[derive(Debug, Clone)]
struct Row {
    entry_id: i64,
    created_at: i64,
    audio_file: Option<String>,
    audio_path: Option<String>,
}

struct Paths {
    history_db: PathBuf,
    recordings: PathBuf,
    dataset_audio: PathBuf,
}

impl Paths {
    fn new(app_data_dir: &Path) -> Self {
        Self {
            history_db: app_data_dir.join("history.db"),
            recordings: app_data_dir.join("recordings"),
            dataset_audio: app_data_dir.join("dataset").join("audio"),
        }
    }

    /// The entry's own audio file (dataset copy first, then History's).
    fn locate(&self, row: &Row) -> Option<PathBuf> {
        let mut candidates = Vec::new();
        if let Some(p) = &row.audio_path {
            candidates.push(PathBuf::from(p));
        }
        if let Some(f) = &row.audio_file {
            candidates.push(self.dataset_audio.join(f));
            candidates.push(self.recordings.join(f));
        }
        candidates.into_iter().find(|p| p.is_file())
    }
}

fn open_history(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    Ok(conn)
}

fn load_rows(conn: &Connection) -> Result<Vec<Row>> {
    let mut stmt = conn.prepare(
        "SELECT id, created_at, audio_file, audio_path FROM dictation_log
         WHERE audio_file IS NOT NULL OR audio_path IS NOT NULL",
    )?;
    let rows = stmt.query_map([], |r| {
        Ok(Row {
            entry_id: r.get(0)?,
            created_at: r.get(1)?,
            audio_file: r.get(2)?,
            audio_path: r.get(3)?,
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn history_files(conn: &Connection) -> Result<HashMap<String, i64>> {
    let mut stmt = conn.prepare("SELECT file_name, id FROM transcription_history")?;
    let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn file_name(p: &Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn is_wav(p: &Path) -> bool {
    p.extension().is_some_and(|e| e.eq_ignore_ascii_case("wav"))
}

#[derive(Debug)]
enum Action {
    Compress(PathBuf),
    Delete(PathBuf),
}

struct Plan {
    actions: Vec<(Row, Action)>,
    preview: RetentionPreview,
}

fn make_plan(
    app_data_dir: &Path,
    settings: &RetentionSettings,
    now: i64,
    verified: &HashSet<i64>,
    transcribed: &HashSet<i64>,
) -> Result<Plan> {
    let paths = Paths::new(app_data_dir);
    let conn = open_history(&paths.history_db)?;
    let rows = load_rows(&conn)?;
    let in_history = history_files(&conn)?;
    drop(conn);

    let mut plan = Plan {
        actions: Vec::new(),
        preview: RetentionPreview::default(),
    };
    for row in rows {
        let Some(path) = paths.locate(&row) else {
            continue;
        };
        let age = now - row.created_at;
        let bytes = fs::metadata(&path).map(|m| m.len() as f64).unwrap_or(0.0);
        let item = RetentionItem {
            entry_id: row.entry_id,
            created_at: row.created_at,
            file: file_name(&path),
            bytes,
        };
        let delete_due =
            settings.delete_after_days > 0 && age >= i64::from(settings.delete_after_days) * DAY;
        if delete_due {
            let shown_in_history = row
                .audio_file
                .as_ref()
                .is_some_and(|f| in_history.contains_key(f));
            if !transcribed.contains(&row.entry_id) {
                plan.preview.kept_unprocessed += 1;
            } else if !verified.contains(&row.entry_id) {
                plan.preview.kept_unverified += 1;
            } else if shown_in_history {
                plan.preview.kept_in_history += 1;
            } else {
                plan.preview.bytes_to_delete += bytes;
                plan.preview.delete.push(item);
                plan.actions.push((row, Action::Delete(path)));
                continue;
            }
        }
        let compress_due = settings.compress_after_days > 0
            && age >= i64::from(settings.compress_after_days) * DAY
            && is_wav(&path);
        if compress_due {
            plan.preview.bytes_to_compress += bytes;
            plan.preview.compress.push(item);
            plan.actions.push((row, Action::Compress(path)));
        }
    }
    plan.preview.estimated_bytes_saved = plan.preview.bytes_to_compress * 0.5;
    Ok(plan)
}

/// What enabling (or the next pass) would do. Reads only.
pub fn preview(
    app_data_dir: &Path,
    settings: &RetentionSettings,
    now: i64,
) -> Result<RetentionPreview> {
    let verified = verbatim_store::verified_done_ids(app_data_dir)?;
    let transcribed: HashSet<i64> = verbatim_store::load(app_data_dir)?
        .by_entry
        .keys()
        .copied()
        .collect();
    Ok(make_plan(app_data_dir, settings, now, &verified, &transcribed)?.preview)
}

/// Runs one retention pass. `keep_going` is checked before every file so
/// a recording starting mid-pass stops it.
pub fn apply(
    app_data_dir: &Path,
    settings: &RetentionSettings,
    now: i64,
    keep_going: &dyn Fn() -> bool,
) -> Result<RetentionReport> {
    if !settings.enabled {
        return Ok(RetentionReport::default());
    }
    let verified = verbatim_store::verified_done_ids(app_data_dir)?;
    let transcribed: HashSet<i64> = verbatim_store::load(app_data_dir)?
        .by_entry
        .keys()
        .copied()
        .collect();
    let plan = make_plan(app_data_dir, settings, now, &verified, &transcribed)?;
    let paths = Paths::new(app_data_dir);
    let mut report = RetentionReport::default();
    for (row, action) in plan.actions {
        if !keep_going() {
            break;
        }
        let result = match &action {
            Action::Compress(path) => compress_entry(&paths, &row, path, &mut report),
            Action::Delete(path) => delete_entry(app_data_dir, &paths, &row, path, &mut report),
        };
        if let Err(e) = result {
            report.failed += 1;
            report.errors.push(format!("entry {}: {e:#}", row.entry_id));
            log::warn!("Retention: entry {} failed: {e:#}", row.entry_id);
        }
    }
    Ok(report)
}

// ---------------------------------------------------------------------------
// Compression
// ---------------------------------------------------------------------------

fn read_wav_i16(path: &Path) -> Result<(Vec<i16>, u32, u16)> {
    let reader = hound::WavReader::open(path)?;
    let spec = reader.spec();
    if spec.bits_per_sample != 16 || spec.sample_format != hound::SampleFormat::Int {
        bail!("not a 16-bit PCM WAV");
    }
    let samples = reader
        .into_samples::<i16>()
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok((samples, spec.sample_rate, spec.channels))
}

pub fn encode_flac(samples: &[i16], sample_rate: u32, channels: u16) -> Result<Vec<u8>> {
    use flacenc::component::BitRepr;
    use flacenc::error::Verify;
    let config = flacenc::config::Encoder::default()
        .into_verified()
        .map_err(|e| anyhow!("flac config: {e:?}"))?;
    let ints: Vec<i32> = samples.iter().map(|&s| i32::from(s)).collect();
    let source = flacenc::source::MemSource::from_samples(
        &ints,
        usize::from(channels),
        16,
        sample_rate as usize,
    );
    let stream = flacenc::encode_with_fixed_block_size(&config, source, config.block_size)
        .map_err(|e| anyhow!("flac encode: {e:?}"))?;
    let mut sink = flacenc::bitsink::ByteSink::new();
    stream
        .write(&mut sink)
        .map_err(|e| anyhow!("flac write: {e:?}"))?;
    let mut bytes = sink.as_slice().to_vec();
    fix_min_block_size(&mut bytes);
    Ok(bytes)
}

/// flacenc records the (shorter) final block in STREAMINFO's minimum block
/// size. The FLAC spec excludes the last block from that field, and
/// symphonia (Handy's decoder) refuses such a fixed-blocksize stream, so
/// set min = max as the spec says. Audio and its MD5 are unaffected.
fn fix_min_block_size(bytes: &mut [u8]) {
    // "fLaC", then the STREAMINFO block header (type 0) and its body.
    if bytes.len() >= 12 && &bytes[..4] == b"fLaC" && bytes[4] & 0x7f == 0 {
        let max = [bytes[10], bytes[11]];
        bytes[8] = max[0];
        bytes[9] = max[1];
    }
}

/// Decodes a FLAC file to interleaved i16, exactly.
pub fn decode_flac_i16(path: &Path) -> Result<(Vec<i16>, u32)> {
    use symphonia::core::audio::SampleBuffer;
    use symphonia::core::codecs::DecoderOptions;
    use symphonia::core::errors::Error as SymErr;
    use symphonia::core::formats::FormatOptions;
    use symphonia::core::io::MediaSourceStream;
    use symphonia::core::meta::MetadataOptions;
    use symphonia::core::probe::Hint;

    let file = fs::File::open(path)?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    hint.with_extension("flac");
    let mut format = symphonia::default::get_probe()
        .format(
            &hint,
            mss,
            &FormatOptions::default(),
            &MetadataOptions::default(),
        )?
        .format;
    let track = format
        .default_track()
        .ok_or_else(|| anyhow!("no track"))?
        .clone();
    let rate = track.codec_params.sample_rate.unwrap_or(0);
    let mut decoder =
        symphonia::default::get_codecs().make(&track.codec_params, &DecoderOptions::default())?;
    let mut out = Vec::new();
    loop {
        let packet = match format.next_packet() {
            Ok(p) => p,
            Err(SymErr::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(e.into()),
        };
        let decoded = decoder.decode(&packet)?;
        let mut buf = SampleBuffer::<i16>::new(decoded.capacity() as u64, *decoded.spec());
        buf.copy_interleaved_ref(decoded);
        out.extend_from_slice(buf.samples());
    }
    Ok((out, rate))
}

fn write_synced(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut f = fs::File::create(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}

/// Places `flac` next to every copy of the WAV (dataset + History), switches
/// the records, then removes the WAVs.
fn compress_entry(
    paths: &Paths,
    row: &Row,
    wav: &Path,
    report: &mut RetentionReport,
) -> Result<()> {
    let (samples, rate, channels) = read_wav_i16(wav).context("reading the WAV")?;
    let flac_bytes = encode_flac(&samples, rate, channels)?;
    let flac_name = format!(
        "{}.flac",
        wav.file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default()
    );
    let wav_name = file_name(wav);
    let flac_path = wav.with_file_name(&flac_name);
    let partial = wav.with_file_name(format!("{flac_name}.partial"));
    write_synced(&partial, &flac_bytes)?;
    // Verify before anything points at it.
    let (decoded, decoded_rate) = decode_flac_i16(&partial).context("verifying the FLAC")?;
    if decoded != samples || decoded_rate != rate {
        let _ = fs::remove_file(&partial);
        bail!("FLAC verification failed (samples differ); WAV kept");
    }
    fs::rename(&partial, &flac_path)?;

    // History's copy of the same recording (usually a hard link).
    let history_wav = paths.recordings.join(&wav_name);
    let history_flac = paths.recordings.join(&flac_name);
    let mut history_copy = false;
    if history_wav.is_file() && history_wav != wav {
        if !history_flac.exists() && fs::hard_link(&flac_path, &history_flac).is_err() {
            write_synced(&history_flac, &flac_bytes)?;
        }
        history_copy = true;
    }

    // Switch Handy's records in one transaction.
    let mut conn = open_history(&paths.history_db)?;
    let tx = conn.transaction()?;
    let new_audio_path = if wav.starts_with(&paths.recordings) && row.audio_path.is_none() {
        None
    } else {
        Some(flac_path.to_string_lossy().into_owned())
    };
    tx.execute(
        "UPDATE dictation_log SET audio_file = ?1, audio_path = ?2 WHERE id = ?3",
        params![flac_name, new_audio_path, row.entry_id],
    )?;
    let mut changed: Vec<i64> = Vec::new();
    {
        let mut stmt = tx.prepare("SELECT id FROM transcription_history WHERE file_name = ?1")?;
        let ids = stmt.query_map([&wav_name], |r| r.get::<_, i64>(0))?;
        for id in ids {
            changed.push(id?);
        }
    }
    tx.execute(
        "UPDATE transcription_history SET file_name = ?1 WHERE file_name = ?2",
        params![flac_name, wav_name],
    )?;
    tx.commit()?;

    // Only now remove the WAVs.
    let before = fs::metadata(wav).map(|m| m.len()).unwrap_or(0);
    fs::remove_file(wav)?;
    if history_copy {
        let _ = fs::remove_file(&history_wav);
    }
    report.compressed += 1;
    report.bytes_freed += before.saturating_sub(flac_bytes.len() as u64) as f64;
    report.history_ids_changed.extend(changed);
    Ok(())
}

// ---------------------------------------------------------------------------
// Deletion
// ---------------------------------------------------------------------------

fn delete_entry(
    app_data_dir: &Path,
    paths: &Paths,
    row: &Row,
    audio: &Path,
    report: &mut RetentionReport,
) -> Result<()> {
    // Re-check right before deleting: the plan may be stale.
    if !verbatim_store::verified_done(app_data_dir, row.entry_id)? {
        bail!("no verified verbatim transcript; audio kept");
    }
    let conn = open_history(&paths.history_db)?;
    if let Some(f) = &row.audio_file {
        let shown: i64 = conn.query_row(
            "SELECT COUNT(*) FROM transcription_history WHERE file_name = ?1",
            [f],
            |r| r.get(0),
        )?;
        if shown > 0 {
            bail!("History still shows this recording; audio kept");
        }
    }
    // Records first: never a record pointing at a deleted file.
    conn.execute(
        "UPDATE dictation_log SET audio_path = NULL WHERE id = ?1",
        [row.entry_id],
    )?;
    drop(conn);
    let bytes = fs::metadata(audio).map(|m| m.len()).unwrap_or(0);
    fs::remove_file(audio)?;
    if let Some(f) = &row.audio_file {
        for copy in [paths.dataset_audio.join(f), paths.recordings.join(f)] {
            if copy != audio && copy.is_file() {
                let _ = fs::remove_file(copy);
            }
        }
    }
    report.deleted += 1;
    report.bytes_freed += bytes as f64;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::personal::verbatim_store::tests::fake_worker_db;

    const NOW: i64 = 1_800_000_000;

    struct Fixture {
        dir: tempfile::TempDir,
    }

    impl Fixture {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let conn = Connection::open(dir.path().join("history.db")).unwrap();
            let schema = include_str!("../../../analysis-worker/tests/fixtures/handy_schema.sql");
            conn.execute_batch(schema).unwrap();
            fs::create_dir_all(dir.path().join("dataset/audio")).unwrap();
            fs::create_dir_all(dir.path().join("recordings")).unwrap();
            Self { dir }
        }

        fn app(&self) -> &Path {
            self.dir.path()
        }

        fn wav(path: &Path, seconds: f32) -> Vec<i16> {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: 16_000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            };
            let mut w = hound::WavWriter::create(path, spec).unwrap();
            let n = (seconds * 16_000.0) as usize;
            let samples: Vec<i16> = (0..n)
                .map(|i| ((i as f32 * 0.05).sin() * 8000.0 + (i % 7) as f32 * 30.0) as i16)
                .collect();
            for s in &samples {
                w.write_sample(*s).unwrap();
            }
            w.finalize().unwrap();
            samples
        }

        /// A dictation `age_days` old with audio in dataset/audio, optionally
        /// also in History (hard link, like Handy does).
        fn dictation(&self, id: i64, age_days: i64, in_history: bool) -> Vec<i16> {
            let name = format!("handy-{id}.wav");
            let dataset = self.app().join("dataset/audio").join(&name);
            let samples = Self::wav(&dataset, 1.0);
            let conn = Connection::open(self.app().join("history.db")).unwrap();
            conn.execute(
                "INSERT INTO dictation_log (id, created_at, audio_file, audio_path, raw_asr_text,
                    rule_cleaned_text, final_text) VALUES (?1, ?2, ?3, ?4, 'x', 'x', 'x')",
                params![id, NOW - age_days * DAY, name, dataset.to_string_lossy()],
            )
            .unwrap();
            if in_history {
                fs::hard_link(&dataset, self.app().join("recordings").join(&name)).unwrap();
                conn.execute(
                    "INSERT INTO transcription_history (file_name, timestamp, title, transcription_text)
                     VALUES (?1, ?2, 't', 'x')",
                    params![name, NOW - age_days * DAY],
                )
                .unwrap();
            }
            samples
        }

        fn row(&self, id: i64) -> (Option<String>, Option<String>) {
            Connection::open(self.app().join("history.db"))
                .unwrap()
                .query_row(
                    "SELECT audio_file, audio_path FROM dictation_log WHERE id = ?1",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .unwrap()
        }
    }

    fn enabled() -> RetentionSettings {
        RetentionSettings {
            enabled: true,
            ..Default::default()
        }
    }

    #[test]
    fn flac_round_trip_is_lossless() {
        let dir = tempfile::tempdir().unwrap();
        let wav = dir.path().join("a.wav");
        let samples = Fixture::wav(&wav, 2.0);
        let bytes = encode_flac(&samples, 16_000, 1).unwrap();
        assert!(bytes.len() < fs::metadata(&wav).unwrap().len() as usize);
        let flac = dir.path().join("a.flac");
        fs::write(&flac, bytes).unwrap();
        let (decoded, rate) = decode_flac_i16(&flac).unwrap();
        assert_eq!(rate, 16_000);
        assert_eq!(decoded, samples);
        // The rest of Handy (retry, analysis) reads it too.
        let f32s = crate::audio_toolkit::decode_audio_file(&flac).unwrap();
        assert_eq!(f32s.len(), samples.len());
    }

    #[test]
    fn preview_never_lists_unprocessed_or_unverified_audio_for_deletion() {
        let fx = Fixture::new();
        fx.dictation(1, 200, false); // old, verified → delete
        fx.dictation(2, 200, false); // old, no transcript → keep
        fx.dictation(3, 200, false); // old, unverified → keep
        fx.dictation(4, 200, true); // old, verified, but in History → keep
        fx.dictation(5, 40, false); // compress only
        fx.dictation(6, 5, false); // nothing
        fake_worker_db(
            fx.app(),
            &[
                (1, "um hi", "done", true),
                (3, "uh", "done", false),
                (4, "hi", "done", true),
            ],
        );
        let p = preview(fx.app(), &enabled(), NOW).unwrap();
        assert_eq!(
            p.delete.iter().map(|i| i.entry_id).collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(
            (p.kept_unprocessed, p.kept_unverified, p.kept_in_history),
            (1, 1, 1)
        );
        let mut compress: Vec<i64> = p.compress.iter().map(|i| i.entry_id).collect();
        compress.sort_unstable();
        // Kept old ones are still compressed (lossless), plus the 40-day one.
        assert_eq!(compress, vec![2, 3, 4, 5]);
        assert!(p.bytes_to_delete > 0.0);
    }

    #[test]
    fn disabled_retention_does_nothing() {
        let fx = Fixture::new();
        fx.dictation(1, 400, false);
        let r = apply(fx.app(), &RetentionSettings::default(), NOW, &|| true).unwrap();
        assert_eq!((r.compressed, r.deleted), (0, 0));
        assert!(fx.app().join("dataset/audio/handy-1.wav").exists());
    }

    #[test]
    fn never_deletes_unprocessed_audio() {
        let fx = Fixture::new();
        fx.dictation(1, 400, false);
        fx.dictation(2, 400, false);
        fake_worker_db(fx.app(), &[(2, "", "pending", false)]);
        let settings = RetentionSettings {
            enabled: true,
            compress_after_days: 0,
            delete_after_days: 180,
        };
        let r = apply(fx.app(), &settings, NOW, &|| true).unwrap();
        assert_eq!(r.deleted, 0);
        assert!(fx.app().join("dataset/audio/handy-1.wav").exists());
        assert!(fx.app().join("dataset/audio/handy-2.wav").exists());
    }

    #[test]
    fn deletes_only_verified_audio_and_keeps_records_consistent() {
        let fx = Fixture::new();
        fx.dictation(1, 400, false);
        fake_worker_db(fx.app(), &[(1, "um hi", "done", true)]);
        let settings = RetentionSettings {
            enabled: true,
            compress_after_days: 0,
            delete_after_days: 180,
        };
        let r = apply(fx.app(), &settings, NOW, &|| true).unwrap();
        assert_eq!(r.deleted, 1, "{:?}", r.errors);
        assert!(!fx.app().join("dataset/audio/handy-1.wav").exists());
        let (file, path) = fx.row(1);
        assert_eq!(file.as_deref(), Some("handy-1.wav")); // name kept for linking
        assert_eq!(path, None); // nothing points at a missing file
                                // The dictation itself (text) is untouched.
        let n: i64 = Connection::open(fx.app().join("history.db"))
            .unwrap()
            .query_row("SELECT COUNT(*) FROM dictation_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn compression_moves_files_and_updates_every_record() {
        let fx = Fixture::new();
        let samples = fx.dictation(1, 40, true); // dataset + History copy
        let r = apply(fx.app(), &enabled(), NOW, &|| true).unwrap();
        assert_eq!(r.compressed, 1, "{:?}", r.errors);
        assert_eq!(r.history_ids_changed.len(), 1);

        let dataset_flac = fx.app().join("dataset/audio/handy-1.flac");
        let history_flac = fx.app().join("recordings/handy-1.flac");
        assert!(dataset_flac.exists() && history_flac.exists());
        assert!(!fx.app().join("dataset/audio/handy-1.wav").exists());
        assert!(!fx.app().join("recordings/handy-1.wav").exists());

        let (file, path) = fx.row(1);
        assert_eq!(file.as_deref(), Some("handy-1.flac"));
        assert_eq!(
            path.as_deref(),
            Some(dataset_flac.to_string_lossy().as_ref())
        );
        let history_name: String = Connection::open(fx.app().join("history.db"))
            .unwrap()
            .query_row("SELECT file_name FROM transcription_history", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(history_name, "handy-1.flac");
        // Playback / re-transcription read the moved file losslessly.
        assert_eq!(decode_flac_i16(&history_flac).unwrap().0, samples);
        // A second pass has nothing left to compress.
        let again = apply(fx.app(), &enabled(), NOW, &|| true).unwrap();
        assert_eq!(again.compressed, 0);
    }

    #[test]
    fn a_recording_starting_stops_the_pass() {
        let fx = Fixture::new();
        fx.dictation(1, 40, false);
        fx.dictation(2, 40, false);
        let r = apply(fx.app(), &enabled(), NOW, &|| false).unwrap();
        assert_eq!(r.compressed, 0);
    }

    #[test]
    fn a_corrupt_wav_is_reported_and_kept() {
        let fx = Fixture::new();
        fx.dictation(1, 40, false);
        fs::write(fx.app().join("dataset/audio/handy-1.wav"), b"RIFFjunk").unwrap();
        let r = apply(fx.app(), &enabled(), NOW, &|| true).unwrap();
        assert_eq!((r.compressed, r.failed), (0, 1));
        assert!(fx.app().join("dataset/audio/handy-1.wav").exists());
        assert_eq!(fx.row(1).0.as_deref(), Some("handy-1.wav"));
    }
}
