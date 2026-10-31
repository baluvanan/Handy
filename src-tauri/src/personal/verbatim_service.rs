//! The background verbatim-analysis service, owned by Handy.
//!
//! Handy decides; the worker (`analysis-worker/`) does the heavy lifting in
//! its own process. This module:
//! - stores the settings and writes them to the worker's `config.json`;
//! - installs/uninstalls the launchd agent through the worker when the
//!   service is switched on/off (and re-installs when the schedule changes);
//! - triggers a run when you've been idle for N minutes and the queue isn't
//!   empty, or when you press "Run now";
//! - keeps the worker healthy: detects missing / not set up / out of date /
//!   broken, offers a one-click fix and, if allowed, re-syncs automatically
//!   after you pull a new version of the fork;
//! - after each run refreshes the speech metrics, runs the retention pass
//!   (daily, if enabled), and generates the weekly coaching note;
//! - raises a macOS notification only when the service needs attention.
//!
//! Nothing here runs on the dictation path; everything is on its own thread
//! and backs off while Handy is busy.

use std::path::PathBuf;
use std::process::Child;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use specta::Type;
use tauri::{AppHandle, Manager};
use tauri_specta::Event;

use super::retention::{RetentionPreview, RetentionSettings};
use super::worker::{self, WorkerEnv, WorkerHealth, WorkerStatus};
use crate::settings::{get_settings, write_settings};

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Type)]
#[serde(default)]
pub struct VerbatimServiceSettings {
    /// Installs the launchd agent (nightly + at login) and enables triggers.
    pub enabled: bool,
    pub schedule_hour: u8,
    pub schedule_minute: u8,
    /// Run when the Mac has been idle this long and there is work queued.
    pub idle_trigger_enabled: bool,
    pub idle_minutes: u32,
    pub skip_on_battery: bool,
    /// Nightly runs stop by this time and continue on the next run.
    pub time_budget_enabled: bool,
    pub stop_by_hour: u8,
    pub stop_by_minute: u8,
    /// Re-sync the worker's environment automatically when it's out of date.
    pub auto_update: bool,
    /// Empty: the fork's own `analysis-worker/`.
    pub worker_dir: String,
    /// Empty: search the usual locations.
    pub uv_path: String,
    pub retention: RetentionSettings,
    /// Generate the weekly coaching note on Mondays.
    pub coaching_weekly: bool,
    /// LLM provider for the coaching note (aggregated metrics only).
    pub coaching_provider_id: String,
}

impl Default for VerbatimServiceSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            schedule_hour: 2,
            schedule_minute: 0,
            idle_trigger_enabled: true,
            idle_minutes: 20,
            skip_on_battery: true,
            time_budget_enabled: true,
            stop_by_hour: 7,
            stop_by_minute: 0,
            auto_update: true,
            worker_dir: String::new(),
            uv_path: String::new(),
            retention: RetentionSettings::default(),
            coaching_weekly: true,
            coaching_provider_id: super::lmstudio::PROVIDER_ID.to_string(),
        }
    }
}

impl VerbatimServiceSettings {
    /// Clamps out-of-range values (hand-edited settings, bad input).
    pub fn sanitized(mut self) -> Self {
        self.schedule_hour = self.schedule_hour.min(23);
        self.schedule_minute = self.schedule_minute.min(59);
        self.stop_by_hour = self.stop_by_hour.min(23);
        self.stop_by_minute = self.stop_by_minute.min(59);
        self.idle_minutes = self.idle_minutes.clamp(5, 24 * 60);
        if self.retention.delete_after_days > 0
            && self.retention.compress_after_days > self.retention.delete_after_days
        {
            self.retention.compress_after_days = self.retention.delete_after_days;
        }
        self
    }

    /// The worker's `config.json` (see `handy_worker/config.py`).
    pub fn worker_config(&self) -> serde_json::Value {
        serde_json::json!({
            "schedule_hour": self.schedule_hour,
            "schedule_minute": self.schedule_minute,
            "skip_on_battery": self.skip_on_battery,
            "time_budget_enabled": self.time_budget_enabled,
            "stop_by_hour": self.stop_by_hour,
            "stop_by_minute": self.stop_by_minute,
        })
    }

