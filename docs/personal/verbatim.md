# Verbatim transcription (Handy Personal)

Goal: keep disfluencies ("um", "uh", false starts, repetitions) in the **raw**
transcript that is logged to the dataset, while the pasted text is still cleaned.

## What transcribe.cpp exposes (v0.4.0)

| Mechanism              | Field                                                                                  | Gate                     | Models                                                      |
| ---------------------- | -------------------------------------------------------------------------------------- | ------------------------ | ----------------------------------------------------------- |
| Whisper initial prompt | `RunOptions::family = RunExtension::Whisper(WhisperRunOptions { initial_prompt, .. })` | `Feature::InitialPrompt` | Whisper (all sizes, incl. Turbo)                            |
| Generic context prompt | `RunOptions::prompt`                                                                   | `Feature::ContextPrompt` | Models with a transcription-conditioning slot (not Whisper) |
| Vocabulary             | `RunOptions::vocabulary`                                                               | `Feature::Vocabulary`    | Formatted per family                                        |

`initial_prompt` **cannot be combined** with `vocabulary` or `prompt` (the
library rejects the combination), so in verbatim mode Handy Personal folds your
custom words into the end of the initial prompt instead and sends no vocabulary.
Fuzzy custom-word correction still runs on the output.

## What Handy Personal does

When _Verbatim transcription_ is on and the loaded model reports
`Feature::InitialPrompt`, every run gets the initial prompt (default:
`Umm, uh, so, like, I mean, you know... I- I was, uh, I think, hmm, kind of, er, okay.`
plus your custom words). Rule-based filler removal still runs afterwards, so
`raw_asr_text` keeps the fillers and `rule_cleaned_text` / `final_text` do not.

Implementation: `src-tauri/src/personal/verbatim.rs`, hooked from
`run_setup()` in `managers/transcription.rs`.

## What is missing / limits

- **Non-Whisper models** (Parakeet, Moonshine, SenseVoice, Canary, ...) do not
  support `InitialPrompt`; verbatim mode is a no-op for them (logged at debug
  level). `Feature::ContextPrompt` exists for some families, but it is a
  _context_ slot (domain text), and whether fillers in it change disfluency
  behaviour is model-specific and untested, so it is not used.
- **Streaming (live) decoding** uses the same `RunOptions` for `StreamBegin`;
  whether a family honours the Whisper extension while streaming depends on
  the family. Whisper models in Handy are decoded in batch, so this does not
  matter today.
- Whisper only conditions on roughly the last 224 prompt tokens; the prompt is
  truncated to 600 characters.
- An initial prompt biases style, it does not guarantee verbatim output: very
  short clips or strong fillers may still be dropped. Measure with
  `eval/` (WER against verbatim references) before relying on it.
