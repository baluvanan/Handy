//! Stored speech metrics, with the definition version they were computed
//! with (`<app data>/insights.db`, Handy-owned).
//!
//! A period's stored result is reused as long as its input (which
//! dictations, which transcripts) is unchanged, **even if a metric's
//! definition has changed since**. That keeps history as it was measured and
//! makes a calculation change visible as a marked discontinuity on the
//! charts instead of silently rewriting the past or looking like a change in
//! how you speak. When the input changes (e.g. the worker adds a verbatim
//! transcript), the period is recomputed with the current definitions.
//! "Recompute with current definitions" ([`clear`]) rewrites everything.

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};

use super::analysis::{discontinuities, Aggregate, InsightsReport, PeriodAggregate};

const MIGRATIONS: &[&str] = &["CREATE TABLE IF NOT EXISTS period_metrics (
        granularity TEXT NOT NULL,
        period TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        metric_versions TEXT NOT NULL,
        metrics TEXT NOT NULL,
        computed_at INTEGER NOT NULL,
        PRIMARY KEY (granularity, period)
    );"];

pub fn db_path(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("insights.db")
}

pub fn open(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)?;
    conn.busy_timeout(std::time::Duration::from_secs(2))?;
    let version: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
    for (i, sql) in MIGRATIONS.iter().enumerate().skip(version.max(0) as usize) {
        conn.execute_batch(sql)?;
        conn.pragma_update(None, "user_version", (i + 1) as i64)?;
    }
    Ok(conn)
}

fn stabilize_periods(
    conn: &Connection,
    granularity: &str,
    periods: &mut [PeriodAggregate],
    now: i64,
    write: bool,
) -> Result<()> {
    for p in periods.iter_mut() {
        let stored: Option<(String, String, String)> = conn
            .query_row(
                "SELECT input_hash, metric_versions, metrics FROM period_metrics
                 WHERE granularity = ?1 AND period = ?2",
                params![granularity, p.period],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        if let Some((hash, versions, metrics)) = stored {
            if hash == p.input_hash {
                let versions = serde_json::from_str(&versions);
                let metrics = serde_json::from_str::<Aggregate>(&metrics);
                if let (Ok(versions), Ok(metrics)) = (versions, metrics) {
                    p.frozen = versions != p.metric_versions;
                    p.metric_versions = versions;
                    p.metrics = metrics;
                    continue;
                }
            }
        }
        if !write {
            continue;
        }
        conn.execute(
            "INSERT INTO period_metrics (granularity, period, input_hash, metric_versions,
                metrics, computed_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(granularity, period) DO UPDATE SET input_hash = excluded.input_hash,
                metric_versions = excluded.metric_versions, metrics = excluded.metrics,
                computed_at = excluded.computed_at",
            params![
                granularity,
                p.period,
                p.input_hash,
                serde_json::to_string(&p.metric_versions)?,
                serde_json::to_string(&p.metrics)?,
                now
            ],
        )?;
    }
    Ok(())
}

/// Replaces freshly computed periods with stored ones whose input is
/// unchanged, stores the rest, and recomputes the discontinuities.
pub fn stabilize(conn: &Connection, report: &mut InsightsReport) -> Result<()> {
    let now = chrono::Utc::now().timestamp();
    stabilize_periods(conn, "day", &mut report.days, now, true)?;
    stabilize_periods(conn, "week", &mut report.weeks, now, true)?;
    report.day_discontinuities = discontinuities(&report.days);
    report.week_discontinuities = discontinuities(&report.weeks);
    Ok(())
}

/// Like [`stabilize`] but never stores (for partial date ranges, whose edge
/// weeks are incomplete).
pub fn stabilize_read_only(conn: &Connection, report: &mut InsightsReport) -> Result<()> {
    stabilize_periods(conn, "day", &mut report.days, 0, false)?;
    stabilize_periods(conn, "week", &mut report.weeks, 0, false)?;
    report.day_discontinuities = discontinuities(&report.days);
    report.week_discontinuities = discontinuities(&report.weeks);
    Ok(())
}

/// Forgets every stored result (the next report uses current definitions).
pub fn clear(conn: &Connection) -> Result<usize> {
    Ok(conn.execute("DELETE FROM period_metrics", [])?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::personal::analysis::{report_with, AnalysisContext};
    use crate::personal::dataset::DictationRecord;
    use crate::personal::timeline::Segment;

    fn rec(id: i64, ts: i64, text: &str) -> DictationRecord {
        DictationRecord {
            id,
            created_at: ts,
            raw_asr_text: text.into(),
            vad_segments: vec![Segment {
                start_ms: 0,
                end_ms: 10_000,
            }],
            ..Default::default()
        }
    }

    #[test]
    fn unchanged_periods_keep_their_stored_version_and_changes_are_marked() {
        let dir = tempfile::tempdir().unwrap();
        let conn = open(&db_path(dir.path())).unwrap();
        let day = 86_400;
        let t0 = 1_700_000_000;
        let records = vec![rec(1, t0, "um so we go"), rec(2, t0 + day, "and then")];
        let ctx = AnalysisContext::default();

        // First computation is stored at the current versions.
        let mut first = report_with(&records, None, &ctx);
        stabilize(&conn, &mut first).unwrap();
        assert!(first.days.iter().all(|d| !d.frozen));
        assert!(first.day_discontinuities.is_empty());

        // Simulate an older definition of `fillers` having produced day 1.
        conn.execute(
            "UPDATE period_metrics SET metric_versions = ?1 WHERE granularity = 'day' AND period = ?2",
            params![
                serde_json::to_string(
                    &first.days[0]
                        .metric_versions
                        .iter()
                        .map(|(k, v)| (k.clone(), if k == "fillers" { 0 } else { *v }))
                        .collect::<std::collections::BTreeMap<_, _>>()
                )
                .unwrap(),
                first.days[0].period
            ],
        )
        .unwrap();

        let mut again = report_with(&records, None, &ctx);
        stabilize(&conn, &mut again).unwrap();
        assert!(again.days[0].frozen);
        assert_eq!(again.days[0].metric_versions["fillers"], 0);
        let d = &again.day_discontinuities;
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].period, again.days[1].period);
        assert_eq!(d[0].metrics, vec!["fillers".to_string()]);
        assert_eq!(d[0].kind, "definitions");

        // New input for day 1 → recomputed at the current version.
        let mut more = records.clone();
        more.push(rec(3, t0 + 60, "uh right"));
        let mut changed = report_with(&more, None, &ctx);
        stabilize(&conn, &mut changed).unwrap();
        assert!(!changed.days[0].frozen);
        assert!(changed.day_discontinuities.is_empty());

        // Clearing recomputes everything with current definitions.
        assert!(clear(&conn).unwrap() > 0);
    }
}
