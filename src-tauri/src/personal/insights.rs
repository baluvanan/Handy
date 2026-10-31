//! Tauri commands for the Insights page, and the weekly coaching note.
//!
//! The coaching note sends **aggregated metrics only** (rates, counts, which
//! fixed filler/hedge words occurred) to the coaching LLM — the local LM
//! Studio provider by default. Transcript text never leaves the machine;
//! `coaching_payload` is unit tested for that.
//!
//! Reports prefer verbatim transcripts from the background worker and keep
//! stored per-period results with their metric definition versions
//! (`metric_snapshots`).

use std::time::Duration;

use chrono::Datelike;
use serde::Serialize;
use tauri::AppHandle;

use super::analysis::{report_with, Aggregate, AnalysisContext, InsightsReport, PeriodAggregate};
use super::dataset::DatasetStore;

/// Builds the report from the dictation log plus verbatim transcripts, and
/// reconciles it with stored results. Works without the worker (no verbatim
/// data → raw text plus estimates).
pub fn build_report(
    app_data_dir: &std::path::Path,
    since: Option<i64>,
) -> Result<InsightsReport, String> {
    let store = DatasetStore::open(app_data_dir).map_err(|e| format!("{e:#}"))?;
    // Calibrate on everything, report on the requested range.
    let all = store.records(None).map_err(|e| format!("{e:#}"))?;
    let verbatim = super::verbatim_store::load(app_data_dir).unwrap_or_else(|e| {
        log::warn!("Verbatim transcripts unavailable: {e:#}");
        Default::default()
    });
    let ctx = AnalysisContext::new(&all, verbatim);
    let records: Vec<_> = all
        .into_iter()
        .filter(|r| since.is_none_or(|s| r.created_at >= s))
        .collect();
    let mut report = report_with(&records, since, &ctx);
    match super::metric_snapshots::open(&super::metric_snapshots::db_path(app_data_dir)) {
        // Partial ranges cut weeks short; only stabilize full-history runs.
        Ok(conn) if since.is_none() => {
            if let Err(e) = super::metric_snapshots::stabilize(&conn, &mut report) {
                log::warn!("Metric snapshots: {e:#}");
            }
        }
        Ok(conn) => {
            // Use stored (frozen) results for the periods in range too.
            if let Err(e) = super::metric_snapshots::stabilize_read_only(&conn, &mut report) {
                log::warn!("Metric snapshots: {e:#}");
            }
        }
        Err(e) => log::warn!("Metric snapshots unavailable: {e:#}"),
    }
    Ok(report)
}

fn load_report(app: &AppHandle, since: Option<i64>) -> Result<InsightsReport, String> {
    let dir = crate::portable::app_data_dir(app).map_err(|e| e.to_string())?;
    build_report(&dir, since)
}

/// Recomputes every stored period with the current metric definitions
/// (removes "definitions changed" markers).
#[tauri::command]
#[specta::specta]
pub async fn recompute_speech_metrics(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let dir = crate::portable::app_data_dir(&app).map_err(|e| e.to_string())?;
        let conn = super::metric_snapshots::open(&super::metric_snapshots::db_path(&dir))
            .map_err(|e| format!("{e:#}"))?;
        super::metric_snapshots::clear(&conn).map_err(|e| format!("{e:#}"))?;
        build_report(&dir, None).map(|_| ())
    })
    .await
    .map_err(|e| e.to_string())?
}

fn since_days_ago(days: u32) -> i64 {
    let midnight = chrono::Local::now()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap_or_default();
    let midnight = chrono::TimeZone::from_local_datetime(&chrono::Local, &midnight)
        .earliest()
        .map(|d| d.timestamp())
        .unwrap_or_else(|| chrono::Utc::now().timestamp());
    midnight - i64::from(days) * 86_400
}

/// Speech metrics per day and week for the Insights page. `since_days`
/// limits to the last N days (all time when `None`).
#[tauri::command]
#[specta::specta]
pub async fn get_speech_insights(
    app: AppHandle,
    since_days: Option<u32>,
) -> Result<InsightsReport, String> {
    let since = since_days.map(since_days_ago);
    tauri::async_runtime::spawn_blocking(move || load_report(&app, since))
        .await
        .map_err(|e| e.to_string())?
}

#[derive(Serialize)]
struct CoachingWeek<'a> {
    week: &'a str,
    metrics: &'a Aggregate,
}

pub const COACHING_SYSTEM_PROMPT: &str = "You are a warm, practical speaking coach. \
You receive weekly aggregate speech metrics from someone's own dictation (no transcripts). \
`fillers` are counted from verbatim transcripts where available; `estimated_fillers` is a \
rough estimate for the rest, so treat it as uncertain. \
Write a short, encouraging note (under 150 words): one sentence on what went well, \
then 2-3 concrete, specific things to practise this week, tied to the numbers \
(fillers per minute and which fillers, hedges, pauses, false starts, words per minute, \
vocabulary variety). Compare with the previous week when given. \
Do not invent data. Plain text, no headings.";

