//! Where speech fell within a recording, in recording time.
//!
//! VAD drops non-speech frames before audio is saved, so the WAV holds only
//! speech, back to back. This tracks the original timeline alongside (hooked
//! into `CaptureSink`) so pause lengths can be analysed later.
//!
//! Caveat for analysis: the VAD's onset/hangover smoothing (~60 ms onset,
//! 450 ms offline hangover) means pauses shorter than about half a second are
//! absorbed into the surrounding segment and never show up as a gap.

use serde::{Deserialize, Serialize};

const SAMPLES_PER_MS: u64 = 16; // 16 kHz

/// A `[start_ms, end_ms)` span of kept (speech) audio in recording time.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Segment {
    pub start_ms: u64,
    pub end_ms: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SpeechTimeline {
    /// Samples received so far (pre-VAD), i.e. the recording's length.
    position: u64,
    /// Merged speech spans, in samples.
    spans: Vec<(u64, u64)>,
}

impl SpeechTimeline {
    /// Records one chunk of `chunk_len` samples, of which the last
    /// `kept_len` were emitted as speech. `kept_len` may exceed `chunk_len`
    /// when the VAD releases pre-roll with the frame that started speech; the
    /// pre-roll lies just before this chunk.
    pub fn record(&mut self, chunk_len: usize, kept_len: usize) {
        let end = self.position + chunk_len as u64;
        self.position = end;
        if kept_len == 0 {
            return;
        }
        let start = end.saturating_sub(kept_len as u64);
        match self.spans.last_mut() {
            // Contiguous or overlapping with the previous span: extend it.
            Some(last) if start <= last.1 => last.1 = last.1.max(end),
            _ => self.spans.push((start, end)),
        }
    }

    pub fn total_ms(&self) -> u64 {
        self.position / SAMPLES_PER_MS
    }

    pub fn segments(&self) -> Vec<Segment> {
        self.spans
            .iter()
            .map(|&(s, e)| Segment {
                start_ms: s / SAMPLES_PER_MS,
                end_ms: e / SAMPLES_PER_MS,
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FRAME: usize = 512; // 32 ms

    #[test]
    fn merges_contiguous_speech_and_splits_on_silence() {
        let mut t = SpeechTimeline::default();
        t.record(FRAME, FRAME); // 0-32
        t.record(FRAME, FRAME); // 32-64
        t.record(FRAME, 0); // silence 64-96
        t.record(FRAME, 0); // silence 96-128
        t.record(FRAME, FRAME); // 128-160
        assert_eq!(t.total_ms(), 160);
        assert_eq!(
            t.segments(),
            vec![
                Segment {
                    start_ms: 0,
                    end_ms: 64
                },
                Segment {
                    start_ms: 128,
                    end_ms: 160
                }
            ]
        );
    }

    #[test]
    fn preroll_extends_the_segment_backwards() {
        let mut t = SpeechTimeline::default();
        for _ in 0..4 {
            t.record(FRAME, 0); // 0-128 silence
        }
        // Speech starts; VAD releases two frames of pre-roll plus this one.
        t.record(FRAME, 3 * FRAME);
        assert_eq!(
            t.segments(),
            vec![Segment {
                start_ms: 64,
                end_ms: 160
            }]
        );
    }

    #[test]
    fn empty_recording_has_no_segments() {
        let t = SpeechTimeline::default();
        assert_eq!(t.total_ms(), 0);
        assert!(t.segments().is_empty());
    }
}
