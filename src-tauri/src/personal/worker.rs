//! Talking to the verbatim worker (`analysis-worker/`, Python).
//!
//! Handy owns the worker's lifecycle: it finds the checkout and `uv` by
//! **absolute path** (a GUI app, like launchd, has no shell PATH), runs
//! `handy-worker` subcommands with an explicit environment, parses their JSON,
//! and decides whether the worker is missing, broken or out of date.
//!
//! The worker always runs as `<worker>/.venv/bin/python -m handy_worker`, the
//! same interpreter the launchd agent uses.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use specta::Type;

/// The worker version this Handy was built with (from its pyproject.toml).
pub const BUNDLED_PYPROJECT: &str = include_str!("../../../analysis-worker/pyproject.toml");
/// The status/CLI contract version (`handy_worker.versions.PROTOCOL`).
pub const PROTOCOL: u32 = 1;
/// Default location: the fork's own checkout.
pub const DEFAULT_WORKER_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../analysis-worker");

const UV_CANDIDATES: &[&str] = &[
    "~/.local/bin/uv",
    "~/.cargo/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
];
/// System tools only, like launchd gives the agent.
const SYSTEM_PATH: &str = "/usr/bin:/bin:/usr/sbin:/sbin";

pub fn bundled_worker_version() -> Option<String> {
    BUNDLED_PYPROJECT.lines().find_map(|l| {
        let l = l.trim();
        l.strip_prefix("version")
            .map(str::trim_start)
            .and_then(|r| r.strip_prefix('='))
            .map(|r| r.trim().trim_matches('"').to_string())
    })
}

fn expand_home(p: &str) -> PathBuf {
    match (p.strip_prefix("~/"), std::env::var_os("HOME")) {
        (Some(rest), Some(home)) => PathBuf::from(home).join(rest),
        _ => PathBuf::from(p),
    }
}

/// Everything needed to run the worker.
#[derive(Clone, Debug)]
pub struct WorkerEnv {
    pub worker_dir: PathBuf,
    pub uv: Option<PathBuf>,
    pub app_data_dir: PathBuf,
    pub log_dir: PathBuf,
    pub bundle_id: String,
    pub product_name: String,
}

impl WorkerEnv {
    pub fn python(&self) -> PathBuf {
        self.worker_dir.join(".venv").join("bin").join("python")
    }

    pub fn exists(&self) -> bool {
        self.worker_dir.join("pyproject.toml").is_file()
    }

    pub fn is_synced(&self) -> bool {
        self.python().exists()
    }

    fn base_command(&self, program: &Path) -> Command {
        let mut cmd = Command::new(program);
        cmd.current_dir(&self.worker_dir)
            .env_clear()
            .env("PATH", SYSTEM_PATH)
            .env("LANG", "en_US.UTF-8")
            .env("HANDY_APP_DATA_DIR", &self.app_data_dir)
            .env("HANDY_WORKER_LOG_DIR", &self.log_dir)
            .env("HANDY_BUNDLE_ID", &self.bundle_id)
            .env("HANDY_PRODUCT_NAME", &self.product_name)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null());
        if let Some(home) = std::env::var_os("HOME") {
            cmd.env("HOME", home);
        }
        cmd
    }

    /// `python -m handy_worker <args>`.
    pub fn worker_command(&self, args: &[&str]) -> Command {
        let mut cmd = self.base_command(&self.python());
        cmd.arg("-m").arg("handy_worker").args(args);
        cmd
    }

    /// `uv sync --frozen --extra model` (needs network for new packages).
    pub fn sync_command(&self) -> Option<Command> {
        let uv = self.uv.as_ref()?;
        let mut cmd = self.base_command(uv);
        // uv itself needs to find its cache and managed Pythons.
        let path = match uv.parent() {
            Some(dir) => format!("{}:{SYSTEM_PATH}", dir.display()),
            None => SYSTEM_PATH.to_string(),
        };
        cmd.env("PATH", path)
            .args(["sync", "--frozen", "--extra", "model", "--project"])
            .arg(&self.worker_dir);
        Some(cmd)
    }
}

