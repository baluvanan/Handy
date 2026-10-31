"""``handy-worker status``: one JSON document Handy renders on Insights.

Cheap and side-effect free: reads the worker's own DB, Handy's DB
read-only (for the queue length and schema), and files on disk. Never
imports the model stack.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from handy_worker import busy, conditions, handy_db, launchd, lock, versions
from handy_worker import results_db as rdb
from handy_worker.config import WorkerConfig
from handy_worker.paths import Paths
from handy_worker.runner import ATTENTION_KEY
from handy_worker.transcribers import crisper


def _dir_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _results(paths: Paths) -> dict[str, Any]:
    if not paths.results_db.exists():
        return {
            "counts": None,
            "last_run": None,
            "recent_runs": [],
            "failures": [],
            "attention": None,
        }
    try:
        db = rdb.ResultsDb.open(paths.results_db)
    except (sqlite3.Error, RuntimeError) as e:
        return {
            "error": str(e),
            "counts": None,
            "last_run": None,
            "recent_runs": [],
            "failures": [],
        }
    try:
        runs = db.recent_runs(10)
        settled = {
            int(r[0])
            for r in db.conn.execute(
                "SELECT entry_id FROM entries WHERE status IN ('done', 'failed', 'skipped')"
            )
        }
        return {
            "settled": settled,
            "schema_version": db.version(),
            "counts": db.counts(),
            "last_run": runs[0] if runs else None,
            "recent_runs": runs,
            "failures": db.failures(),
            "attention": db.get_meta(ATTENTION_KEY),
        }
    finally:
        db.close()


def _queue_length(paths: Paths, settled: set[int]) -> dict[str, Any]:
    """Pending = dictations with audio that have no verbatim transcript and
    are not given up/skipped (reconciled the same way a run does)."""
    try:
        snapshot = handy_db.read_snapshot(paths.history_db)
    except handy_db.UnknownSchemaError as e:
        return {"schema": {"ok": False, "error": str(e)}, "pending": None}
    except handy_db.HandyDbError as e:
        return {"schema": {"ok": False, "error": str(e)}, "pending": None}
    pending = sum(1 for c in snapshot.candidates if c.entry_id not in settled)
    return {
        "schema": {"ok": True, **snapshot.schema.as_dict()},
        "pending": pending,
        "dictations_with_audio": len(snapshot.candidates),
    }


def collect(paths: Paths, config: WorkerConfig) -> dict[str, Any]:
    lock_info = lock.inspect(paths.lock_file)
    agent = launchd.state(paths)
    flag = busy.read_flag(paths.busy_flag)
    probe = conditions.default_probe()
    results = _results(paths)
    queue = _queue_length(paths, results.pop("settled", set()))
    attention: list[str] = []
    if results.get("attention"):
        attention.append(str(results["attention"]))
    if not queue["schema"]["ok"]:
        attention.append(str(queue["schema"]["error"]))
    model = crisper.model_state(paths.models_dir, paths.model_pin)
    if config.transcriber == "crisperwhisper" and model["state"] != "ready":
        attention.append("the verbatim model is not downloaded")
    vers = versions.report()
    if not vers["up_to_date"]:
        attention.append("the worker environment is out of date: " + "; ".join(vers["problems"]))
    return {
        "ok": not attention,
        "attention": attention,
        "versions": vers,
        "paths": {
            "app_data": str(paths.app_data),
            "results_db": str(paths.results_db),
            "log_file": str(paths.log_file),
            "log_dir": str(paths.log_dir),
        },
        "running": lock_info.holder if lock_info.held else None,
        "stale_lock": lock_info.holder if lock_info.stale else None,
        "handy_busy": {"active": flag.active, "reason": flag.reason},
        "queue": {
            "pending": queue["pending"],
            "dictations_with_audio": queue.get("dictations_with_audio"),
            **(results.get("counts") or {}),
        },
        "schema": queue["schema"],
        "last_run": results.get("last_run"),
        "recent_runs": results.get("recent_runs", []),
        "failures": results.get("failures", []),
        "model": model,
        "disk": {
            "worker_bytes": _dir_bytes(paths.worker_data),
            "model_bytes": int(model.get("bytes") or 0),
            "results_db_bytes": paths.results_db.stat().st_size if paths.results_db.exists() else 0,
            "free_bytes": probe.free_disk_bytes(paths.worker_data),
        },
        "launchd": {
            "installed": agent.installed,
            "loaded": agent.loaded,
            "plist": agent.plist,
            "last_exit": agent.last_exit,
            "label": paths.launchd_label,
        },
        "config": config.as_dict(),
    }
