//! Speech analysis over the dictation log.
//!
//! Every metric is computed from the **raw** ASR text (before filler removal)
//! and the VAD speech segments, then aggregated per day and per ISO week.
//! Raw text keeps more disfluencies with verbatim mode on; without it,
//! Whisper already drops many fillers, so filler counts are a lower bound.
//!
//! Metrics (all heuristic, documented where they are computed):
//! - words per minute of speech (and of recording time)
//! - fillers per minute, broken down by filler
//! - pause length distribution (gaps between VAD speech segments; pauses
//!   under ~0.5 s are invisible, see `timeline.rs`)
//! - false starts / restarts, immediately repeated words
//! - hedge phrases ("to be honest", "I think", "kind of", ...)
//! - vocabulary variety (unique words, type-token ratio, moving-average TTR)

use std::collections::{BTreeMap, HashSet};

use chrono::{Datelike, Local, NaiveDate, TimeZone};
use serde::{Deserialize, Serialize};
use specta::Type;

use super::dataset::DictationRecord;
use super::timeline::Segment;

// ---------------------------------------------------------------------------
// Vocabulary lists
// ---------------------------------------------------------------------------

/// Single-token fillers: always counted.
const PURE_FILLERS: &[&str] = &[
    "um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm",
];

/// Ambiguous fillers, counted only when followed by a comma or ellipsis
/// (", like," / "you know," / "I mean,"), since "I like it" or "you know
/// him" are not fillers. Whisper punctuates these fairly consistently.
const DISCOURSE_FILLERS: &[&[&str]] = &[&["like"], &["you", "know"], &["i", "mean"]];

/// Hedge phrases, matched on word sequences. Longer phrases are matched
/// first so "i think" inside "i think so" counts once.
const HEDGES: &[&str] = &[
    "to be honest",
    "if that makes sense",
    "i'm not sure",
    "a little bit",
    "more or less",
    "i feel like",
    "i think",
    "i guess",
    "i suppose",
    "kind of",
    "sort of",
    "in a way",
    "maybe",
    "probably",
    "perhaps",
    "basically",
    "actually",
    "just",
];

/// Legitimate doubles that are not stutters ("that that", "had had").
const ALLOWED_REPEATS: &[&str] = &["that", "had", "is", "do", "very", "no", "bye"];

/// Window for the moving-average type-token ratio.
const MATTR_WINDOW: usize = 50;

/// Pause histogram bucket upper bounds (ms); the last bucket is open-ended.
pub const PAUSE_BUCKETS_MS: &[u64] = &[1000, 2000, 5000];

// ---------------------------------------------------------------------------
// Tokenization
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
struct Token {
    /// Lowercase word with surrounding punctuation stripped.
    word: String,
    /// Followed by a comma or ellipsis.
    comma_after: bool,
    /// Cut off: written with a trailing hyphen/dash ("I- I", "we sh—").
    cut_off: bool,
}

fn tokenize(text: &str) -> Vec<Token> {
    let raw: Vec<&str> = text.split_whitespace().collect();
    let mut tokens: Vec<Token> = Vec::with_capacity(raw.len());
    for piece in &raw {
        let trimmed_end = piece.trim_end_matches(|c: char| ",.?!;:\"')]…".contains(c));
        let trailing = &piece[trimmed_end.len()..];
        let comma_after =
            trailing.contains(',') || trailing.contains('…') || trailing.contains("..");
        let cut_off = trimmed_end.ends_with(['-', '—', '–']);
        let word: String = trimmed_end
            .trim_matches(|c: char| !c.is_alphanumeric() && c != '\'')
            .to_lowercase()
            .replace('’', "'");
        if word.is_empty() {
            // A lone dash: the previous word was cut off.
            if cut_off {
                if let Some(last) = tokens.last_mut() {
                    last.cut_off = true;
                }
            }
            continue;
        }
        tokens.push(Token {
            word,
            comma_after,
            cut_off,
        });
    }
    tokens
}

fn matches_at(tokens: &[Token], i: usize, phrase: &[&str]) -> bool {
    i + phrase.len() <= tokens.len()
        && phrase
            .iter()
            .enumerate()
            .all(|(k, w)| tokens[i + k].word == *w)
}

