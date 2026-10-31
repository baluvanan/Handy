//! Refuses to paste into password fields.
//!
//! Hooked from `clipboard::paste` (before any clipboard or keyboard access)
//! and `actions.rs` (to suppress the generic paste-error toast in favour of a
//! specific notice).

use tauri::{AppHandle, Emitter};

use super::notice::{self, PersonalNoticeKind};
use crate::settings::{AppSettings, PasteMethod};

/// Error string `clipboard::paste` returns when it refused to paste.
pub const PASTE_BLOCKED_SECURE_FIELD: &str = "paste blocked: a secure (password) field is focused";

/// Pure decision, so the policy is testable without a focused window.
pub fn should_block(enabled: bool, paste_method: PasteMethod, secure_field_focused: bool) -> bool {
    enabled && paste_method != PasteMethod::None && secure_field_focused
}

/// Checks focus and, when the paste must not happen, tells the user why.
/// Returns `Err` with [`PASTE_BLOCKED_SECURE_FIELD`] to abort the paste.
pub fn check(app: &AppHandle, settings: &AppSettings) -> Result<(), String> {
    let enabled = settings.personal.block_paste_in_secure_fields;
    // Only query focus when it can matter (AX calls are cheap but not free).
    if !enabled || settings.paste_method == PasteMethod::None {
        return Ok(());
    }
    if should_block(
        enabled,
        settings.paste_method,
        super::focus::secure_field_focused(),
    ) {
        log::info!("Not pasting: a secure text field / secure input is active");
        notice::emit(app, PersonalNoticeKind::PasteBlockedSecureField, None);
        return Err(PASTE_BLOCKED_SECURE_FIELD.to_string());
    }
    Ok(())
}

pub fn is_blocked_error(error: &str) -> bool {
    error == PASTE_BLOCKED_SECURE_FIELD
}

/// Shows upstream's generic paste-failure toast, except for a refused paste,
/// which already showed its own notice. Hooked from `actions.rs`.
pub fn emit_paste_error(app: &AppHandle, error: &str) {
    if !is_blocked_error(error) {
        let _ = app.emit("paste-error", ());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_only_when_enabled_and_secure() {
        assert!(should_block(true, PasteMethod::CtrlV, true));
        assert!(should_block(true, PasteMethod::Direct, true));
        assert!(!should_block(false, PasteMethod::CtrlV, true));
        assert!(!should_block(true, PasteMethod::CtrlV, false));
        assert!(!should_block(true, PasteMethod::None, true));
    }

    #[test]
    fn blocked_error_is_recognised() {
        assert!(is_blocked_error(PASTE_BLOCKED_SECURE_FIELD));
        assert!(!is_blocked_error("some other failure"));
    }
}
