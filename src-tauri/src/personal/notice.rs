//! Small, non-blocking notices.
//!
//! Always emitted as a typed event (rendered as a toast by
//! `src/components/personal/PersonalNotices.tsx` when the main window is
//! open). Handy usually runs with its window hidden, so on macOS the notice is
//! also posted as a system notification when the main window is not visible.
//! Notifications are rate-limited per kind so an LLM that stays down does not
//! spam one per dictation.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use specta::Type;
use tauri::{AppHandle, Manager};
use tauri_specta::Event;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize, Type)]
#[serde(rename_all = "snake_case")]
pub enum PersonalNoticeKind {
    /// Nothing was pasted because a password field had focus.
    PasteBlockedSecureField,
    /// The cleanup LLM was unreachable or too slow; rule-cleaned text was
    /// pasted instead.
    LlmFallback,
}

impl PersonalNoticeKind {
    /// English text for the system notification (the in-app toast is
    /// localized on the frontend).
    fn system_text(self) -> (&'static str, &'static str) {
        match self {
            Self::PasteBlockedSecureField => (
                "Not pasted into a password field",
                "The transcript is in Handy's History.",
            ),
            Self::LlmFallback => (
                "Cleanup LLM unavailable",
                "Pasted the rule-cleaned text instead.",
            ),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, Type, tauri_specta::Event)]
pub struct PersonalNotice {
    pub kind: PersonalNoticeKind,
    /// Technical detail for the toast description (never transcript text).
    pub detail: Option<String>,
}

/// Minimum gap between two system notifications of the same kind.
const SYSTEM_NOTIFICATION_INTERVAL: Duration = Duration::from_secs(60);

static LAST_SYSTEM_NOTIFICATION: Mutex<Option<HashMap<PersonalNoticeKind, Instant>>> =
    Mutex::new(None);

/// Pure rate-limit decision.
fn rate_limit_allows(last: Option<Instant>, now: Instant) -> bool {
    last.is_none_or(|last| now.duration_since(last) >= SYSTEM_NOTIFICATION_INTERVAL)
}

pub fn emit(app: &AppHandle, kind: PersonalNoticeKind, detail: Option<String>) {
    if let Err(e) = (PersonalNotice {
        kind,
        detail: detail.clone(),
    })
    .emit(app)
    {
        log::warn!("Failed to emit personal notice {kind:?}: {e}");
    }

    let window_visible = app
        .get_webview_window("main")
        .and_then(|w| w.is_visible().ok())
        .unwrap_or(false);
    if window_visible {
        return;
    }

    let now = Instant::now();
    let allowed = {
        let mut guard = LAST_SYSTEM_NOTIFICATION
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let map = guard.get_or_insert_with(HashMap::new);
        let allowed = rate_limit_allows(map.get(&kind).copied(), now);
        if allowed {
            map.insert(kind, now);
        }
        allowed
    };
    if allowed {
        let (title, body) = kind.system_text();
        system_notification(title, body);
    }
}

/// Posts a macOS notification without blocking the caller. Best effort:
/// `osascript` notifications appear under "Script Editor" in System Settings >
/// Notifications and need to be allowed there once.
#[cfg(target_os = "macos")]
fn system_notification(title: &str, body: &str) {
    let script = format!(
        "display notification {} with title {}",
        applescript_string(body),
        applescript_string(&format!("Handy Personal: {title}"))
    );
    let spawned = std::process::Command::new("osascript")
        .arg("-e")
        .arg(script)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
    match spawned {
        // Reap it off-thread so no zombie is left behind.
        Ok(mut child) => {
            std::thread::spawn(move || {
                let _ = child.wait();
            });
        }
        Err(e) => log::debug!("osascript notification failed: {e}"),
    }
}

#[cfg(not(target_os = "macos"))]
fn system_notification(title: &str, body: &str) {
    log::info!("Notice: {title} — {body}");
}

/// Quotes a string as an AppleScript literal.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn applescript_string(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rate_limit_allows_first_and_spaced_notifications() {
        let now = Instant::now();
        assert!(rate_limit_allows(None, now));
        assert!(!rate_limit_allows(Some(now), now + Duration::from_secs(5)));
        assert!(rate_limit_allows(
            Some(now),
            now + SYSTEM_NOTIFICATION_INTERVAL
        ));
    }

    #[test]
    fn applescript_strings_are_escaped() {
        assert_eq!(applescript_string(r#"a "b" \c"#), r#""a \"b\" \\c""#);
    }
}