// ---------------------------------------------------------------------------
// Per-text analysis
// ---------------------------------------------------------------------------

/// Counts for one piece of text.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TextCounts {
    /// Words excluding fillers.
    pub words: u32,
    pub fillers: BTreeMap<String, u32>,
    pub false_starts: u32,
    pub repeated_words: u32,
    pub hedges: BTreeMap<String, u32>,
    /// Content words in order, for vocabulary metrics.
    pub vocabulary: Vec<String>,
}

pub fn analyze_text(text: &str) -> TextCounts {
    let tokens = tokenize(text);
    let mut counts = TextCounts::default();
    let mut filler_mask = vec![false; tokens.len()];

    // Fillers.
    let mut i = 0;
    while i < tokens.len() {
        if PURE_FILLERS.contains(&tokens[i].word.as_str()) {
            *counts.fillers.entry(tokens[i].word.clone()).or_default() += 1;
            filler_mask[i] = true;
            i += 1;
            continue;
        }
        let mut matched = false;
        for phrase in DISCOURSE_FILLERS {
            if matches_at(&tokens, i, phrase) {
                let last = i + phrase.len() - 1;
                if tokens[last].comma_after {
                    *counts.fillers.entry(phrase.join(" ")).or_default() += 1;
                    for m in filler_mask.iter_mut().take(last + 1).skip(i) {
                        *m = true;
                    }
                    i = last + 1;
                    matched = true;
                    break;
                }
            }
        }
        if !matched {
            i += 1;
        }
    }

    // False starts: a cut-off word ("I- I", "we sh- we").
    counts.false_starts = tokens.iter().filter(|t| t.cut_off).count() as u32;

    // Immediate repeats, skipping fillers and cut-offs (already counted).
    let content: Vec<&Token> = tokens
        .iter()
        .zip(&filler_mask)
        .filter(|(_, f)| !**f)
        .map(|(t, _)| t)
        .collect();
    for pair in content.windows(2) {
        if pair[0].word == pair[1].word
            && !pair[0].cut_off
            && !ALLOWED_REPEATS.contains(&pair[0].word.as_str())
        {
            counts.repeated_words += 1;
        }
    }

    // Hedges (longest phrase first, non-overlapping).
    let mut hedges: Vec<Vec<&str>> = HEDGES.iter().map(|h| h.split(' ').collect()).collect();
    hedges.sort_by_key(|h| std::cmp::Reverse(h.len()));
    let mut i = 0;
    while i < tokens.len() {
        if let Some(h) = hedges.iter().find(|h| matches_at(&tokens, i, h)) {
            *counts.hedges.entry(h.join(" ")).or_default() += 1;
            i += h.len();
        } else {
            i += 1;
        }
    }

    counts.vocabulary = content.iter().map(|t| t.word.clone()).collect();
    counts.words = counts.vocabulary.len() as u32;
    counts
}

/// Unique words / words.
pub fn type_token_ratio(words: &[String]) -> f64 {
    if words.is_empty() {
        return 0.0;
    }
    let unique: HashSet<&String> = words.iter().collect();
    unique.len() as f64 / words.len() as f64
}

/// Moving-average TTR (Covington & McFall): the mean TTR over every window of
/// `MATTR_WINDOW` words. Unlike plain TTR it does not fall just because more
/// was said, so days with different amounts of speech are comparable. Falls
/// back to plain TTR for shorter texts.
pub fn mattr(words: &[String]) -> f64 {
    if words.len() <= MATTR_WINDOW {
        return type_token_ratio(words);
    }
    let mut freq: std::collections::HashMap<&str, u32> = std::collections::HashMap::new();
    for w in &words[..MATTR_WINDOW] {
        *freq.entry(w).or_default() += 1;
    }
    let mut total = freq.len() as f64;
    let windows = words.len() - MATTR_WINDOW + 1;
    for i in 1..windows {
        let out = words[i - 1].as_str();
        if let Some(c) = freq.get_mut(out) {
            *c -= 1;
            if *c == 0 {
                freq.remove(out);
            }
        }
        *freq.entry(&words[i + MATTR_WINDOW - 1]).or_default() += 1;
        total += freq.len() as f64;
    }
    total / windows as f64 / MATTR_WINDOW as f64
}

