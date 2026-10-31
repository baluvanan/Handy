"""Test doubles for the service."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeProbe:
    battery: bool | None = False
    low_power: bool | None = False
    thermal: bool | None = False
    disk: int | None = 500 * 1024**3
    memory: int | None = 16 * 1024**3
    load: float | None = 0.1
    idle: float | None = 3600.0
    calls: list[str] = field(default_factory=list)

    def on_battery(self) -> bool | None:
        return self.battery

    def low_power_mode(self) -> bool | None:
        return self.low_power

    def thermal_throttled(self) -> bool | None:
        return self.thermal

    def free_disk_bytes(self, path: Path) -> int | None:
        return self.disk

    def free_memory_bytes(self) -> int | None:
        return self.memory

    def load_per_cpu(self) -> float | None:
        return self.load

    def user_idle_seconds(self) -> float | None:
        return self.idle


def write_busy_flag(path: Path, state: str = "recording", pid: int | None = None) -> None:
    now = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "version": 1,
                "pid": pid or os.getpid(),
                "state": state,
                "since": now,
                "heartbeat": now,
            }
        )
    )
    tmp.replace(path)