    fn schedule_changed(&self, other: &Self) -> bool {
        (self.schedule_hour, self.schedule_minute) != (other.schedule_hour, other.schedule_minute)
            || self.worker_dir != other.worker_dir
    }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/// Emitted whenever the service status may have changed (the UI refetches).
#[derive(Clone, Debug, Serialize, Deserialize, Type, tauri_specta::Event)]
pub struct VerbatimServiceChanged {
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Type)]
pub struct VerbatimServiceStatus {
    pub platform_supported: bool,
    pub enabled: bool,
    pub health: WorkerHealth,
    pub status: Option<WorkerStatus>,
    pub error: Option<String>,
    pub worker_dir: String,
    pub uv_path: Option<String>,
    pub log_dir: String,
    pub bundled_worker_version: Option<String>,
    /// A run Handy started is in progress.
    pub run_in_progress: bool,
    /// install / uninstall / sync / model download in progress.
    pub operation: Option<String>,
    pub last_operation_error: Option<String>,
    /// Seconds since last keyboard/mouse input (macOS), for the idle trigger.
    pub user_idle_seconds: Option<f64>,
}

#[derive(Default)]
struct ServiceState {
    status: Option<Result<WorkerStatus, String>>,
    status_at: Option<Instant>,
    run_child: Option<Child>,
    operation: Option<String>,
    last_operation_error: Option<String>,
    last_idle_trigger: Option<Instant>,
    last_retention_day: Option<chrono::NaiveDate>,
    last_coaching_attempt: Option<Instant>,
    notified: Option<String>,
    auto_sync_attempted_for: Option<String>,
}

pub struct VerbatimService {
    app: AppHandle,
    state: Mutex<ServiceState>,
}

const STATUS_TTL: Duration = Duration::from_secs(600);
const TICK: Duration = Duration::from_secs(30);
const IDLE_RETRIGGER: Duration = Duration::from_secs(30 * 60);
const COACHING_RETRY: Duration = Duration::from_secs(3 * 3600);