/// Gaps between consecutive speech segments (ms).
pub fn pauses(segments: &[Segment]) -> Vec<u64> {
    segments
        .windows(2)
        .map(|w| w[1].start_ms.saturating_sub(w[0].end_ms))
        .filter(|&gap| gap > 0)
        .collect()
}

// ---------------------------------------------------------------------------
// Aggregates
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, Type)]
pub struct CountRate {
    pub count: u32,
    pub per_minute: f64,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, Type)]
pub struct PauseStats {
    pub count: u32,
    pub mean_ms: f64,
    pub median_ms: f64,
    pub p90_ms: f64,
    pub longest_ms: f64,
    /// Counts per bucket: <1 s, 1–2 s, 2–5 s, ≥5 s.
    pub buckets: Vec<u32>,
    pub per_minute: f64,
}

/// Metrics for a set of dictations.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, Type)]
pub struct Aggregate {
    pub dictations: u32,
    pub words: u32,
    /// Seconds of detected speech.
    pub speech_seconds: f64,
    /// Seconds of recording (speech + pauses).
    pub recording_seconds: f64,
    /// Words per minute of speech (articulation rate).
    pub words_per_minute: f64,
    /// Words per minute of recording (including pauses).
    pub words_per_minute_overall: f64,
    pub fillers: CountRate,
    pub fillers_by_word: BTreeMap<String, CountRate>,
    pub pauses: PauseStats,
    pub false_starts: CountRate,
    pub repeated_words: CountRate,
    pub hedges: CountRate,
    pub hedges_by_phrase: BTreeMap<String, CountRate>,
    pub unique_words: u32,
    pub type_token_ratio: f64,
    pub mattr: f64,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, Type)]
pub struct PeriodAggregate {
    /// `YYYY-MM-DD` (day) or `YYYY-Www` (ISO week).
    pub period: String,
    /// First day of the period, `YYYY-MM-DD`.
    pub start_date: String,
    pub metrics: Aggregate,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, Type)]
pub struct InsightsReport {
    /// Unix seconds.
    pub generated_at: i64,
    /// Unix seconds; `None` for all time.
    pub since: Option<i64>,
    pub totals: Aggregate,
    pub days: Vec<PeriodAggregate>,
    pub weeks: Vec<PeriodAggregate>,
}

fn round2(v: f64) -> f64 {
    (v * 100.0).round() / 100.0
}

fn rate(count: u32, minutes: f64) -> CountRate {
    CountRate {
        count,
        per_minute: if minutes > 0.0 {
            round2(count as f64 / minutes)
        } else {
            0.0
        },
    }
}

fn percentile(sorted: &[u64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() - 1) as f64 * p).round() as usize;
    sorted[idx] as f64
}

fn pause_stats(mut gaps: Vec<u64>, minutes: f64) -> PauseStats {
    gaps.sort_unstable();
    let mut buckets = vec![0u32; PAUSE_BUCKETS_MS.len() + 1];
    for &g in &gaps {
        let idx = PAUSE_BUCKETS_MS
            .iter()
            .position(|&b| g < b)
            .unwrap_or(PAUSE_BUCKETS_MS.len());
        buckets[idx] += 1;
    }
    let count = gaps.len() as u32;
    PauseStats {
        count,
        mean_ms: if gaps.is_empty() {
            0.0
        } else {
            round2(gaps.iter().sum::<u64>() as f64 / gaps.len() as f64)
        },
        median_ms: percentile(&gaps, 0.5),
        p90_ms: percentile(&gaps, 0.9),
        longest_ms: gaps.last().copied().unwrap_or(0) as f64,
        buckets,
        per_minute: rate(count, minutes).per_minute,
    }
}

