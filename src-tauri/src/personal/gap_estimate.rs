//! Estimating fillers Parakeet dropped, for dictations that have no
//! verbatim transcript yet.
//!
//! Two estimators, both clearly labelled as **estimates** in the report and
//! drawn differently on the charts:
//!
//! 1. [`from_word_gaps`] — the intended method: stretches where the VAD says
//!    you were speaking but the dictation model produced no word are
//!    probable fillers or mumbles. Needs per-word timestamps from the
//!    dictation model.
//! 2. [`from_duration`] — what is possible **today**. Handy does not log
//!    Parakeet's word timestamps (the dictation path keeps only text; adding
//!    them would change the Parakeet transcription path, which this work
//!    must not touch). So this compares how many tokens the speech time
//!    should hold (your own speaking rate, calibrated on dictations that do
//!    have a verbatim transcript) with how many words Parakeet produced; the
//!    shortfall is counted as probable fillers.
//!
//! What's missing for (1): `dictation_log` would need a `word_timings`
//! column filled from the engine's token timestamps. Once that exists,
//! `aggregate` can switch to `from_word_gaps` per record.

use super::timeline::Segment;

// `from_word_gaps` and its constants are the intended estimator; they are
// tested but unused until the dictation log records word timings.

/// Gaps shorter than this inside speech are normal coarticulation.
#[allow(dead_code)]
pub const MIN_GAP_MS: u64 = 250;
/// A gap longer than this is one mumble/hesitation, not many fillers.
#[allow(dead_code)]
pub const MAX_GAP_MS: u64 = 2000;
/// Typical filler length ("um", "uh") when nothing is calibrated.
#[allow(dead_code)]
pub const DEFAULT_FILLER_MS: f64 = 350.0;
/// ~150 tokens per minute of speech when nothing is calibrated.
pub const DEFAULT_MS_PER_TOKEN: f64 = 400.0;
/// Never claim more than this share of the expected tokens were fillers.
const MAX_FILLER_SHARE: f64 = 0.35;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Calibration {
    /// Speech milliseconds per spoken token (words + fillers), from your own
    /// verbatim-transcribed dictations.
    pub ms_per_token: f64,
    /// How many dictations it was calibrated on (0 = defaults).
    pub samples: u32,
}

impl Default for Calibration {
    fn default() -> Self {
        Self {
            ms_per_token: DEFAULT_MS_PER_TOKEN,
            samples: 0,
        }
    }
}

/// Builds a calibration from `(speech_ms, verbatim_token_count)` pairs.
/// Needs at least 3 dictations with 5+ tokens; otherwise the defaults.
pub fn calibrate(samples: impl IntoIterator<Item = (u64, u32)>) -> Calibration {
    let mut ms = 0u64;
    let mut tokens = 0u64;
    let mut n = 0u32;
    for (speech_ms, count) in samples {
        if count >= 5 && speech_ms > 0 {
            ms += speech_ms;
            tokens += u64::from(count);
            n += 1;
        }
    }
    if n < 3 || tokens == 0 {
        return Calibration::default();
    }
    let rate = ms as f64 / tokens as f64;
    Calibration {
        // Keep it within plausible speaking rates (60–300 tokens/min).
        ms_per_token: rate.clamp(200.0, 1000.0),
        samples: n,
    }
}

/// Estimator 2: probable fillers from speech duration vs words produced.
pub fn from_duration(speech_ms: u64, asr_words: u32, cal: &Calibration) -> f64 {
    if speech_ms == 0 {
        return 0.0;
    }
    let expected = speech_ms as f64 / cal.ms_per_token;
    let missing = (expected - f64::from(asr_words)).max(0.0);
    // Round down to whole fillers' worth and cap.
    missing.min(expected * MAX_FILLER_SHARE).floor()
}

/// Estimator 1: probable fillers from uncovered stretches of speech.
/// `words` are `(start_ms, end_ms)` in recording time.
#[allow(dead_code)]
pub fn from_word_gaps(segments: &[Segment], words: &[(u64, u64)], filler_ms: f64) -> f64 {
    let mut words: Vec<(u64, u64)> = words.to_vec();
    words.sort_unstable();
    let mut total = 0.0;
    for seg in segments {
        let mut cursor = seg.start_ms;
        let mut gaps: Vec<u64> = Vec::new();
        for &(ws, we) in words
            .iter()
            .filter(|(s, e)| *e > seg.start_ms && *s < seg.end_ms)
        {
            let ws = ws.max(seg.start_ms);
            if ws > cursor {
                gaps.push(ws - cursor);
            }
            cursor = cursor.max(we.min(seg.end_ms));
        }
        if seg.end_ms > cursor {
            gaps.push(seg.end_ms - cursor);
        }
        for gap in gaps {
            if gap < MIN_GAP_MS {
                continue;
            }
            total += if gap > MAX_GAP_MS {
                1.0
            } else {
                (gap as f64 / filler_ms).round().max(1.0)
            };
        }
    }
    total
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(s: u64, e: u64) -> Segment {
        Segment {
            start_ms: s,
            end_ms: e,
        }
    }

    #[test]
    fn word_gaps_inside_speech_are_probable_fillers() {
        // Speech 0–3000 ms; words cover all but a 700 ms hole and a 100 ms
        // seam (ignored).
        let segments = [seg(0, 3000)];
        let words = [(0, 1000), (1100, 1500), (2200, 3000)];
        assert_eq!(from_word_gaps(&segments, &words, 350.0), 2.0);
        // Fully covered speech: nothing.
        assert_eq!(from_word_gaps(&segments, &[(0, 3000)], 350.0), 0.0);
        // Speech with no words at all for 1.5 s → ~4 fillers.
        assert_eq!(from_word_gaps(&[seg(0, 1500)], &[], 350.0), 4.0);
        // A very long uncovered stretch counts as one mumble.
        assert_eq!(from_word_gaps(&[seg(0, 9000)], &[], 350.0), 1.0);
        // Silence between segments is a pause, not a filler.
        let two = [seg(0, 1000), seg(5000, 6000)];
        assert_eq!(from_word_gaps(&two, &[(0, 1000), (5000, 6000)], 350.0), 0.0);
    }

    #[test]
    fn duration_estimate_uses_the_shortfall_and_is_capped() {
        let cal = Calibration {
            ms_per_token: 400.0,
            samples: 10,
        };
        // 10 s of speech ≈ 25 tokens; 20 words → 5 probable fillers.
        assert_eq!(from_duration(10_000, 20, &cal), 5.0);
        // More words than expected (fast speaker) → none.
        assert_eq!(from_duration(10_000, 40, &cal), 0.0);
        // No words for long speech: capped at 35 % of expected.
        assert_eq!(from_duration(10_000, 0, &cal), 8.0);
        assert_eq!(from_duration(0, 0, &cal), 0.0);
    }

    #[test]
    fn calibration_needs_enough_data_and_stays_plausible() {
        assert_eq!(calibrate([(10_000, 25)]), Calibration::default());
        let c = calibrate([(10_000, 25), (20_000, 50), (4_000, 10), (1_000, 2)]);
        assert_eq!(c.samples, 3);
        assert!((c.ms_per_token - 400.0).abs() < 1e-9);
        let silly = calibrate([(100_000, 5), (100_000, 5), (100_000, 5)]);
        assert_eq!(silly.ms_per_token, 1000.0);
    }
}
