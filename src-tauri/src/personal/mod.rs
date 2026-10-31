//! Fork-only additions for "Handy Personal".
//!
//! Everything the fork adds lives under this module (or in new files next to
//! it) so upstream merges only ever touch a handful of hook lines in existing
//! files. Each submodule documents the upstream call sites it relies on.

pub mod focus;
pub mod notice;
pub mod paste_guard;
pub mod pasteboard;
#[cfg(target_os = "macos")]
pub mod pasteboard_macos;
pub mod settings;

/// This fork never self-updates: an upstream release must never overwrite it,
/// and builds carry no updater signing key. Forces update checks off exactly
/// like `HANDY_DISABLE_UPDATER=1` does (UI shows the toggle as locked).
pub const UPDATER_DISABLED: bool = true;