/// Aggregates a set of records (order irrelevant).
pub fn aggregate<'a>(records: impl IntoIterator<Item = &'a DictationRecord>) -> Aggregate {
    let mut dictations = 0u32;
    let mut speech_ms = 0u64;
    let mut recording_ms = 0u64;
    let mut fillers: BTreeMap<String, u32> = BTreeMap::new();
    let mut hedges: BTreeMap<String, u32> = BTreeMap::new();
    let mut false_starts = 0u32;
    let mut repeated = 0u32;
    let mut vocabulary: Vec<String> = Vec::new();
    let mut gaps: Vec<u64> = Vec::new();

    for r in records {
        if r.raw_asr_text.trim().is_empty() {
            continue;
        }
        dictations += 1;
        let segment_ms: u64 = r
            .vad_segments
            .iter()
            .map(|s| s.end_ms.saturating_sub(s.start_ms))
            .sum();
        // Prefer the timeline; fall back to the kept-audio length.
        let this_speech = if segment_ms > 0 {
            segment_ms
        } else {
            r.speech_audio_ms.unwrap_or(0)
        };
        speech_ms += this_speech;
        recording_ms += r
            .latency
            .recording_ms
            .unwrap_or(this_speech)
            .max(this_speech);
        gaps.extend(pauses(&r.vad_segments));

        let c = analyze_text(&r.raw_asr_text);
        for (k, v) in c.fillers {
            *fillers.entry(k).or_default() += v;
        }
        for (k, v) in c.hedges {
            *hedges.entry(k).or_default() += v;
        }
        false_starts += c.false_starts;
        repeated += c.repeated_words;
        vocabulary.extend(c.vocabulary);
    }

    let speech_min = speech_ms as f64 / 60_000.0;
    let recording_min = recording_ms as f64 / 60_000.0;
    let words = vocabulary.len() as u32;
    let filler_total: u32 = fillers.values().sum();
    let hedge_total: u32 = hedges.values().sum();
    let unique: HashSet<&String> = vocabulary.iter().collect();

    Aggregate {
        dictations,
        words,
        speech_seconds: round2(speech_ms as f64 / 1000.0),
        recording_seconds: round2(recording_ms as f64 / 1000.0),
        words_per_minute: rate(words, speech_min).per_minute,
        words_per_minute_overall: rate(words, recording_min).per_minute,
        fillers: rate(filler_total, speech_min),
        fillers_by_word: fillers
            .into_iter()
            .map(|(k, v)| (k, rate(v, speech_min)))
            .collect(),
        pauses: pause_stats(gaps, recording_min),
        false_starts: rate(false_starts, speech_min),
        repeated_words: rate(repeated, speech_min),
        hedges: rate(hedge_total, speech_min),
        hedges_by_phrase: hedges
            .into_iter()
            .map(|(k, v)| (k, rate(v, speech_min)))
            .collect(),
        unique_words: unique.len() as u32,
        type_token_ratio: round2(type_token_ratio(&vocabulary)),
        mattr: round2(mattr(&vocabulary)),
    }
}

fn local_date(ts: i64) -> NaiveDate {
    Local
        .timestamp_opt(ts, 0)
        .single()
        .map(|d| d.date_naive())
        .unwrap_or_default()
}

