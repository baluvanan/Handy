"""The launchd agent: ``~/Library/LaunchAgents/<bundle id>.verbatim-worker.plist``.

launchd does **not** load a shell profile: no PATH from ``~/.zshrc``, no
Homebrew, no pyenv. So the plist uses only absolute paths (the venv's own
Python, which itself points at uv's managed interpreter), sets every
environment variable the worker needs explicitly, and is validated with
``plutil -lint``. ``doctor`` re-runs the worker under exactly this
environment to catch "works in Terminal, fails under launchd".
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from handy_worker.config import WorkerConfig
from handy_worker.paths import WORKER_DIR, Paths

PLUTIL = "/usr/bin/plutil"
LAUNCHCTL = "/bin/launchctl"
#: The only PATH the job gets: system tools (pmset, caffeinate, osascript...).
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class LaunchdError(RuntimeError):
    pass


def venv_python(worker_dir: Path = WORKER_DIR) -> Path:
    """The synced environment's interpreter, by absolute path (not resolved:
    the venv's site-packages hang off this path)."""
    return worker_dir / ".venv" / "bin" / "python"


def job_environment(paths: Paths) -> dict[str, str]:
    return {
        "PATH": LAUNCHD_PATH,
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HANDY_APP_DATA_DIR": str(paths.app_data),
        "HANDY_WORKER_LOG_DIR": str(paths.log_dir),
        "HANDY_BUNDLE_ID": paths.identity.identifier,
        "HANDY_PRODUCT_NAME": paths.identity.product_name,
        # Fully offline at runtime; no telemetry.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "DO_NOT_TRACK": "1",
        "PYTORCH_ENABLE_MPS_FALLBACK": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }


def build_plist(
    paths: Paths, config: WorkerConfig, python: Path, worker_dir: Path = WORKER_DIR
) -> dict[str, Any]:
    return {
        "Label": paths.launchd_label,
        "ProgramArguments": [
            str(python),
            "-m",
            "handy_worker",
            "run",
            "--trigger",
            "launchd",
            "--quiet",
        ],
        "WorkingDirectory": str(worker_dir),
        "EnvironmentVariables": job_environment(paths),
        # Nightly; launchd runs a missed interval on wake if asleep.
        "StartCalendarInterval": {"Hour": config.schedule_hour, "Minute": config.schedule_minute},
        # Catch up at login after the Mac was off.
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "LowPriorityBackgroundIO": True,
        "Nice": 10,
        "StandardOutPath": str(paths.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(paths.log_dir / "launchd.err.log"),
        # Give an in-flight entry time to checkpoint on SIGTERM.
        "ExitTimeOut": 30,
    }


def write_plist(path: Path, plist: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    with tmp.open("wb") as f:
        plistlib.dump(plist, f, sort_keys=True)
    tmp.replace(path)


def lint(path: Path) -> tuple[bool, str]:
    if not Path(PLUTIL).exists():
        # Not macOS: at least make sure it parses back.
        try:
            with path.open("rb") as f:
                plistlib.load(f)
        except Exception as e:
            return False, str(e)
        return True, "parsed (plutil not available)"
    out = subprocess.run([PLUTIL, "-lint", str(path)], capture_output=True, text=True, check=False)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, check=False)


def domain() -> str:
    return f"gui/{os.getuid()}"


@dataclass
class AgentState:
    installed: bool
    loaded: bool
    plist: str
    last_exit: str | None = None
    detail: str = ""


def state(paths: Paths) -> AgentState:
    plist = paths.launch_agent_plist
    installed = plist.exists()
    if sys.platform != "darwin" or not Path(LAUNCHCTL).exists():
        return AgentState(installed, False, str(plist), detail="launchd is macOS-only")
    out = _launchctl("print", f"{domain()}/{paths.launchd_label}")
    if out.returncode != 0:
        return AgentState(installed, False, str(plist), detail=out.stderr.strip())
    last_exit = None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("last exit code"):
            last_exit = line.split("=", 1)[-1].strip()
    return AgentState(installed, True, str(plist), last_exit=last_exit)


def install(paths: Paths, config: WorkerConfig, python: Path | None = None) -> dict[str, Any]:
    python = python or venv_python()
    if not python.exists():
        raise LaunchdError(f"{python} does not exist; run `uv sync --extra model` first")
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    paths.worker_data.mkdir(parents=True, exist_ok=True)
    plist = build_plist(paths, config, python)
    target = paths.launch_agent_plist
    write_plist(target, plist)
    ok, detail = lint(target)
    if not ok:
        target.unlink(missing_ok=True)
        raise LaunchdError(f"plutil -lint rejected the plist: {detail}")
    if sys.platform == "darwin":
        # Reload: bootout is allowed to fail (not loaded yet).
        _launchctl("bootout", f"{domain()}/{paths.launchd_label}")
        out = _launchctl("bootstrap", domain(), str(target))
        if out.returncode != 0:
            raise LaunchdError(f"launchctl bootstrap failed: {out.stderr.strip() or out.stdout}")
        _launchctl("enable", f"{domain()}/{paths.launchd_label}")
    return {"plist": str(target), "label": paths.launchd_label, "lint": detail}


def uninstall(paths: Paths, purge: bool = False) -> dict[str, Any]:
    """Removes the agent. With ``purge`` also the worker's own data (results,
    model, logs) — never anything of Handy's."""
    removed: list[str] = []
    if sys.platform == "darwin":
        _launchctl("bootout", f"{domain()}/{paths.launchd_label}")
    if paths.launch_agent_plist.exists():
        paths.launch_agent_plist.unlink()
        removed.append(str(paths.launch_agent_plist))
    if purge:
        for p in (paths.worker_data, paths.log_dir):
            if p.exists() and _is_worker_owned(p, paths):
                shutil.rmtree(p)
                removed.append(str(p))
    return {"removed": removed, "purged": purge}


def _is_worker_owned(p: Path, paths: Paths) -> bool:
    """Guard against ever deleting Handy's own folders."""
    p = p.resolve()
    if p == paths.worker_data.resolve():
        return True
    # The log dir is shared with nothing of Handy's (Handy logs to
    # ~/Library/Logs/<bundle id>), but only remove it if it holds only ours.
    if p == paths.log_dir.resolve():
        return all(f.name.startswith(("verbatim-worker", "launchd.")) for f in p.iterdir())
    return False