fn lock(state: &Mutex<ServiceState>) -> std::sync::MutexGuard<'_, ServiceState> {
    state
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Seconds since the last keyboard/mouse event (macOS); None elsewhere.
pub fn user_idle_seconds() -> Option<f64> {
    #[cfg(target_os = "macos")]
    {
        #[link(name = "CoreGraphics", kind = "framework")]
        extern "C" {
            fn CGEventSourceSecondsSinceLastEventType(state: i32, event_type: u32) -> f64;
        }
        // kCGEventSourceStateHIDSystemState = 1, kCGAnyInputEventType = ~0.
        let secs = unsafe { CGEventSourceSecondsSinceLastEventType(1, u32::MAX) };
        secs.is_finite().then_some(secs)
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

/// The idle trigger's decision, pure for tests.
pub fn should_trigger_idle(
    settings: &VerbatimServiceSettings,
    idle_seconds: Option<f64>,
    pending: Option<i64>,
    worker_running: bool,
    handy_busy: bool,
    since_last_trigger: Option<Duration>,
) -> bool {
    settings.enabled
        && settings.idle_trigger_enabled
        && !worker_running
        && !handy_busy
        && pending.unwrap_or(0) > 0
        && idle_seconds.is_some_and(|s| s >= f64::from(settings.idle_minutes) * 60.0)
        && since_last_trigger.is_none_or(|d| d >= IDLE_RETRIGGER)
}

impl VerbatimService {
    pub fn new(app: &AppHandle) -> Arc<Self> {
        Arc::new(Self {
            app: app.clone(),
            state: Mutex::new(ServiceState::default()),
        })
    }

    fn settings(&self) -> VerbatimServiceSettings {
        get_settings(&self.app).personal.verbatim_service
    }

    pub fn env(&self) -> Result<WorkerEnv, String> {
        let s = self.settings();
        let app_data_dir = crate::portable::app_data_dir(&self.app).map_err(|e| e.to_string())?;
        let config = self.app.config();
        let bundle_id = config.identifier.clone();
        let product_name = config
            .product_name
            .clone()
            .unwrap_or_else(|| bundle_id.clone());
        Ok(WorkerEnv {
            worker_dir: worker::resolve_worker_dir(Some(&s.worker_dir)),
            uv: worker::find_uv(Some(&s.uv_path)),
            log_dir: worker::default_log_dir(&product_name, &bundle_id),
            app_data_dir,
            bundle_id,
            product_name,
        })
    }

    fn emit(&self, reason: &str) {
        let _ = VerbatimServiceChanged {
            reason: reason.to_string(),
        }
        .emit(&self.app);
    }

    /// Runs `handy-worker status` (a few hundred ms; never imports the model).
    fn fetch_status(&self, env: &WorkerEnv) -> Result<WorkerStatus, String> {
        let out =
            worker::run_with_timeout(env.worker_command(&["status"]), Duration::from_secs(60))?;
        if out.code != Some(0) {
            return Err(format!(
                "status failed: {}",
                out.stderr.lines().last().unwrap_or("no output")
            ));
        }
        worker::parse_status(&out.stdout)
    }

    fn refresh(&self, env: &WorkerEnv) -> Option<Result<WorkerStatus, String>> {
        if !(env.exists() && env.is_synced()) {
            let mut st = lock(&self.state);
            st.status = None;
            st.status_at = Some(Instant::now());
            return None;
        }
        let status = self.fetch_status(env);
        let mut st = lock(&self.state);
        st.status = Some(status.clone());
        st.status_at = Some(Instant::now());
        Some(status)
    }

    fn run_in_progress(&self) -> bool {
        let mut st = lock(&self.state);
        match st.run_child.as_mut().map(|c| c.try_wait()) {
            Some(Ok(None)) => true,
            Some(_) => {
                st.run_child = None;
                false
            }
            None => false,
        }
    }

    pub fn status(&self, refresh: bool) -> VerbatimServiceStatus {
        let settings = self.settings();
        let env = self.env();
        let (health, status, error) = match &env {
            Err(e) => (WorkerHealth::Broken, None, Some(e.clone())),
            Ok(env) => {
                let stale = lock(&self.state)
                    .status_at
                    .is_none_or(|t| t.elapsed() > STATUS_TTL);
                let current = if refresh || stale {
                    self.refresh(env)
                } else {
                    lock(&self.state).status.clone()
                };
                let health = worker::classify(
                    env.exists(),
                    env.uv.is_some(),
                    env.is_synced(),
                    current.as_ref(),
                );
                match current {
                    Some(Ok(s)) => (health, Some(s), None),
                    Some(Err(e)) => (health, None, Some(e)),
                    None => (health, None, None),
                }
            }
        };
        let run_in_progress = self.run_in_progress();
        let st = lock(&self.state);
        VerbatimServiceStatus {
            platform_supported: cfg!(target_os = "macos"),
            enabled: settings.enabled,
            health,
            status,
            error,
            worker_dir: env
                .as_ref()
                .map(|e| e.worker_dir.display().to_string())
                .unwrap_or_default(),
            uv_path: env
                .as_ref()
                .ok()
                .and_then(|e| e.uv.as_ref().map(|p| p.display().to_string())),
            log_dir: env
                .as_ref()
                .map(|e| e.log_dir.display().to_string())
                .unwrap_or_default(),
            bundled_worker_version: worker::bundled_worker_version(),
            run_in_progress,
            operation: st.operation.clone(),
            last_operation_error: st.last_operation_error.clone(),
            user_idle_seconds: user_idle_seconds(),
        }
    }

    /// Writes `config.json` for the worker (it re-reads it every run).
    pub fn write_worker_config(&self, settings: &VerbatimServiceSettings) -> Result<(), String> {
        let dir = crate::portable::app_data_dir(&self.app).map_err(|e| e.to_string())?;
        let path = dir.join("verbatim").join("config.json");
        std::fs::create_dir_all(path.parent().unwrap_or(&dir)).map_err(|e| e.to_string())?;
        let tmp = path.with_extension("json.tmp");
        let json =
            serde_json::to_vec_pretty(&settings.worker_config()).map_err(|e| e.to_string())?;
        std::fs::write(&tmp, json).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, &path).map_err(|e| e.to_string())
    }

    /// Runs a long operation (sync/install/...) on its own thread, one at a
    /// time; the UI follows through `VerbatimServiceChanged`.
    fn spawn_operation(
        self: &Arc<Self>,
        name: &str,
        op: impl FnOnce(&Self, &WorkerEnv) -> Result<(), String> + Send + 'static,
    ) -> Result<(), String> {
        {
            let mut st = lock(&self.state);
            if let Some(current) = &st.operation {
                return Err(format!("Busy: {current}"));
            }
            st.operation = Some(name.to_string());
            st.last_operation_error = None;
        }
        self.emit("operation_started");
        let me = Arc::clone(self);
        let name = name.to_string();
        std::thread::Builder::new()
            .name(format!("verbatim-{name}"))
            .spawn(move || {
                let result = me.env().and_then(|env| op(&me, &env));
                if let Err(e) = &result {
                    log::warn!("Verbatim service: {name} failed: {e}");
                }
                if let Ok(env) = me.env() {
                    me.refresh(&env);
                }
                {
                    let mut st = lock(&me.state);
                    st.operation = None;
                    st.last_operation_error = result.err();
                }
                me.emit("operation_finished");
            })
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn run_worker(env: &WorkerEnv, args: &[&str], timeout: Duration) -> Result<String, String> {
        let out = worker::run_with_timeout(env.worker_command(args), timeout)?;
        if out.code == Some(0) {
            Ok(out.stdout)
        } else {
            let tail = out
                .stdout
                .lines()
                .chain(out.stderr.lines())
                .rfind(|l| !l.trim().is_empty())
                .unwrap_or("no output")
                .to_string();
            Err(format!("handy-worker {} failed: {tail}", args.join(" ")))
        }
    }

    /// `uv sync` + `mark-synced`: creates or updates the environment.
    fn sync(env: &WorkerEnv) -> Result<(), String> {
        let cmd = env
            .sync_command()
            .ok_or("uv was not found. Install it (https://docs.astral.sh/uv/) or set its path.")?;
        let out = worker::run_with_timeout(cmd, Duration::from_secs(1800))?;
        if out.code != Some(0) {
            return Err(format!(
                "uv sync failed: {}",
                out.stderr.lines().last().unwrap_or("no output")
            ));
        }
        Self::run_worker(
            env,
            &["mark-synced", "--extra", "model"],
            Duration::from_secs(60),
        )
        .map(|_| ())
    }

    fn install(env: &WorkerEnv) -> Result<(), String> {
        Self::run_worker(env, &["install"], Duration::from_secs(120)).map(|_| ())
    }

    fn uninstall(env: &WorkerEnv) -> Result<(), String> {
        if !env.is_synced() {
            return Ok(()); // nothing was ever installed through it
        }
        Self::run_worker(env, &["uninstall"], Duration::from_secs(60)).map(|_| ())
    }

    /// Applies new settings: stores them, writes the worker config, and
    /// installs/uninstalls/re-installs the agent as needed.
    pub fn apply_settings(
        self: &Arc<Self>,
        new: VerbatimServiceSettings,
    ) -> Result<VerbatimServiceSettings, String> {
        let new = new.sanitized();
        let mut settings = get_settings(&self.app);
        let old = settings.personal.verbatim_service.clone();
        settings.personal.verbatim_service = new.clone();
        write_settings(&self.app, settings);
        if let Err(e) = self.write_worker_config(&new) {
            log::warn!("Cannot write the worker config: {e}");
        }
        let turned_on = new.enabled && !old.enabled;
        let turned_off = !new.enabled && old.enabled;
        let reinstall = new.enabled && !turned_on && new.schedule_changed(&old);
        if turned_on || reinstall {
            self.spawn_operation("install", |_, env| {
                if !env.is_synced() {
                    Self::sync(env)?;
                }
                Self::install(env)
            })?;
        } else if turned_off {
            self.spawn_operation("uninstall", |_, env| Self::uninstall(env))?;
        } else {
            self.emit("settings");
        }
        Ok(new)
    }

    pub fn run_now(self: &Arc<Self>, trigger: &str) -> Result<(), String> {
        if self.run_in_progress() {
            return Err("A run is already in progress.".into());
        }
        let env = self.env()?;
        if !env.is_synced() {
            return Err("The worker is not set up yet (use Fix).".into());
        }
        let mut cmd = env.worker_command(&["run", "--trigger", trigger, "--quiet"]);
        cmd.stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
        let child = cmd
            .spawn()
            .map_err(|e| format!("cannot start the worker: {e}"))?;
        log::info!("Verbatim worker started ({trigger}), pid {}", child.id());
        lock(&self.state).run_child = Some(child);
        self.emit("run_started");
        Ok(())
    }

    pub fn retry(&self, entry_id: Option<i64>) -> Result<(), String> {
        let env = self.env()?;
        let id = entry_id.map(|i| i.to_string());
        let args: Vec<&str> = match &id {
            Some(id) => vec!["retry", id],
            None => vec!["retry", "--all-failed"],
        };
        Self::run_worker(&env, &args, Duration::from_secs(30))?;
        self.refresh(&env);
        self.emit("retry");
        Ok(())
    }

    /// One click for whatever is wrong: set up / update the environment,
    /// download the model (the one-time network access), (re)install.
    pub fn fix(self: &Arc<Self>) -> Result<(), String> {
        let enabled = self.settings().enabled;
        self.spawn_operation("fix", move |me, env| {
            if !env.exists() {
                return Err(format!(
                    "No worker at {}. Set the worker folder in settings.",
                    env.worker_dir.display()
                ));
            }
            let status = me.fetch_status(env).ok();
            let health = worker::classify(true, env.uv.is_some(), env.is_synced(), None);
            let needs_sync = health == WorkerHealth::NotSynced
                || status.as_ref().is_none_or(|s| {
                    worker::classify(true, true, true, Some(&Ok(s.clone())))
                        == WorkerHealth::OutOfDate
                });
            if needs_sync {
                Self::sync(env)?;
            }
            let status = me.fetch_status(env)?;
            if status.model.state != "ready" {
                Self::run_worker(env, &["model", "download"], Duration::from_secs(4 * 3600))?;
            }
            if enabled {
                Self::install(env)?;
            }
            Ok(())
        })
    }

    pub fn doctor(&self) -> Result<String, String> {
        let env = self.env()?;
        if !env.is_synced() {
            return Err("The worker environment is not set up yet (use Fix).".into());
        }
        let out =
            worker::run_with_timeout(env.worker_command(&["doctor"]), Duration::from_secs(300))?;
        Ok(format!("{}{}", out.stdout, out.stderr))
    }

    // -- background loop ----------------------------------------------------

    pub fn start(self: &Arc<Self>) {
        if let Err(e) = self.write_worker_config(&self.settings()) {
            log::debug!("Worker config not written yet: {e}");
        }
        let me = Arc::clone(self);
        let spawned = std::thread::Builder::new()
            .name("verbatim-service".into())
            .spawn(move || {
                // Let startup settle before the first status call.
                std::thread::sleep(Duration::from_secs(20));
                loop {
                    if let Err(e) =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| me.tick()))
                    {
                        log::error!("Verbatim service tick panicked: {e:?}");
                    }
                    std::thread::sleep(TICK);
                }
            });
        if let Err(e) = spawned {
            log::warn!("Verbatim service thread failed to start: {e}");
        }
    }

    fn handy_busy(&self) -> bool {
        crate::tray::dictation_in_progress(&self.app)
            || self
                .app
                .try_state::<Arc<crate::managers::transcription::TranscriptionManager>>()
                .is_some_and(|tm| tm.job_active())
    }

    fn tick(self: &Arc<Self>) {
        let settings = self.settings();
        if !settings.enabled {
            return;
        }
        // A run Handy started just finished → fresh status and metrics.
        let finished = {
            let mut st = lock(&self.state);
            match st.run_child.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(code))) => {
                    log::info!("Verbatim worker exited: {code}");
                    st.run_child = None;
                    st.status_at = None;
                    true
                }
                Some(Err(_)) => {
                    st.run_child = None;
                    true
                }
                _ => false,
            }
        };
        let status = self.status(false);
        let last_run_end = status
            .status
            .as_ref()
            .and_then(|s| s.last_run.as_ref())
            .and_then(|r| r.finished_at);
        if finished || self.metrics_stale(last_run_end) {
            self.refresh_metrics();
            self.emit("run_finished");
        }

        self.maybe_notify(&status);
        self.maybe_auto_update(&settings, &status);

        let worker_running =
            status.run_in_progress || status.status.as_ref().is_some_and(|s| s.running.is_some());
        let busy = self.handy_busy();
        let since = lock(&self.state).last_idle_trigger.map(|t| t.elapsed());
        if status.health == WorkerHealth::Ok
            && should_trigger_idle(
                &settings,
                status.user_idle_seconds,
                status.status.as_ref().and_then(|s| s.queue.pending),
                worker_running,
                busy,
                since,
            )
        {
            lock(&self.state).last_idle_trigger = Some(Instant::now());
            if let Err(e) = self.run_now("idle") {
                log::warn!("Idle trigger failed: {e}");
            }
        }

        if !worker_running && !busy {
            self.maybe_retention(&settings);
        }
        self.maybe_coaching(&settings);
    }

    fn metrics_stale(&self, last_run_end: Option<i64>) -> bool {
        let Some(end) = last_run_end else {
            return false;
        };
        let Ok(dir) = crate::portable::app_data_dir(&self.app) else {
            return false;
        };
        let marker = dir.join("verbatim").join(".metrics-refreshed-at");
        let done: i64 = std::fs::read_to_string(&marker)
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .unwrap_or(0);
        end > done
    }

    /// Recomputes and stores the speech metrics (verbatim where available).
    fn refresh_metrics(&self) {
        let Ok(dir) = crate::portable::app_data_dir(&self.app) else {
            return;
        };
        match super::insights::build_report(&dir, None) {
            Ok(_) => {
                let marker = dir.join("verbatim").join(".metrics-refreshed-at");
                let _ = std::fs::write(marker, chrono::Utc::now().timestamp().to_string());
                log::info!("Speech metrics refreshed after a verbatim run");
            }
            Err(e) => log::warn!("Speech metrics refresh failed: {e}"),
        }
    }

    fn maybe_notify(&self, status: &VerbatimServiceStatus) {
        let problem = match status.health {
            WorkerHealth::Missing => Some("The verbatim worker is missing."),
            WorkerHealth::NoUv => Some("uv was not found; the worker can't be set up."),
            WorkerHealth::NotSynced => Some("The verbatim worker isn't set up yet."),
            WorkerHealth::Broken => Some("The verbatim worker is broken."),
            _ => None,
        };
        let mut st = lock(&self.state);
        match problem {
            Some(p) if st.notified.as_deref() != Some(p) => {
                st.notified = Some(p.to_string());
                drop(st);
                super::notice::emit(
                    &self.app,
                    super::notice::PersonalNoticeKind::VerbatimServiceAttention,
                    Some(p.to_string()),
                );
            }
            None => st.notified = None,
            _ => {}
        }
    }

    fn maybe_auto_update(
        self: &Arc<Self>,
        settings: &VerbatimServiceSettings,
        status: &VerbatimServiceStatus,
    ) {
        if !settings.auto_update
            || status.health != WorkerHealth::OutOfDate
            || status.operation.is_some()
        {
            return;
        }
        // Once per checkout state, so a failing sync doesn't loop.
        let key = status
            .status
            .as_ref()
            .map(|s| {
                format!(
                    "{}:{:?}",
                    s.versions.worker_version, s.versions.source_version
                )
            })
            .unwrap_or_default();
        {
            let mut st = lock(&self.state);
            if st.auto_sync_attempted_for.as_deref() == Some(key.as_str()) {
                return;
            }
            st.auto_sync_attempted_for = Some(key);
        }
        log::info!("Verbatim worker is out of date; re-syncing its environment");
        let _ = self.spawn_operation("update", |_, env| {
            Self::sync(env)?;
            Self::install(env)
        });
    }

    fn maybe_retention(&self, settings: &VerbatimServiceSettings) {
        if !settings.retention.enabled {
            return;
        }
        let today = chrono::Local::now().date_naive();
        {
            let mut st = lock(&self.state);
            if st.last_retention_day == Some(today) {
                return;
            }
            st.last_retention_day = Some(today);
        }
        let Ok(dir) = crate::portable::app_data_dir(&self.app) else {
            return;
        };
        let app = self.app.clone();
        let keep_going = move || {
            !crate::tray::dictation_in_progress(&app)
                && !app
                    .try_state::<Arc<crate::managers::transcription::TranscriptionManager>>()
                    .is_some_and(|tm| tm.job_active())
        };
        match super::retention::apply(
            &dir,
            &settings.retention,
            chrono::Utc::now().timestamp(),
            &keep_going,
        ) {
            Ok(report) => {
                if report.compressed + report.deleted + report.failed > 0 {
                    log::info!(
                        "Retention: compressed {}, deleted {}, failed {}, freed {:.0} MB",
                        report.compressed,
                        report.deleted,
                        report.failed,
                        report.bytes_freed / 1e6
                    );
                }
                self.announce_history_changes(&report.history_ids_changed);
            }
            Err(e) => log::warn!("Retention pass failed: {e:#}"),
        }
    }

    fn announce_history_changes(&self, ids: &[i64]) {
        use crate::managers::history::{HistoryManager, HistoryUpdatePayload};
        let Some(hm) = self.app.try_state::<Arc<HistoryManager>>() else {
            return;
        };
        for id in ids {
            if let Ok(Some(entry)) = tauri::async_runtime::block_on(hm.get_entry_by_id(*id)) {
                let _ = HistoryUpdatePayload::Updated { entry }.emit(&self.app);
            }
        }
    }

    fn maybe_coaching(&self, settings: &VerbatimServiceSettings) {
        if !settings.coaching_weekly {
            return;
        }
        let Ok(dir) = crate::portable::app_data_dir(&self.app) else {
            return;
        };
        let last_week = previous_iso_week(chrono::Local::now().date_naive());
        let notes = load_coaching_notes(&dir);
        if notes.iter().any(|n| n.week == last_week) {
            return;
        }
        {
            let mut st = lock(&self.state);
            if st
                .last_coaching_attempt
                .is_some_and(|t| t.elapsed() < COACHING_RETRY)
            {
                return;
            }
            st.last_coaching_attempt = Some(Instant::now());
        }
        let app = self.app.clone();
        tauri::async_runtime::spawn(async move {
            match super::insights::coaching_note(&app, true).await {
                Ok((week, text)) => {
                    if let Err(e) = save_coaching_note(&dir, &week, &text) {
                        log::warn!("Cannot save the coaching note: {e}");
                    }
                    let _ = VerbatimServiceChanged {
                        reason: "coaching_note".into(),
                    }
                    .emit(&app);
                }
                // LM Studio not running etc.: try again later, quietly.
                Err(e) => log::info!("Weekly coaching note not generated yet: {e}"),
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Coaching notes (stored locally)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Serialize, Deserialize, Type, PartialEq)]
pub struct CoachingNote {
    /// ISO week the note is about (`YYYY-Www`).
    pub week: String,
    pub created_at: i64,
    pub text: String,
}

pub fn previous_iso_week(today: chrono::NaiveDate) -> String {
    use chrono::Datelike;
    let w = (today - chrono::Duration::days(7)).iso_week();
    format!("{}-W{:02}", w.year(), w.week())
}

fn notes_path(app_data_dir: &std::path::Path) -> PathBuf {
    app_data_dir.join("coaching").join("notes.json")
}

pub fn load_coaching_notes(app_data_dir: &std::path::Path) -> Vec<CoachingNote> {
    std::fs::read(notes_path(app_data_dir))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

pub fn save_coaching_note(
    app_data_dir: &std::path::Path,
    week: &str,
    text: &str,
) -> Result<(), String> {
    let mut notes = load_coaching_notes(app_data_dir);
    notes.retain(|n| n.week != week);
    notes.push(CoachingNote {
        week: week.to_string(),
        created_at: chrono::Utc::now().timestamp(),
        text: text.to_string(),
    });
    notes.sort_by(|a, b| b.week.cmp(&a.week));
    notes.truncate(52);
    let path = notes_path(app_data_dir);
    std::fs::create_dir_all(path.parent().unwrap_or(app_data_dir)).map_err(|e| e.to_string())?;
    std::fs::write(
        &path,
        serde_json::to_vec_pretty(&notes).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

fn service(app: &AppHandle) -> Result<Arc<VerbatimService>, String> {
    app.try_state::<Arc<VerbatimService>>()
        .map(|s| Arc::clone(&s))
        .ok_or_else(|| "The verbatim service is not running.".to_string())
}

async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
#[specta::specta]
pub async fn get_verbatim_service_status(
    app: AppHandle,
    refresh: bool,
) -> Result<VerbatimServiceStatus, String> {
    let svc = service(&app)?;
    blocking(move || Ok(svc.status(refresh))).await
}

#[tauri::command]
#[specta::specta]
pub async fn update_verbatim_service_settings(
    app: AppHandle,
    settings: VerbatimServiceSettings,
) -> Result<VerbatimServiceSettings, String> {
    let svc = service(&app)?;
    blocking(move || svc.apply_settings(settings)).await
}

#[tauri::command]
#[specta::specta]
pub async fn verbatim_run_now(app: AppHandle) -> Result<(), String> {
    let svc = service(&app)?;
    blocking(move || svc.run_now("manual")).await
}

#[tauri::command]
#[specta::specta]
pub async fn verbatim_retry(app: AppHandle, entry_id: Option<i64>) -> Result<(), String> {
    let svc = service(&app)?;
    blocking(move || svc.retry(entry_id)).await
}

#[tauri::command]
#[specta::specta]
pub async fn verbatim_fix(app: AppHandle) -> Result<(), String> {
    let svc = service(&app)?;
    blocking(move || svc.fix()).await
}

#[tauri::command]
#[specta::specta]
pub async fn verbatim_doctor(app: AppHandle) -> Result<String, String> {
    let svc = service(&app)?;
    blocking(move || svc.doctor()).await
}

#[tauri::command]
#[specta::specta]
pub async fn verbatim_reveal_logs(app: AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    let svc = service(&app)?;
    let dir = svc.env()?.log_dir;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    app.opener()
        .open_path(dir.to_string_lossy(), None::<String>)
        .map_err(|e| e.to_string())
}

#[tauri::command]
#[specta::specta]
pub async fn retention_preview(
    app: AppHandle,
    settings: RetentionSettings,
) -> Result<RetentionPreview, String> {
    blocking(move || {
        let dir = crate::portable::app_data_dir(&app).map_err(|e| e.to_string())?;
        super::retention::preview(&dir, &settings, chrono::Utc::now().timestamp())
            .map_err(|e| format!("{e:#}"))
    })
    .await
}

#[tauri::command]
#[specta::specta]
pub async fn get_coaching_notes(app: AppHandle) -> Result<Vec<CoachingNote>, String> {
    let dir = crate::portable::app_data_dir(&app).map_err(|e| e.to_string())?;
    Ok(load_coaching_notes(&dir))
}

/// Hooked from `lib.rs` after the core managers exist.
pub fn init(app: &AppHandle) {
    super::busy::init(app);
    let svc = VerbatimService::new(app);
    app.manage(Arc::clone(&svc));
    svc.start();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn on() -> VerbatimServiceSettings {
        VerbatimServiceSettings {
            enabled: true,
            ..Default::default()
        }
    }

    #[test]
    fn defaults_match_the_spec() {
        let s = VerbatimServiceSettings::default();
        assert!(!s.enabled);
        assert_eq!((s.schedule_hour, s.schedule_minute), (2, 0));
        assert_eq!((s.stop_by_hour, s.stop_by_minute), (7, 0));
        assert_eq!(s.idle_minutes, 20);
        assert!(s.skip_on_battery && s.time_budget_enabled && s.idle_trigger_enabled);
        assert!(!s.retention.enabled);
        assert_eq!(
            (
                s.retention.compress_after_days,
                s.retention.delete_after_days
            ),
            (30, 180)
        );
        assert_eq!(s.coaching_provider_id, "lmstudio");
        let parsed: VerbatimServiceSettings = serde_json::from_str("{}").unwrap();
        assert_eq!(parsed, s);
    }

    #[test]
    fn sanitizes_out_of_range_values() {
        let s = VerbatimServiceSettings {
            schedule_hour: 30,
            schedule_minute: 99,
            idle_minutes: 0,
            retention: RetentionSettings {
                enabled: true,
                compress_after_days: 400,
                delete_after_days: 180,
            },
            ..Default::default()
        }
        .sanitized();
        assert_eq!(
            (s.schedule_hour, s.schedule_minute, s.idle_minutes),
            (23, 59, 5)
        );
        assert_eq!(s.retention.compress_after_days, 180);
    }

    #[test]
    fn worker_config_carries_the_run_rules() {
        let s = VerbatimServiceSettings {
            schedule_hour: 3,
            skip_on_battery: false,
            stop_by_hour: 6,
            ..Default::default()
        };
        let v = s.worker_config();
        assert_eq!(v["schedule_hour"], 3);
        assert_eq!(v["skip_on_battery"], false);
        assert_eq!(v["stop_by_hour"], 6);
        assert_eq!(v["time_budget_enabled"], true);
    }

    #[test]
    fn idle_trigger_rules() {
        let idle = Some(25.0 * 60.0);
        assert!(should_trigger_idle(
            &on(),
            idle,
            Some(3),
            false,
            false,
            None
        ));
        // Not idle long enough.
        assert!(!should_trigger_idle(
            &on(),
            Some(60.0),
            Some(3),
            false,
            false,
            None
        ));
        // Empty queue, worker already running, Handy busy, disabled.
        assert!(!should_trigger_idle(
            &on(),
            idle,
            Some(0),
            false,
            false,
            None
        ));
        assert!(!should_trigger_idle(&on(), idle, None, false, false, None));
        assert!(!should_trigger_idle(
            &on(),
            idle,
            Some(3),
            true,
            false,
            None
        ));
        assert!(!should_trigger_idle(
            &on(),
            idle,
            Some(3),
            false,
            true,
            None
        ));
        assert!(!should_trigger_idle(
            &VerbatimServiceSettings::default(),
            idle,
            Some(3),
            false,
            false,
            None
        ));
        let off = VerbatimServiceSettings {
            idle_trigger_enabled: false,
            ..on()
        };
        assert!(!should_trigger_idle(
            &off,
            idle,
            Some(3),
            false,
            false,
            None
        ));
        // Not retriggered within 30 minutes.
        assert!(!should_trigger_idle(
            &on(),
            idle,
            Some(3),
            false,
            false,
            Some(Duration::from_secs(60))
        ));
        // No idle information (not macOS): never.
        assert!(!should_trigger_idle(
            &on(),
            None,
            Some(3),
            false,
            false,
            None
        ));
    }

    #[test]
    fn schedule_changes_require_a_reinstall() {
        let a = on();
        let b = VerbatimServiceSettings {
            schedule_hour: 4,
            ..on()
        };
        assert!(a.schedule_changed(&b));
        let c = VerbatimServiceSettings {
            idle_minutes: 30,
            ..on()
        };
        assert!(!a.schedule_changed(&c));
    }

    #[test]
    fn coaching_notes_are_stored_per_week() {
        let dir = tempfile::tempdir().unwrap();
        assert!(load_coaching_notes(dir.path()).is_empty());
        save_coaching_note(dir.path(), "2026-W43", "first").unwrap();
        save_coaching_note(dir.path(), "2026-W44", "second").unwrap();
        save_coaching_note(dir.path(), "2026-W44", "again").unwrap();
        let notes = load_coaching_notes(dir.path());
        assert_eq!(notes.len(), 2);
        assert_eq!(notes[0].week, "2026-W44");
        assert_eq!(notes[0].text, "again");
        let d = chrono::NaiveDate::from_ymd_opt(2026, 11, 2).unwrap(); // Monday W45
        assert_eq!(previous_iso_week(d), "2026-W44");
    }
}
