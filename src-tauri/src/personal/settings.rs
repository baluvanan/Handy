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
}

impl Default for PersonalSettings {
    fn default() -> Self {
        Self {
            block_paste_in_secure_fields: true,
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