/// Builds the full report: totals, per-day and per-ISO-week aggregates.
pub fn report(records: &[DictationRecord], since: Option<i64>) -> InsightsReport {
    let mut by_day: BTreeMap<NaiveDate, Vec<&DictationRecord>> = BTreeMap::new();
    let mut by_week: BTreeMap<(i32, u32), Vec<&DictationRecord>> = BTreeMap::new();
    for r in records {
        let date = local_date(r.created_at);
        let week = date.iso_week();
        by_day.entry(date).or_default().push(r);
        by_week
            .entry((week.year(), week.week()))
            .or_default()
            .push(r);
    }
    InsightsReport {
        generated_at: chrono::Utc::now().timestamp(),
        since,
        totals: aggregate(records),
        days: by_day
            .into_iter()
            .map(|(date, rs)| PeriodAggregate {
                period: date.format("%Y-%m-%d").to_string(),
                start_date: date.format("%Y-%m-%d").to_string(),
                metrics: aggregate(rs),
            })
            .collect(),
        weeks: by_week
            .into_iter()
            .map(|((year, week), rs)| {
                let start = NaiveDate::from_isoywd_opt(year, week, chrono::Weekday::Mon)
                    .unwrap_or_default();
                PeriodAggregate {
                    period: format!("{year}-W{week:02}"),
                    start_date: start.format("%Y-%m-%d").to_string(),
                    metrics: aggregate(rs),
                }
            })
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::personal::dataset::Latency;

    fn record(ts: i64, text: &str, segments: &[(u64, u64)]) -> DictationRecord {
        DictationRecord {
            created_at: ts,
            raw_asr_text: text.into(),
            vad_segments: segments
                .iter()
                .map(|&(s, e)| Segment {
                    start_ms: s,
                    end_ms: e,
                })
                .collect(),
            latency: Latency {
                recording_ms: segments.last().map(|s| s.1),
                ..Default::default()
            },
            ..Default::default()
        }
    }

    #[test]
    fn counts_pure_fillers_by_word() {
        let c = analyze_text("Um, so I was, uh, thinking. Umm hmm.");
        assert_eq!(c.fillers.get("um"), Some(&1));
        assert_eq!(c.fillers.get("uh"), Some(&1));
        assert_eq!(c.fillers.get("umm"), Some(&1));
        assert_eq!(c.fillers.get("hmm"), Some(&1));
        // Fillers are not words.
        assert_eq!(c.words, 4); // so I was thinking
    }

    #[test]
    fn discourse_fillers_need_punctuation() {
        let c = analyze_text("I like it. It was, like, huge, you know, and I mean, wow.");
        assert_eq!(c.fillers.get("like"), Some(&1));
        assert_eq!(c.fillers.get("you know"), Some(&1));
        assert_eq!(c.fillers.get("i mean"), Some(&1));
        let c = analyze_text("Do you know him? I mean what I say.");
        assert!(c.fillers.is_empty(), "{:?}", c.fillers);
    }

    #[test]
    fn counts_false_starts() {
        let c = analyze_text("I- I think we sh- we should go — go now.");
        // "I-", "sh-", and the dash after "go"? The lone dash marks "should go"
        // as cut off.
        assert_eq!(c.false_starts, 3);
    }

    #[test]
    fn counts_immediate_repeats_but_not_allowed_doubles() {
        let c = analyze_text("the the plan is that that we we go");
        assert_eq!(c.repeated_words, 2); // the the, we we
                                         // A filler between repeats doesn't hide them.
        let c = analyze_text("I, um, I went");
        assert_eq!(c.repeated_words, 1);
        // A cut-off and its restart is a false start, not a repeat.
        let c = analyze_text("I- I went");
        assert_eq!(c.repeated_words, 0);
        assert_eq!(c.false_starts, 1);
    }

    #[test]
    fn counts_hedges_longest_first() {
        let c = analyze_text(
            "To be honest I think it's kind of fine, maybe. I feel like it's just okay.",
        );
        assert_eq!(c.hedges.get("to be honest"), Some(&1));
        assert_eq!(c.hedges.get("i think"), Some(&1));
        assert_eq!(c.hedges.get("kind of"), Some(&1));
        assert_eq!(c.hedges.get("maybe"), Some(&1));
        assert_eq!(c.hedges.get("i feel like"), Some(&1));
        assert_eq!(c.hedges.get("just"), Some(&1));
        assert_eq!(c.hedges.values().sum::<u32>(), 6);
    }

    #[test]
    fn vocabulary_variety() {
        let words: Vec<String> = "a b a b".split(' ').map(String::from).collect();
        assert_eq!(type_token_ratio(&words), 0.5);
        assert_eq!(type_token_ratio(&[]), 0.0);
        // MATTR is stable under repetition of the same material.
        let base: Vec<String> = (0..60).map(|i| format!("w{}", i % 30)).collect();
        let doubled: Vec<String> = base.iter().chain(base.iter()).cloned().collect();
        assert!((mattr(&base) - mattr(&doubled)).abs() < 0.05);
        // While plain TTR halves.
        assert!(type_token_ratio(&doubled) < type_token_ratio(&base));
        // All-unique text has MATTR 1.
        let unique: Vec<String> = (0..120).map(|i| format!("u{i}")).collect();
        assert!((mattr(&unique) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn pauses_are_gaps_between_segments() {
        let segs = [
            Segment {
                start_ms: 0,
                end_ms: 1000,
            },
            Segment {
                start_ms: 1600,
                end_ms: 3000,
            },
            Segment {
                start_ms: 6000,
                end_ms: 7000,
            },
        ];
        assert_eq!(pauses(&segs), vec![600, 3000]);
        assert!(pauses(&segs[..1]).is_empty());
    }

    #[test]
    fn pause_stats_buckets_and_percentiles() {
        let s = pause_stats(vec![600, 1500, 3000, 8000], 2.0);
        assert_eq!(s.count, 4);
        assert_eq!(s.buckets, vec![1, 1, 1, 1]);
        assert_eq!(s.longest_ms, 8000.0);
        assert_eq!(s.median_ms, 3000.0); // index round(1.5)=2
        assert_eq!(s.mean_ms, 3275.0);
        assert_eq!(s.per_minute, 2.0);
    }

    #[test]
    fn words_per_minute_uses_speech_time() {
        // 10 words over 6 s of speech (two 3 s segments) in a 10 s recording.
        let r = record(
            1_700_000_000,
            "one two three four five six seven eight nine ten",
            &[(0, 3000), (7000, 10000)],
        );
        let a = aggregate([&r]);
        assert_eq!(a.words, 10);
        assert_eq!(a.speech_seconds, 6.0);
        assert_eq!(a.recording_seconds, 10.0);
        assert_eq!(a.words_per_minute, 100.0);
        assert_eq!(a.words_per_minute_overall, 60.0);
        assert_eq!(a.pauses.count, 1);
        assert_eq!(a.pauses.mean_ms, 4000.0);
    }

    #[test]
    fn fillers_per_minute_by_word() {
        // 3 fillers in 30 s of speech -> 6/min; "um" twice -> 4/min.
        let r = record(1_700_000_000, "um I um went uh home", &[(0, 30_000)]);
        let a = aggregate([&r]);
        assert_eq!(a.fillers.count, 3);
        assert_eq!(a.fillers.per_minute, 6.0);
        assert_eq!(a.fillers_by_word["um"].per_minute, 4.0);
        assert_eq!(a.fillers_by_word["uh"].count, 1);
    }

    #[test]
    fn speech_falls_back_to_audio_length_without_segments() {
        let mut r = record(1_700_000_000, "a b c", &[]);
        r.speech_audio_ms = Some(60_000);
        let a = aggregate([&r]);
        assert_eq!(a.words_per_minute, 3.0);
    }

    #[test]
    fn empty_raw_text_is_ignored() {
        let r = record(1_700_000_000, "  ", &[(0, 1000)]);
        let a = aggregate([&r]);
        assert_eq!(a.dictations, 0);
        assert_eq!(a.words_per_minute, 0.0);
    }

    #[test]
    fn report_groups_by_local_day_and_iso_week() {
        let day = |y, m, d, h| {
            Local
                .with_ymd_and_hms(y, m, d, h, 0, 0)
                .unwrap()
                .timestamp()
        };
        let records = vec![
            record(day(2026, 10, 26, 9), "um hello", &[(0, 1000)]), // Mon W44
            record(day(2026, 10, 26, 18), "hello again", &[(0, 1000)]),
            record(day(2026, 10, 30, 9), "uh bye", &[(0, 1000)]), // Fri W44
            record(day(2026, 11, 2, 9), "next week", &[(0, 1000)]), // Mon W45
        ];
        let rep = report(&records, None);
        assert_eq!(rep.totals.dictations, 4);
        let days: Vec<&str> = rep.days.iter().map(|d| d.period.as_str()).collect();
        assert_eq!(days, vec!["2026-10-26", "2026-10-30", "2026-11-02"]);
        assert_eq!(rep.days[0].metrics.dictations, 2);
        let weeks: Vec<&str> = rep.weeks.iter().map(|w| w.period.as_str()).collect();
        assert_eq!(weeks, vec!["2026-W44", "2026-W45"]);
        assert_eq!(rep.weeks[0].start_date, "2026-10-26");
        assert_eq!(rep.weeks[0].metrics.dictations, 3);
        assert_eq!(rep.weeks[0].metrics.fillers.count, 2);
    }
}
