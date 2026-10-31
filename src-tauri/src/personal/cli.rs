//! Fork-only CLI flags, flattened into upstream's `CliArgs`:
//!
//! - `--export-dataset <DIR> [--since <DATE>]` writes `DIR/dataset.jsonl`.
//!
//! These run headlessly (no window, tray or microphone) like
//! `--transcribe-file`, print to stdout and exit.

use std::path::PathBuf;

use chrono::{Local, NaiveDate, TimeZone};
use clap::Args;
use tauri::AppHandle;

use super::dataset::{export_jsonl, DatasetStore};

#[derive(Args, Debug, Clone, Default)]
pub struct PersonalCliArgs {
    /// Export every logged dictation (raw/cleaned/LLM/final text, timings,
    /// VAD segments, app, audio path) as JSONL into this directory and exit.
    #[arg(long, value_name = "DIR")]
    pub export_dataset: Option<PathBuf>,

    /// Only include dictations on or after this date: YYYY-MM-DD (local
    /// time) or a relative span such as 7d / 4w.
    #[arg(long, value_name = "DATE")]
    pub since: Option<String>,
}

impl PersonalCliArgs {
    /// True when one of these flags asks for a headless one-shot run.
    pub fn is_headless(&self) -> bool {
        self.export_dataset.is_some()
    }
}

/// Parses `--since` into unix seconds. `now` is injectable for tests.
pub fn parse_since(value: &str, now: chrono::DateTime<Local>) -> Result<i64, String> {
    let value = value.trim();
    if let Some((num, unit)) = value
        .strip_suffix('d')
        .map(|n| (n, 1))
        .or_else(|| value.strip_suffix('w').map(|n| (n, 7)))
    {
        if let Ok(n) = num.parse::<i64>() {
            let midnight = now
                .date_naive()
                .and_hms_opt(0, 0, 0)
                .and_then(|t| Local.from_local_datetime(&t).earliest())
                .ok_or("cannot resolve local midnight")?;
            return Ok((midnight - chrono::Duration::days(n * unit)).timestamp());
        }
    }
    let date = NaiveDate::parse_from_str(value, "%Y-%m-%d")
        .map_err(|_| format!("invalid --since '{value}': use YYYY-MM-DD or e.g. 7d / 4w"))?;
    date.and_hms_opt(0, 0, 0)
        .and_then(|t| Local.from_local_datetime(&t).earliest())
        .map(|t| t.timestamp())
        .ok_or_else(|| format!("invalid --since '{value}'"))
}

fn open_store(app: &AppHandle) -> Result<DatasetStore, String> {
    let dir = crate::portable::app_data_dir(app).map_err(|e| e.to_string())?;
    DatasetStore::open(&dir).map_err(|e| format!("{e:#}"))
}

/// Runs the requested command. Returns a process exit code (0 ok, 1 runtime
/// failure, 2 bad usage).
pub fn run(app: &AppHandle, args: &PersonalCliArgs) -> i32 {
    let since = match args.since.as_deref().map(|s| parse_since(s, Local::now())) {
        Some(Err(e)) => {
            eprintln!("error: {e}");
            return 2;
        }
        Some(Ok(ts)) => Some(ts),
        None => None,
    };
    let store = match open_store(app) {
        Ok(store) => store,
        Err(e) => {
            eprintln!("error: cannot open the dataset: {e}");
            return 1;
        }
    };

    if let Some(dir) = &args.export_dataset {
        match export_jsonl(&store, dir, since) {
            Ok((path, n)) => {
                let total = store.count().unwrap_or(0);
                println!("Exported {n} of {total} dictation(s) to {}", path.display());
                println!("Audio: {}", store.audio_dir().display());
            }
            Err(e) => {
                eprintln!("error: export failed: {e:#}");
                return 1;
            }
        }
    }
    0
}

/// Runs [`run`] on a worker thread and exits the process with its code.
pub fn spawn_and_exit(app: AppHandle, args: PersonalCliArgs) {
    std::thread::spawn(move || {
        let code = run(&app, &args);
        use std::io::Write;
        let _ = std::io::stdout().flush();
        let _ = std::io::stderr().flush();
        std::process::exit(code);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn noon(y: i32, m: u32, d: u32) -> chrono::DateTime<Local> {
        Local.with_ymd_and_hms(y, m, d, 12, 0, 0).unwrap()
    }

    fn midnight(y: i32, m: u32, d: u32) -> i64 {
        Local
            .with_ymd_and_hms(y, m, d, 0, 0, 0)
            .unwrap()
            .timestamp()
    }

    #[test]
    fn parses_dates_and_relative_spans() {
        let now = noon(2026, 10, 31);
        assert_eq!(
            parse_since("2026-10-01", now).unwrap(),
            midnight(2026, 10, 1)
        );
        assert_eq!(parse_since("7d", now).unwrap(), midnight(2026, 10, 24));
        assert_eq!(parse_since("2w", now).unwrap(), midnight(2026, 10, 17));
        assert!(parse_since("yesterday", now).is_err());
        assert!(parse_since("2026-13-01", now).is_err());
    }
}
