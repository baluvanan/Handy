//! The "recording active" flag the verbatim worker yields to.
//!
//! `<app data>/handy-busy.json` exists while Handy records, transcribes,
//! pastes or runs a background transcription job, and is removed when it is
//! idle. The worker (`analysis-worker/src/handy_worker/busy.py`) checks it
//! every 100 ms and between decoding steps, and gets off the CPU/GPU within
//! a second.
//!
//! Dictation is never slowed down by this: the file is written on its own
//! thread. The truth comes from state Handy already keeps (the tray's busy
//! state and the transcription manager's job flag), polled every 250 ms and
//! nudged immediately when a recording starts, so a missed event can never
//! leave the flag wrong. While busy, the heartbeat is refreshed every 10 s;
//! the worker ignores a flag whose PID is dead or whose heartbeat is older
//! than 45 s, so a crashed Handy can't block it forever.
//!
//! Hooks: `transcription_coordinator::run_effect` (nudge on start),
//! `lib.rs` (`init`).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

pub const FLAG_FILE: &str = "handy-busy.json";
const POLL: Duration = Duration::from_millis(250);
pub const HEARTBEAT: Duration = Duration::from_secs(10);

/// What Handy is doing (written for the worker's logs and status).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BusyState {
    /// Recording, transcribing or pasting a dictation.
    Dictating,
    /// A file import or a re-transcription is running.
    Transcribing,
}

#[derive(Debug, Serialize, Deserialize, PartialEq)]
pub struct FlagContents {
    pub version: u32,
    pub pid: u32,
    pub state: BusyState,
    /// Unix seconds (fractional) when this busy stretch began.
    pub since: f64,
    /// Unix seconds of the last refresh.
    pub heartbeat: f64,
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Writes the flag atomically (temp file + rename), so the worker never
/// reads a half-written one.
pub fn write_flag(path: &Path, contents: &FlagContents) -> std::io::Result<()> {
    let tmp = path.with_extension("json.tmp");
    let json = serde_json::to_vec(contents).map_err(std::io::Error::other)?;
    fs::write(&tmp, json)?;
    fs::rename(&tmp, path)
}

pub fn remove_flag(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
        _ => Ok(()),
    }
}

/// The flag writer's state machine, separated from I/O and time for tests.
#[derive(Debug, Default)]
pub struct FlagWriter {
    current: Option<(BusyState, f64)>,
    last_write: Option<Instant>,
}

/// What to do with the file after an observation.
#[derive(Debug, PartialEq)]
pub enum FlagAction {
    Write(FlagContents),
    Remove,
    Nothing,
}

impl FlagWriter {
    pub fn observe(&mut self, busy: Option<BusyState>, now: Instant, unix: f64) -> FlagAction {
        match (busy, self.current) {
            (None, None) => FlagAction::Nothing,
            (None, Some(_)) => {
                self.current = None;
                self.last_write = None;
                FlagAction::Remove
            }
            (Some(state), current) => {
                let since = match current {
                    Some((_, since)) => since,
                    None => unix,
                };
                let changed = current.map(|(s, _)| s) != Some(state);
                let due = self
                    .last_write
                    .is_none_or(|t| now.duration_since(t) >= HEARTBEAT);
                if !changed && !due {
                    return FlagAction::Nothing;
                }
                self.current = Some((state, since));
                self.last_write = Some(now);
                FlagAction::Write(FlagContents {
                    version: 1,
                    pid: std::process::id(),
                    state,
                    since,
                    heartbeat: unix,
                })
            }
        }
    }
}

struct Shared {
    wake: Condvar,
    nudged: Mutex<bool>,
}

static SHARED: OnceLock<Arc<Shared>> = OnceLock::new();

/// Wakes the flag thread now (a recording just started).
pub fn nudge() {
    if let Some(shared) = SHARED.get() {
        *shared
            .nudged
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = true;
        shared.wake.notify_one();
    }
}

fn current_state(app: &AppHandle) -> Option<BusyState> {
    if crate::tray::dictation_in_progress(app)
        || app
            .try_state::<Arc<crate::managers::audio::AudioRecordingManager>>()
            .is_some_and(|rm| rm.is_recording())
    {
        return Some(BusyState::Dictating);
    }
    if app
        .try_state::<Arc<crate::managers::transcription::TranscriptionManager>>()
        .is_some_and(|tm| tm.job_active())
    {
        return Some(BusyState::Transcribing);
    }
    None
}

