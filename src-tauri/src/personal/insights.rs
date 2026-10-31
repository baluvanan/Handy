//! Tauri commands for the Insights page, and the weekly coaching note.
//!
//! The coaching note sends **aggregated metrics only** (rates, counts, which
//! fixed filler/hedge words occurred) to the configured post-processing LLM.
//! Transcript text never leaves the machine; `coaching_payload` is unit
//! tested for that.

use std::time::Duration;

use serde::Serialize;
use tauri::AppHandle;

use super::analysis::{report, Aggregate, InsightsReport, PeriodAggregate};
use super::dataset::DatasetStore;

fn load_report(app: &AppHandle, since: Option<i64>) -> Result<InsightsReport, String> {
    let dir = crate::portable::app_data_dir(app).map_err(|e| e.to_string())?;
    let store = DatasetStore::open(&dir).map_err(|e| format!("{e:#}"))?;
    let records = store.records(since).map_err(|e| format!("{e:#}"))?;
    Ok(report(&records, since))
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
        "notes": "Rates are per minute of detected speech. Pauses shorter than ~0.5 s are not detected.",
    }))
    .ok()
}

/// Asks the configured post-processing LLM for a short coaching note based
/// on the last two weeks of aggregate metrics.
#[tauri::command]
#[specta::specta]
pub async fn generate_coaching_note(app: AppHandle) -> Result<String, String> {
    let since = Some(since_days_ago(14));
    let app_for_report = app.clone();
    let report = tauri::async_runtime::spawn_blocking(move || load_report(&app_for_report, since))
        .await
        .map_err(|e| e.to_string())??;
    let payload =
        coaching_payload(&report).ok_or("No dictations in the last two weeks yet.".to_string())?;

    let settings = crate::settings::get_settings(&app);
    let provider = settings
        .active_post_process_provider()
        .cloned()
        .ok_or("No post-processing provider is selected.")?;
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
        Ok(Ok(Some(text))) if !text.trim().is_empty() => Ok(text.trim().to_string()),
        Ok(Ok(_)) => Err("The LLM returned no text.".into()),
        Ok(Err(e)) => Err(e),
        Err(_) => Err(format!("{} did not answer in time.", provider.label)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
