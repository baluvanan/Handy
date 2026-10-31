"""Run settings. Handy owns them (Settings → Verbatim analysis) and writes
``<app data>/verbatim/config.json``; the worker only reads it. Missing or
invalid values fall back to the defaults below, so a hand-edited or older
file can never stop a run."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    #: Nightly launchd run (local time).
    schedule_hour: int = 2
    schedule_minute: int = 0
    #: Skip (and stop) while on battery.
    skip_on_battery: bool = True
    #: Nightly runs stop by this time and pick up on the next run.
    time_budget_enabled: bool = True
    stop_by_hour: int = 7
    stop_by_minute: int = 0
    #: Runs need at least this much free disk / memory.
    min_free_disk_gb: float = 5.0
    min_free_memory_gb: float = 3.0
    #: Back off when the 1-minute load average per CPU core exceeds this.
    max_load_per_cpu: float = 0.85
    #: Give up on an entry after this many failures (manual retry resets).
    max_attempts: int = 3
    #: After Handy's recording flag clears, wait this long before resuming.
    resume_cooldown_s: float = 30.0
    #: Pause at most this long for dictation to finish; then end the run.
    max_pause_s: float = 600.0
    #: Idle-triggered runs stop when the user is back (input within this).
    user_active_within_s: float = 30.0
    transcriber: str = "crisperwhisper"
    language: str = "en"
    notify: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_RANGES: dict[str, tuple[float, float]] = {
    "schedule_hour": (0, 23),
    "schedule_minute": (0, 59),
    "stop_by_hour": (0, 23),
    "stop_by_minute": (0, 59),
    "min_free_disk_gb": (0, 10_000),
    "min_free_memory_gb": (0, 1_000),
    "max_load_per_cpu": (0.05, 64),
    "max_attempts": (1, 100),
    "resume_cooldown_s": (0, 3600),
    "max_pause_s": (0, 86_400),
    "user_active_within_s": (0, 3600),
}


def _coerce(name: str, default: Any, value: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise TypeError("expected a boolean")
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
            raise TypeError("expected an integer")
        value = int(value)
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("expected a number")
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str) or not value:
            raise TypeError("expected a non-empty string")
    lo_hi = _RANGES.get(name)
    if lo_hi is not None and not lo_hi[0] <= value <= lo_hi[1]:
        raise ValueError(f"out of range {lo_hi}")
    return value


def from_dict(data: dict[str, Any]) -> WorkerConfig:
    defaults = WorkerConfig()
    values: dict[str, Any] = {}
    for f in fields(WorkerConfig):
        if f.name not in data:
            continue
        default = getattr(defaults, f.name)
        try:
            values[f.name] = _coerce(f.name, default, data[f.name])
        except (TypeError, ValueError) as e:
            log.warning("config: ignoring %s=%r (%s); using %r", f.name, data[f.name], e, default)
    return WorkerConfig(**values)


def load(path: Path) -> WorkerConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WorkerConfig()
    except (OSError, ValueError) as e:
        log.warning("config: cannot read %s (%s); using defaults", path, e)
        return WorkerConfig()
    if not isinstance(raw, dict):
        log.warning("config: %s is not an object; using defaults", path)
        return WorkerConfig()
    return from_dict(raw)
