"""When a run may start, and when it must stop.

All system facts come through ``SystemProbe`` so tests can fake them. The
macOS probe shells out to stock tools by **absolute path** (launchd gives
the worker no shell PATH): ``/usr/bin/pmset``, ``/usr/bin/vm_stat``,
``/usr/sbin/ioreg``. Every probe fails open to "unknown" (None) rather than
crashing; unknown never blocks a run except where noted.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from handy_worker.config import WorkerConfig

PMSET = "/usr/bin/pmset"
VM_STAT = "/usr/bin/vm_stat"
IOREG = "/usr/sbin/ioreg"
CAFFEINATE = "/usr/bin/caffeinate"


class SystemProbe(Protocol):
    def on_battery(self) -> bool | None: ...
    def low_power_mode(self) -> bool | None: ...
    def thermal_throttled(self) -> bool | None: ...
    def free_disk_bytes(self, path: Path) -> int | None: ...
    def free_memory_bytes(self) -> int | None: ...
    def load_per_cpu(self) -> float | None: ...
    def user_idle_seconds(self) -> float | None: ...


def _run(args: list[str], timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def parse_pmset_batt(text: str) -> bool | None:
    if "'Battery Power'" in text:
        return True
    if "'AC Power'" in text or "'UPS Power'" in text:
        return False
    return None


def parse_low_power(text: str) -> bool | None:
    m = re.search(r"^\s*lowpowermode\s+(\d+)", text, re.MULTILINE)
    return None if m is None else m.group(1) != "0"


def parse_therm(text: str) -> bool | None:
    """``pmset -g therm``: throttled if a CPU speed limit below 100 % or a
    non-zero thermal/performance warning level is reported."""
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    if m and int(m.group(1)) < 100:
        return True
    for level in re.findall(r"(?:thermal|performance) warning level.*?(\d+)", text, re.I):
        if int(level) > 0:
            return True
    if "No thermal warning level has been recorded" in text or m:
        return False
    return None


def parse_vm_stat(text: str) -> int | None:
    page = re.search(r"page size of (\d+) bytes", text)
    if not page:
        return None
    size = int(page.group(1))
    total = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(rf"{key}:\s+(\d+)", text)
        if m:
            total += int(m.group(1))
    return total * size


def parse_hid_idle(text: str) -> float | None:
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text)
    return None if m is None else int(m.group(1)) / 1e9


class MacProbe:
    def on_battery(self) -> bool | None:
        out = _run([PMSET, "-g", "batt"])
        return None if out is None else parse_pmset_batt(out)

    def low_power_mode(self) -> bool | None:
        out = _run([PMSET, "-g"])
        return None if out is None else parse_low_power(out)

    def thermal_throttled(self) -> bool | None:
        out = _run([PMSET, "-g", "therm"])
        return None if out is None else parse_therm(out)

    def free_disk_bytes(self, path: Path) -> int | None:
        return _free_disk(path)

    def free_memory_bytes(self) -> int | None:
        out = _run([VM_STAT])
        return None if out is None else parse_vm_stat(out)

    def load_per_cpu(self) -> float | None:
        return _load_per_cpu()

    def user_idle_seconds(self) -> float | None:
        out = _run([IOREG, "-c", "IOHIDSystem", "-d", "4", "-r", "-k", "HIDIdleTime"])
        return None if out is None else parse_hid_idle(out)


class LinuxProbe:
    """Development/CI fallback (the service itself is macOS-only)."""

    def on_battery(self) -> bool | None:
        supplies = Path("/sys/class/power_supply")
        if not supplies.is_dir():
            return None
        online = [p / "online" for p in supplies.iterdir() if (p / "online").exists()]
        if not online:
            return None
        return not any(p.read_text().strip() == "1" for p in online)

    def low_power_mode(self) -> bool | None:
        return None

    def thermal_throttled(self) -> bool | None:
        return None

    def free_disk_bytes(self, path: Path) -> int | None:
        return _free_disk(path)

    def free_memory_bytes(self) -> int | None:
        try:
            text = Path("/proc/meminfo").read_text()
        except OSError:
            return None
        m = re.search(r"MemAvailable:\s+(\d+) kB", text)
        return None if m is None else int(m.group(1)) * 1024

    def load_per_cpu(self) -> float | None:
        return _load_per_cpu()

    def user_idle_seconds(self) -> float | None:
        return None


def _free_disk(path: Path) -> int | None:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _load_per_cpu() -> float | None:
    try:
        return os.getloadavg()[0] / float(os.cpu_count() or 1)
    except OSError:
        return None


def default_probe() -> SystemProbe:
    return MacProbe() if sys.platform == "darwin" else LinuxProbe()


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: list[str]


GB = 1024**3


def check_start(config: WorkerConfig, probe: SystemProbe, data_dir: Path) -> Verdict:
    """Conditions to begin a run (non-negotiable: never degrade dictation)."""
    reasons: list[str] = []
    if config.skip_on_battery and probe.on_battery():
        reasons.append("on battery")
    if probe.low_power_mode():
        reasons.append("Low Power Mode is on")
    if probe.thermal_throttled():
        reasons.append("the Mac is thermally throttled")
    disk = probe.free_disk_bytes(data_dir)
    if disk is not None and disk < config.min_free_disk_gb * GB:
        reasons.append(f"only {disk / GB:.1f} GB free disk (< {config.min_free_disk_gb:g} GB)")
    mem = probe.free_memory_bytes()
    if mem is not None and mem < config.min_free_memory_gb * GB:
        reasons.append(f"only {mem / GB:.1f} GB free memory (< {config.min_free_memory_gb:g} GB)")
    load = probe.load_per_cpu()
    if load is not None and load > config.max_load_per_cpu:
        reasons.append(f"the Mac is busy (load {load:.2f} per core)")
    return Verdict(not reasons, reasons)


def check_continue(config: WorkerConfig, probe: SystemProbe) -> str | None:
    """Conditions that end a run in progress (checked every few seconds).
    Memory/disk/load were checked at start; they only pause between entries
    (see ``runner``) because the worker itself raises them."""
    if config.skip_on_battery and probe.on_battery():
        return "switched to battery"
    if probe.low_power_mode():
        return "Low Power Mode was turned on"
    if probe.thermal_throttled():
        return "the Mac became thermally throttled"
    return None
