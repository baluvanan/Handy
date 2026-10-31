"""Version exchange with Handy.

Handy compares three things to decide whether the worker's environment is
up to date after you pull a new version of the fork:

- ``worker_version`` (installed package) vs ``source_version`` (the
  checkout's pyproject.toml),
- ``lock_synced`` (the uv.lock hash recorded when the environment was last
  synced) vs ``lock_current`` (the checkout's uv.lock),
- ``protocol`` vs the protocol Handy was built for (the status/CLI shape).

Any mismatch → "out of date" → Handy runs ``uv sync --frozen --extra
model`` then ``handy-worker mark-synced``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from handy_worker import __version__
from handy_worker.paths import WORKER_DIR

#: Bump when the CLI/status JSON contract with Handy changes incompatibly.
PROTOCOL = 1
#: Metric definitions are versioned in Handy (Rust); the worker only stores
#: transcripts, whose shape is versioned by results_db.SCHEMA_VERSION.


def sync_marker(worker_dir: Path = WORKER_DIR) -> Path:
    return worker_dir / ".venv" / "handy-worker-sync.json"


def lock_hash(worker_dir: Path = WORKER_DIR) -> str | None:
    try:
        return hashlib.sha256((worker_dir / "uv.lock").read_bytes()).hexdigest()
    except OSError:
        return None


def source_version(worker_dir: Path = WORKER_DIR) -> str | None:
    try:
        text = (worker_dir / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def read_marker(worker_dir: Path = WORKER_DIR) -> dict[str, Any] | None:
    try:
        data = json.loads(sync_marker(worker_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mark_synced(worker_dir: Path = WORKER_DIR, extras: list[str] | None = None) -> dict[str, Any]:
    record = {
        "lock_hash": lock_hash(worker_dir),
        "version": source_version(worker_dir),
        "extras": extras or [],
        "synced_at": int(time.time()),
        "python": sys.executable,
    }
    marker = sync_marker(worker_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def report(worker_dir: Path = WORKER_DIR) -> dict[str, Any]:
    marker = read_marker(worker_dir) or {}
    current = lock_hash(worker_dir)
    src = source_version(worker_dir)
    problems: list[str] = []
    if src and src != __version__:
        problems.append(f"installed worker {__version__} != checkout {src}")
    if current and marker.get("lock_hash") != current:
        problems.append("uv.lock changed since the environment was last synced")
    return {
        "worker_version": __version__,
        "source_version": src,
        "protocol": PROTOCOL,
        "lock_current": current,
        "lock_synced": marker.get("lock_hash"),
        "synced_at": marker.get("synced_at"),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "up_to_date": not problems,
        "problems": problems,
    }