pub fn flag_path(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join(FLAG_FILE)
}

/// Starts the flag thread. Removes a flag a previous (crashed) Handy left.
pub fn init(app: &AppHandle) {
    let Ok(dir) = crate::portable::app_data_dir(app) else {
        return;
    };
    let path = flag_path(&dir);
    if let Err(e) = remove_flag(&path) {
        log::warn!("Busy flag: cannot remove a stale flag: {e}");
    }
    let shared = Arc::new(Shared {
        wake: Condvar::new(),
        nudged: Mutex::new(false),
    });
    if SHARED.set(Arc::clone(&shared)).is_err() {
        return;
    }
    let app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("handy-busy-flag".into())
        .spawn(move || {
            let mut writer = FlagWriter::default();
            loop {
                let action = writer.observe(current_state(&app), Instant::now(), unix_now());
                let result = match &action {
                    FlagAction::Write(contents) => write_flag(&path, contents),
                    FlagAction::Remove => remove_flag(&path),
                    FlagAction::Nothing => Ok(()),
                };
                if let Err(e) = result {
                    log::debug!("Busy flag update failed: {e}");
                }
                let guard = shared
                    .nudged
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                let (mut guard, _) = shared
                    .wake
                    .wait_timeout_while(guard, POLL, |nudged| !*nudged)
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                *guard = false;
            }
        });
    if let Err(e) = spawned {
        log::warn!("Busy flag thread failed to start: {e}");
    }
}

/// Removes the flag on a clean exit.
pub fn clear(app: &AppHandle) {
    if let Ok(dir) = crate::portable::app_data_dir(app) {
        let _ = remove_flag(&flag_path(&dir));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_on_start_refreshes_heartbeat_and_removes_when_idle() {
        let mut w = FlagWriter::default();
        let t0 = Instant::now();
        assert_eq!(w.observe(None, t0, 100.0), FlagAction::Nothing);

        let FlagAction::Write(first) = w.observe(Some(BusyState::Dictating), t0, 100.0) else {
            panic!("expected a write when recording starts");
        };
        assert_eq!(first.state, BusyState::Dictating);
        assert_eq!(first.pid, std::process::id());
        assert_eq!((first.since, first.heartbeat), (100.0, 100.0));

        // Still busy, before the heartbeat is due: no I/O.
        let t1 = t0 + Duration::from_secs(3);
        assert_eq!(
            w.observe(Some(BusyState::Dictating), t1, 103.0),
            FlagAction::Nothing
        );
        // Heartbeat due: rewrite, keeping `since`.
        let t2 = t0 + HEARTBEAT;
        let FlagAction::Write(beat) = w.observe(Some(BusyState::Dictating), t2, 110.0) else {
            panic!("expected a heartbeat");
        };
        assert_eq!((beat.since, beat.heartbeat), (100.0, 110.0));
        // State change (dictation done, a file job runs): immediate rewrite.
        let t3 = t2 + Duration::from_secs(1);
        let FlagAction::Write(job) = w.observe(Some(BusyState::Transcribing), t3, 111.0) else {
            panic!("expected a write on state change");
        };
        assert_eq!(job.state, BusyState::Transcribing);
        // Idle: removed once.
        assert_eq!(w.observe(None, t3, 111.0), FlagAction::Remove);
        assert_eq!(w.observe(None, t3, 111.0), FlagAction::Nothing);
    }

    #[test]
    fn flag_file_round_trips_in_the_format_the_worker_reads() {
        let dir = tempfile::tempdir().unwrap();
        let path = flag_path(dir.path());
        let contents = FlagContents {
            version: 1,
            pid: 42,
            state: BusyState::Dictating,
            since: 1.5,
            heartbeat: 2.5,
        };
        write_flag(&path, &contents).unwrap();
        let v: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        // Keys the Python side relies on (busy.read_flag).
        assert_eq!(v["pid"], 42);
        assert_eq!(v["state"], "dictating");
        assert_eq!(v["heartbeat"], 2.5);
        assert!(!path.with_extension("json.tmp").exists());
        remove_flag(&path).unwrap();
        assert!(!path.exists());
        remove_flag(&path).unwrap(); // idempotent
    }
}