/// Finds `uv`: the configured path, then the usual install locations.
pub fn find_uv(configured: Option<&str>) -> Option<PathBuf> {
    if let Some(p) = configured.filter(|p| !p.trim().is_empty()) {
        let p = expand_home(p.trim());
        return p.is_file().then_some(p);
    }
    UV_CANDIDATES
        .iter()
        .map(|c| expand_home(c))
        .find(|p| p.is_file())
}

pub fn resolve_worker_dir(configured: Option<&str>) -> PathBuf {
    let raw = configured
        .filter(|p| !p.trim().is_empty())
        .map(|p| expand_home(p.trim()))
        .unwrap_or_else(|| PathBuf::from(DEFAULT_WORKER_DIR));
    raw.canonicalize().unwrap_or(raw)
}

/// `~/Library/Logs/<product name>/` on macOS (the worker's default).
pub fn default_log_dir(product_name: &str, bundle_id: &str) -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    if cfg!(target_os = "macos") {
        home.join("Library").join("Logs").join(product_name)
    } else {
        home.join(".local")
            .join("state")
            .join(bundle_id)
            .join("logs")
    }
}

#[derive(Debug)]
pub struct Output {
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

/// Runs `cmd` with a timeout (the child is killed when it expires).
pub fn run_with_timeout(mut cmd: Command, timeout: Duration) -> Result<Output, String> {
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("cannot start {:?}: {e}", cmd.get_program()))?;
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let out_reader = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(o) = stdout.as_mut() {
            let _ = std::io::Read::read_to_string(o, &mut s);
        }
        s
    });
    let err_reader = std::thread::spawn(move || {
        let mut s = String::new();
        if let Some(e) = stderr.as_mut() {
            let _ = std::io::Read::read_to_string(e, &mut s);
        }
        s
    });
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if start.elapsed() >= timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("timed out after {}s", timeout.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(e.to_string()),
        }
    };
    Ok(Output {
        code: status.code(),
        stdout: out_reader.join().unwrap_or_default(),
        stderr: err_reader.join().unwrap_or_default(),
    })
}

