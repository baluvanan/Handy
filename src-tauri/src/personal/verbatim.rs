//! "Verbatim" transcription: bias Whisper towards keeping disfluencies.
//!
//! Whisper tends to drop fillers ("um", "uh") and false starts because its
//! training transcripts were cleaned. Conditioning it with an initial prompt
//! that itself contains fillers makes it far more likely to keep them, which
//! is what the raw-transcript dataset and speech analysis want. Rule-based
//! filler removal still runs afterwards, so the *pasted* text stays clean.
//!
//! transcribe.cpp exposes this as `WhisperRunOptions::initial_prompt`
//! (`Feature::InitialPrompt`, Whisper models only). It cannot be combined
//! with `RunOptions::vocabulary`, so in verbatim mode custom words are folded
//! into the prompt text instead (fuzzy custom-word correction still runs on
//! the output, because no vocabulary was sent). Non-Whisper models
//! (Parakeet, Moonshine, SenseVoice, ...) have no initial-prompt slot; for
//! them verbatim mode is a no-op (see `docs/personal/verbatim.md`).
//!
//! Hooked from `managers::transcription::run_setup`.

use transcribe_cpp::{RunExtension, RunOptions, WhisperRunOptions};

use crate::settings::AppSettings;

pub const DEFAULT_VERBATIM_PROMPT: &str =
    "Umm, uh, so, like, I mean, you know... I- I was, uh, I think, hmm, kind of, er, okay.";

/// Whisper's prompt window is ~224 tokens; keep well under it.
const MAX_PROMPT_CHARS: usize = 600;

/// The initial prompt for verbatim mode: the filler-laden prompt, then any
/// custom words so they still steer spelling.
pub fn initial_prompt(base: &str, custom_words: &[String]) -> String {
    let base = if base.trim().is_empty() {
        DEFAULT_VERBATIM_PROMPT
    } else {
        base.trim()
    };
    let mut prompt = base.to_string();
    if !custom_words.is_empty() {
        prompt.push(' ');
        prompt.push_str(&custom_words.join(", "));
        prompt.push('.');
    }
    if prompt.chars().count() > MAX_PROMPT_CHARS {
        prompt = prompt.chars().take(MAX_PROMPT_CHARS).collect();
    }
    prompt
}

/// Applies verbatim mode to a run's options when enabled and supported.
pub fn apply(
    mut options: RunOptions,
    supports_initial_prompt: bool,
    settings: &AppSettings,
) -> RunOptions {
    let personal = &settings.personal;
    if !personal.verbatim_mode {
        return options;
    }
    if !supports_initial_prompt {
        log::debug!("Verbatim mode: the loaded model has no initial-prompt slot; ignoring");
        return options;
    }
    let prompt = initial_prompt(&personal.verbatim_prompt, &settings.custom_words);
    log::debug!("Verbatim mode: initial prompt of {} chars", prompt.len());
    // initial_prompt and vocabulary/prompt are mutually exclusive.
    options.vocabulary.clear();
    options.prompt = None;
    let mut whisper = match options.family.take() {
        Some(RunExtension::Whisper(existing)) => existing,
        _ => WhisperRunOptions::default(),
    };
    whisper.initial_prompt = Some(prompt);
    options.family = Some(RunExtension::Whisper(whisper));
    options
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(verbatim: bool) -> AppSettings {
        let mut s = crate::settings::get_default_settings();
        s.personal.verbatim_mode = verbatim;
        s.custom_words = vec!["Handy".into()];
        s
    }

    fn base_options() -> RunOptions {
        RunOptions {
            vocabulary: vec!["Handy".into()],
            ..Default::default()
        }
    }

    #[test]
    fn off_leaves_options_untouched() {
        let out = apply(base_options(), true, &settings(false));
        assert_eq!(out.vocabulary, vec!["Handy".to_string()]);
        assert!(out.family.is_none());
    }

    #[test]
    fn unsupported_model_is_untouched() {
        let out = apply(base_options(), false, &settings(true));
        assert!(out.family.is_none());
        assert_eq!(out.vocabulary.len(), 1);
    }

    #[test]
    fn on_sets_initial_prompt_and_drops_vocabulary() {
        let out = apply(base_options(), true, &settings(true));
        assert!(out.vocabulary.is_empty());
        match out.family {
            Some(RunExtension::Whisper(w)) => {
                let p = w.initial_prompt.unwrap();
                assert!(p.starts_with("Umm, uh"));
                assert!(p.ends_with("Handy."));
            }
            other => panic!("expected a whisper extension, got {other:?}"),
        }
    }

    #[test]
    fn prompt_is_bounded() {
        let words: Vec<String> = (0..500).map(|i| format!("word{i}")).collect();
        assert!(initial_prompt("", &words).chars().count() <= MAX_PROMPT_CHARS);
    }
}
