//! Fork-only settings, nested under `AppSettings::personal` so upstream's
//! settings struct only gains a single field.

use serde::{Deserialize, Serialize};
use specta::Type;
use tauri::AppHandle;

use crate::settings::{get_settings, write_settings};

/// Container-level `serde(default)`: any field missing from a stored settings
/// object falls back to its default, so adding fields never breaks a load.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Type)]
#[serde(default)]
pub struct PersonalSettings {
    /// Refuse to paste while a secure (password) text field is focused, or
    /// while macOS Secure Event Input is on. The transcript stays in History
    /// and a notice explains why nothing was pasted.
    pub block_paste_in_secure_fields: bool,
    /// Log every dictation (raw ASR text, cleanup stages, timings, VAD
    /// segments, frontmost app) to the dataset table and keep its audio.
    pub dataset_logging_enabled: bool,
    /// Prompt Whisper with fillers so disfluencies survive in the raw
    /// transcript (see `personal::verbatim`).
    pub verbatim_mode: bool,
    /// The verbatim initial prompt; empty uses the built-in default.
    pub verbatim_prompt: String,
    /// Budget for LLM post-processing (health check + call). Past it, the
    /// rule-cleaned text is pasted instead.
    pub llm_timeout_ms: u64,
    /// Per-app behaviour keyed on the frontmost app's bundle id.
    pub app_profiles: Vec<super::profiles::AppProfile>,
}

impl Default for PersonalSettings {
    fn default() -> Self {
        Self {
            block_paste_in_secure_fields: true,
            dataset_logging_enabled: true,
            verbatim_mode: false,
            verbatim_prompt: super::verbatim::DEFAULT_VERBATIM_PROMPT.to_string(),
            llm_timeout_ms: super::lmstudio::DEFAULT_LLM_TIMEOUT_MS,
            app_profiles: super::profiles::default_profiles(),
        }
    }
}

/// Replaces the fork's settings block in one go.
#[tauri::command]
#[specta::specta]
pub fn update_personal_settings(
    app: AppHandle,
    personal: PersonalSettings,
) -> Result<PersonalSettings, String> {
    let mut settings = get_settings(&app);
    settings.personal = personal;
    let stored = settings.personal.clone();
    write_settings(&app, settings);
    Ok(stored)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_fields_fall_back_to_defaults() {
        let parsed: PersonalSettings = serde_json::from_str("{}").unwrap();
        assert_eq!(parsed, PersonalSettings::default());
    }
}