// ---------------------------------------------------------------------------
// `handy-worker status` JSON (only the fields Handy shows; tolerant parsing)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerRun {
    pub id: i64,
    pub trigger: String,
    pub started_at: i64,
    pub finished_at: Option<i64>,
    pub outcome: Option<String>,
    pub message: Option<String>,
    pub processed: i64,
    pub failed: i64,
    pub skipped: i64,
    pub yields: i64,
    pub queue_after: Option<i64>,
    pub audio_s: f64,
    pub processing_s: f64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerFailure {
    pub entry_id: i64,
    pub created_at: i64,
    pub status: String,
    pub attempts: i64,
    pub last_error: Option<String>,
    pub skip_reason: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerQueue {
    pub pending: Option<i64>,
    pub dictations_with_audio: Option<i64>,
    pub done: i64,
    pub failed: i64,
    pub skipped: i64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerModel {
    pub model_id: String,
    pub revision: Option<String>,
    pub state: String,
    pub bytes: Option<f64>,
    pub license: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerDisk {
    pub worker_bytes: f64,
    pub model_bytes: f64,
    pub results_db_bytes: f64,
    pub free_bytes: Option<f64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerVersions {
    pub worker_version: String,
    pub source_version: Option<String>,
    pub protocol: u32,
    pub up_to_date: bool,
    pub problems: Vec<String>,
    pub python_version: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerLaunchd {
    pub installed: bool,
    pub loaded: bool,
    pub last_exit: Option<String>,
    pub label: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerSchema {
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerRunning {
    pub pid: Option<i64>,
    pub started_at: Option<i64>,
    pub trigger: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerStatusPaths {
    pub log_dir: String,
    pub log_file: String,
    pub results_db: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, Type, PartialEq)]
#[serde(default)]
pub struct WorkerStatus {
    pub ok: bool,
    pub attention: Vec<String>,
    pub versions: WorkerVersions,
    pub paths: WorkerStatusPaths,
    pub running: Option<WorkerRunning>,
    pub queue: WorkerQueue,
    pub schema: WorkerSchema,
    pub last_run: Option<WorkerRun>,
    pub recent_runs: Vec<WorkerRun>,
    pub failures: Vec<WorkerFailure>,
    pub model: WorkerModel,
    pub disk: WorkerDisk,
    pub launchd: WorkerLaunchd,
}

pub fn parse_status(json: &str) -> Result<WorkerStatus, String> {
    serde_json::from_str(json).map_err(|e| format!("unreadable worker status: {e}"))
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

/// What the settings/Insights UI shows, with a one-click fix where possible.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Type)]
#[serde(rename_all = "snake_case")]
pub enum WorkerHealth {
    /// No `analysis-worker/` at the configured path.
    Missing,
    /// `uv` not found (needed to set up / update the environment).
    NoUv,
    /// The environment was never created (`uv sync` not run).
    NotSynced,
    /// The checkout changed since the last sync, or Handy expects a
    /// different worker version/protocol. Fix: re-sync.
    OutOfDate,
    /// `status` failed to run or returned garbage.
    Broken,
    /// Running fine but something needs a decision (model not downloaded,
    /// unknown schema, repeated failures).
    NeedsAttention,
    Ok,
}

/// Classifies the worker from what's on disk and its status.
pub fn classify(
    env_exists: bool,
    uv_found: bool,
    synced: bool,
    status: Option<&Result<WorkerStatus, String>>,
) -> WorkerHealth {
    if !env_exists {
        return WorkerHealth::Missing;
    }
    if !synced {
        return if uv_found {
            WorkerHealth::NotSynced
        } else {
            WorkerHealth::NoUv
        };
    }
    let Some(status) = status else {
        return WorkerHealth::Broken;
    };
    let Ok(status) = status else {
        return WorkerHealth::Broken;
    };
    let expected = bundled_worker_version();
    let version_mismatch = expected
        .as_deref()
        .is_some_and(|v| v != status.versions.worker_version);
    if !status.versions.up_to_date || status.versions.protocol != PROTOCOL || version_mismatch {
        return WorkerHealth::OutOfDate;
    }
    if !status.ok || !status.attention.is_empty() {
        return WorkerHealth::NeedsAttention;
    }
    WorkerHealth::Ok
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
        "ok": false,
        "attention": ["the verbatim model is not downloaded"],
        "versions": {"worker_version": "0.2.0", "source_version": "0.2.0", "protocol": 1,
                     "lock_current": "a", "lock_synced": "a", "up_to_date": true, "problems": []},
        "paths": {"app_data": "/x", "results_db": "/x/verbatim/verbatim.db",
                  "log_file": "/l/verbatim-worker.log", "log_dir": "/l"},
        "running": null, "stale_lock": null,
        "handy_busy": {"active": false, "reason": "no flag"},
        "queue": {"pending": 3, "dictations_with_audio": 5, "done": 2,
                  "failed": 0, "skipped": 0, "total": 2},
        "schema": {"ok": true, "history_user_version": 4, "dictation_log_version": 2},
        "last_run": {"id": 7, "trigger": "schedule", "started_at": 100, "finished_at": 160,
                     "outcome": "partial", "message": "time budget reached (07:00)",
                     "processed": 2, "failed": 0, "skipped": 0, "yields": 1,
                     "queue_before": 5, "queue_after": 3, "audio_s": 30.5,
                     "processing_s": 12.0, "worker_version": "0.2.0", "model_revision": null},
        "recent_runs": [],
        "failures": [{"entry_id": 9, "history_id": null, "created_at": 1, "status": "failed",
                      "attempts": 3, "last_error": "boom", "skip_reason": null, "updated_at": 2}],
        "model": {"model_id": "nyrahealth/CrisperWhisper", "state": "not_downloaded",
                  "license": "CC BY-NC 4.0"},
        "disk": {"worker_bytes": 10, "model_bytes": 0, "results_db_bytes": 10, "free_bytes": 99},
        "launchd": {"installed": true, "loaded": true, "plist": "/p", "last_exit": "0",
                    "label": "com.baluvanan.handy-personal.verbatim-worker"},
        "config": {"schedule_hour": 2},
        "a_field_from_a_newer_worker": 1
    }"#;

    #[test]
    fn parses_the_worker_status_tolerantly() {
        let s = parse_status(SAMPLE).unwrap();
        assert_eq!(s.queue.pending, Some(3));
        assert_eq!(
            s.last_run.as_ref().unwrap().outcome.as_deref(),
            Some("partial")
        );
        assert_eq!(s.failures[0].attempts, 3);
        assert_eq!(s.model.state, "not_downloaded");
        assert!(s.launchd.loaded);
        assert!(parse_status("not json").is_err());
        // Missing fields default.
        let empty = parse_status("{}").unwrap();
        assert!(empty.last_run.is_none());
    }

    #[test]
    fn bundled_version_matches_the_checkout() {
        let v = bundled_worker_version().unwrap();
        assert!(v.split('.').count() == 3, "{v}");
    }

    #[test]
    fn classifies_health() {
        let ok_json = SAMPLE
            .replace("\"ok\": false", "\"ok\": true")
            .replace(
                "\"attention\": [\"the verbatim model is not downloaded\"]",
                "\"attention\": []",
            )
            .replace("0.2.0", &bundled_worker_version().unwrap());
        let ok_status = parse_status(&ok_json).unwrap();
        let ok: Result<WorkerStatus, String> = Ok(ok_status.clone());
        assert_eq!(
            classify(false, true, true, Some(&ok)),
            WorkerHealth::Missing
        );
        assert_eq!(classify(true, false, false, None), WorkerHealth::NoUv);
        assert_eq!(classify(true, true, false, None), WorkerHealth::NotSynced);
        assert_eq!(
            classify(true, true, true, Some(&Err("x".into()))),
            WorkerHealth::Broken
        );
        assert_eq!(classify(true, true, true, Some(&ok)), WorkerHealth::Ok);

        let mut stale = ok_status.clone();
        stale.versions.up_to_date = false;
        assert_eq!(
            classify(true, true, true, Some(&Ok(stale))),
            WorkerHealth::OutOfDate
        );
        let mut old = ok_status.clone();
        old.versions.worker_version = "0.0.1".into();
        assert_eq!(
            classify(true, true, true, Some(&Ok(old))),
            WorkerHealth::OutOfDate
        );
        let mut proto = ok_status.clone();
        proto.versions.protocol = PROTOCOL + 1;
        assert_eq!(
            classify(true, true, true, Some(&Ok(proto))),
            WorkerHealth::OutOfDate
        );
        let mut attention = ok_status;
        attention.attention = vec!["model".into()];
        assert_eq!(
            classify(true, true, true, Some(&Ok(attention))),
            WorkerHealth::NeedsAttention
        );
    }

    #[test]
    fn commands_get_an_explicit_environment() {
        let env = WorkerEnv {
            worker_dir: PathBuf::from("/repo/analysis-worker"),
            uv: Some(PathBuf::from("/opt/homebrew/bin/uv")),
            app_data_dir: PathBuf::from("/data"),
            log_dir: PathBuf::from("/logs"),
            bundle_id: "com.example".into(),
            product_name: "Example".into(),
        };
        let cmd = env.worker_command(&["status"]);
        assert_eq!(cmd.get_program(), "/repo/analysis-worker/.venv/bin/python");
        let args: Vec<_> = cmd.get_args().collect();
        assert_eq!(args, ["-m", "handy_worker", "status"]);
        let vars: std::collections::HashMap<String, String> = cmd
            .get_envs()
            .filter_map(|(k, v)| {
                Some((
                    k.to_string_lossy().into_owned(),
                    v?.to_string_lossy().into_owned(),
                ))
            })
            .collect();
        assert_eq!(vars["HANDY_APP_DATA_DIR"], "/data");
        assert_eq!(vars["PATH"], SYSTEM_PATH);
        let sync = env.sync_command().unwrap();
        let args: Vec<_> = sync
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(&args[..4], ["sync", "--frozen", "--extra", "model"]);
    }

    #[test]
    fn run_with_timeout_kills_a_hung_child() {
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "sleep 5"]);
        let err = run_with_timeout(cmd, Duration::from_millis(200)).unwrap_err();
        assert!(err.contains("timed out"));
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", "echo hi; echo err >&2; exit 3"]);
        let out = run_with_timeout(cmd, Duration::from_secs(5)).unwrap();
        assert_eq!(
            (out.code, out.stdout.trim(), out.stderr.trim()),
            (Some(3), "hi", "err")
        );
    }
}