/// The exact JSON sent to the LLM: the most recent week and the one before.
pub fn coaching_payload(report: &InsightsReport) -> Option<String> {
    let weeks: Vec<&PeriodAggregate> = report.weeks.iter().rev().take(2).collect();
    let current = weeks.first()?;
    let mut payload = vec![CoachingWeek {
        week: &current.period,
        metrics: &current.metrics,
    }];
    if let Some(previous) = weeks.get(1) {
        payload.push(CoachingWeek {
            week: &previous.period,
            metrics: &previous.metrics,
        });
    }
    serde_json::to_string_pretty(&serde_json::json!({
        "this_week": payload[0],
        "previous_week": payload.get(1),
        "notes": "Rates are per minute of detected speech. Pauses shorter than ~0.5 s are not detected. \
basis.verbatim = dictations measured from verbatim transcripts; basis.asr_only = from the \
dictation model's text, whose dropped fillers are only estimated (estimated_fillers).",
    }))
    .ok()
}

/// The coaching LLM: the one chosen in the verbatim settings, LM Studio (local)
/// by default; the active post-processing provider only if neither exists.
pub fn coaching_provider(
    settings: &crate::settings::AppSettings,
) -> Option<crate::settings::PostProcessProvider> {
    let wanted = settings
        .personal
        .verbatim_service
        .coaching_provider_id
        .as_str();
    settings
        .post_process_providers
        .iter()
        .find(|p| p.id == wanted)
        .or_else(|| {
            settings
                .post_process_providers
                .iter()
                .find(|p| p.id == super::lmstudio::PROVIDER_ID)
        })
        .or_else(|| settings.active_post_process_provider())
        .cloned()
}

/// Asks the coaching LLM (local LM Studio by default) for a short coaching
/// note based on the last two weeks of aggregate metrics.
#[tauri::command]
#[specta::specta]
pub async fn generate_coaching_note(app: AppHandle) -> Result<String, String> {
    coaching_note(&app, false).await.map(|(_, text)| text)
}

/// Builds and sends the coaching request. With `complete_weeks_only` the
/// current (unfinished) ISO week is left out (the scheduled weekly note).
/// Returns the week the note is about and the note.
pub async fn coaching_note(
    app: &AppHandle,
    complete_weeks_only: bool,
) -> Result<(String, String), String> {
    let since = Some(since_days_ago(if complete_weeks_only { 21 } else { 14 }));
    let app_for_report = app.clone();
    let mut report =
        tauri::async_runtime::spawn_blocking(move || load_report(&app_for_report, since))
            .await
            .map_err(|e| e.to_string())??;
    if complete_weeks_only {
        let today = chrono::Local::now().date_naive().iso_week();
        let current = format!("{}-W{:02}", today.year(), today.week());
        report.weeks.retain(|w| w.period != current);
    }
    let week = report
        .weeks
        .last()
        .map(|w| w.period.clone())
        .ok_or("No dictations in the last two weeks yet.".to_string())?;
    let payload =
        coaching_payload(&report).ok_or("No dictations in the last two weeks yet.".to_string())?;

    let settings = crate::settings::get_settings(app);
    let provider = coaching_provider(&settings).ok_or("No coaching LLM provider is available.")?;
    let model = settings
        .post_process_models
        .get(&provider.id)
        .cloned()
        .unwrap_or_default();
    if model.trim().is_empty() {
        return Err(format!("Choose a model for {} first.", provider.label));
    }
    let api_key = settings
        .post_process_api_keys
        .get(&provider.id)
        .cloned()
        .unwrap_or_default();
    let prompt = format!("{COACHING_SYSTEM_PROMPT}\n\nMetrics (JSON):\n{payload}");
    // A note is not latency-critical; still never hang forever.
    let budget = Duration::from_millis(settings.personal.llm_timeout_ms.max(60_000));
    let call = crate::llm_client::send_chat_completion(&provider, api_key, &model, prompt, true);
    match tokio::time::timeout(budget, call).await {
        Ok(Ok(Some(text))) if !text.trim().is_empty() => Ok((week, text.trim().to_string())),
        Ok(Ok(_)) => Err("The LLM returned no text.".into()),
        Ok(Err(e)) => Err(e),
        Err(_) => Err(format!("{} did not answer in time.", provider.label)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::personal::analysis::report;
    use crate::personal::dataset::DictationRecord;
    use crate::personal::timeline::Segment;

    #[test]
    fn coaching_payload_never_contains_transcript_text() {
        let secret = "my bank PIN is four four two nine";
        let records: Vec<DictationRecord> = (0..3)
            .map(|i| DictationRecord {
                created_at: 1_700_000_000 + i * 86_400 * 4,
                raw_asr_text: format!("um, {secret}, you know, I think"),
                final_text: secret.into(),
                rule_cleaned_text: secret.into(),
                vad_segments: vec![Segment {
                    start_ms: 0,
                    end_ms: 5000,
                }],
                ..Default::default()
            })
            .collect();
        let rep = report(&records, None);
        let payload = coaching_payload(&rep).unwrap();
        for word in ["bank", "PIN", "four", "nine", "two"] {
            assert!(
                !payload.contains(word),
                "payload leaked '{word}': {payload}"
            );
        }
        assert!(payload.contains("this_week"));
        assert!(payload.contains("fillers_by_word"));
    }

    #[test]
    fn no_weeks_means_no_payload() {
        assert!(coaching_payload(&InsightsReport::default()).is_none());
    }
}
