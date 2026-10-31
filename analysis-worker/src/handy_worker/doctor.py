"""``handy-worker doctor``: find what would stop the service, and say how
to fix it.

The most important check re-runs the worker's ``selftest`` under exactly
the environment launchd will give it (``env -i`` + the plist's variables,
working directory and program path), which catches the classic "works in
Terminal, fails under launchd" problems: a missing PATH entry, a relative
path, an interpreter that only exists in a shell profile, unreadable data.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from handy_worker import busy, conditions, handy_db, launchd, lock, versions
from handy_worker.config import WorkerConfig
from handy_worker.paths import WORKER_DIR, Paths
from handy_worker.transcribers import crisper

OK, WARN, FAIL = "ok", "warn", "fail"

#: Where uv usually lives; launchd and GUI apps don't see a shell PATH.
UV_CANDIDATES = (
    "~/.local/bin/uv",
    "~/.cargo/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
)


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def find_uv() -> Path | None:
    for c in UV_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p
    found = shutil.which("uv")
    return Path(found) if found else None


def _check_python() -> Check:
    venv = launchd.venv_python()
    in_venv = Path(sys.prefix).resolve() == (WORKER_DIR / ".venv").resolve()
    if not venv.exists():
        return Check(
            "python",
            FAIL,
            f"no environment at {venv}",
            f"cd {WORKER_DIR} && uv sync --extra model",
        )
    detail = f"{sys.executable} (Python {sys.version.split()[0]})"
    if not in_venv:
        return Check("python", WARN, detail + f"; launchd will use {venv}")
    return Check("python", OK, detail)


def _check_uv() -> Check:
    uv = find_uv()
    if uv is None:
        return Check(
            "uv",
            FAIL,
            "uv not found in the usual absolute locations",
            "Install uv (curl -LsSf https://astral.sh/uv/install.sh | sh) and set its path in "
            "Handy → Settings → Verbatim analysis",
        )
    if str(uv) == shutil.which("uv") and not any(Path(c).expanduser() == uv for c in UV_CANDIDATES):
        return Check(
            "uv",
            WARN,
            f"{uv} (only found through your shell PATH)",
            "Set this absolute path in Handy's verbatim settings; Handy and launchd "
            "don't see your shell PATH",
        )
    return Check("uv", OK, str(uv))


def _check_sync() -> Check:
    rep = versions.report()
    if rep["up_to_date"]:
        return Check("environment", OK, f"worker {rep['worker_version']}, uv.lock in sync")
    return Check(
        "environment",
        FAIL,
        "; ".join(rep["problems"]),
        f"cd {WORKER_DIR} && uv sync --frozen --extra model && "
        ".venv/bin/python -m handy_worker mark-synced (or Handy's Fix button)",
    )


def _check_model_stack(config: WorkerConfig) -> Check:
    if config.transcriber != "crisperwhisper":
        return Check("model stack", OK, f"transcriber '{config.transcriber}' needs none")
    code = (
        "import json, torch, transformers;"
        "print(json.dumps({'torch': torch.__version__, 'transformers': transformers.__version__,"
        " 'mps': bool(torch.backends.mps.is_available())}))"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
        )
    except subprocess.TimeoutExpired:
        return Check("model stack", WARN, "importing torch took over 2 minutes")
    if out.returncode != 0:
        return Check(
            "model stack",
            FAIL,
            (out.stderr.strip().splitlines() or ["import failed"])[-1],
            f"cd {WORKER_DIR} && uv sync --frozen --extra model",
        )
    info = json.loads(out.stdout)
    detail = f"torch {info['torch']}, transformers {info['transformers']}, MPS {info['mps']}"
    if info["transformers"] != "4.37.2":
        return Check(
            "model stack",
            FAIL,
            detail + " (expected the 4.37.2 crisper_whisper fork)",
            f"cd {WORKER_DIR} && uv sync --frozen --extra model",
        )
    if sys.platform == "darwin" and not info["mps"]:
        return Check("model stack", WARN, detail + "; will run on CPU (slower)")
    return Check("model stack", OK, detail)


def _check_model(paths: Paths, config: WorkerConfig) -> Check:
    if config.transcriber != "crisperwhisper":
        return Check("model", OK, "not needed")
    st = crisper.model_state(paths.models_dir, paths.model_pin)
    if st["state"] == "ready":
        return Check(
            "model", OK, f"{st['model_id']}@{st['revision']} ({st['bytes'] / 2**30:.1f} GB)"
        )
    return Check(
        "model",
        FAIL,
        f"{crisper.MODEL_ID}: {st['state']} ({paths.models_dir})",
        "handy-worker model download (one-time network access; licence CC BY-NC 4.0)",
    )


def _check_permissions(paths: Paths) -> list[Check]:
    out: list[Check] = []
    if not paths.app_data.is_dir():
        out.append(
            Check(
                "app data",
                FAIL,
                f"{paths.app_data} does not exist",
                "Start Handy once, or set HANDY_APP_DATA_DIR",
            )
        )
        return out
    try:
        paths.worker_data.mkdir(parents=True, exist_ok=True)
        probe = paths.worker_data / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        out.append(Check("worker data", OK, f"{paths.worker_data} writable"))
    except OSError as e:
        out.append(Check("worker data", FAIL, str(e), f"Check permissions on {paths.worker_data}"))
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        out.append(Check("logs", OK, f"{paths.log_dir} writable"))
    except OSError as e:
        out.append(Check("logs", FAIL, str(e), f"Check permissions on {paths.log_dir}"))
    audio_dir = paths.dataset_audio_dir
    if audio_dir.is_dir():
        sample = next(iter(sorted(audio_dir.iterdir())), None)
        if sample is not None and not os.access(sample, os.R_OK):
            out.append(
                Check(
                    "audio",
                    FAIL,
                    f"cannot read {sample}",
                    "If this is under a protected folder, grant the Python binary Full Disk Access",
                )
            )
        else:
            out.append(Check("audio", OK, f"{audio_dir} readable"))
    return out


def _check_db(paths: Paths) -> Check:
    try:
        snap = handy_db.read_snapshot(paths.history_db)
    except handy_db.UnknownSchemaError as e:
        return Check("Handy database", FAIL, str(e), "git pull the fork, then Fix in Handy")
    except handy_db.HandyDbError as e:
        return Check("Handy database", FAIL, str(e), "Start Handy and dictate once")
    s = snap.schema
    return Check(
        "Handy database",
        OK,
        f"schema v{s.history_version} / dictation log v{s.dictation_log_version}, "
        f"{len(snap.candidates)} dictations with audio",
    )


def _check_resources(
    paths: Paths, config: WorkerConfig, probe: conditions.SystemProbe
) -> list[Check]:
    out: list[Check] = []
    disk = probe.free_disk_bytes(paths.app_data)
    if disk is not None:
        ok = disk >= config.min_free_disk_gb * conditions.GB
        out.append(
            Check(
                "disk",
                OK if ok else FAIL,
                f"{disk / conditions.GB:.1f} GB free",
                "" if ok else "Free some disk space or lower the threshold",
            )
        )
    mem = probe.free_memory_bytes()
    if mem is not None:
        ok = mem >= config.min_free_memory_gb * conditions.GB
        out.append(
            Check(
                "memory",
                OK if ok else WARN,
                f"{mem / conditions.GB:.1f} GB available now",
                "" if ok else "Runs will be skipped while memory is this low",
            )
        )
    return out


def _check_runtime(paths: Paths) -> list[Check]:
    out: list[Check] = []
    info = lock.inspect(paths.lock_file)
    if info.held:
        out.append(Check("lock", OK, f"a run is in progress: {info.holder}"))
    elif info.stale:
        out.append(
            Check("lock", WARN, f"stale lock from {info.holder} (taken over on the next run)")
        )
    flag = busy.read_flag(paths.busy_flag)
    out.append(Check("Handy flag", OK, flag.reason))
    return out


def _launchd_env_selftest(plist: dict[str, Any]) -> Check:
    args = list(plist.get("ProgramArguments") or [])
    env = dict(plist.get("EnvironmentVariables") or {})
    cwd = plist.get("WorkingDirectory")
    if not args:
        return Check(
            "launchd environment", FAIL, "plist has no ProgramArguments", "handy-worker install"
        )
    exe = Path(args[0])
    if not exe.is_absolute():
        return Check(
            "launchd environment", FAIL, f"{exe} is not an absolute path", "handy-worker install"
        )
    if not exe.exists():
        return Check(
            "launchd environment",
            FAIL,
            f"{exe} does not exist",
            f"cd {WORKER_DIR} && uv sync --extra model && handy-worker install",
        )
    if cwd and not Path(cwd).is_dir():
        return Check(
            "launchd environment",
            FAIL,
            f"WorkingDirectory {cwd} is missing",
            "handy-worker install (the checkout moved?)",
        )
    cmd = [args[0], "-m", "handy_worker", "selftest"]
    try:
        out = subprocess.run(
            ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], *cmd],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check("launchd environment", FAIL, f"could not run the self-test: {e}")
    if out.returncode != 0:
        tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
        return Check(
            "launchd environment",
            FAIL,
            "the worker fails under launchd's environment: " + " | ".join(tail),
            "Run `handy-worker doctor --verbose`; reinstall with `handy-worker install`",
        )
    return Check("launchd environment", OK, "self-test passes with launchd's exact environment")


def _check_launchd(paths: Paths, config: WorkerConfig) -> list[Check]:
    out: list[Check] = []
    st = launchd.state(paths)
    plist_path = paths.launch_agent_plist
    if not plist_path.exists():
        out.append(
            Check(
                "launchd agent",
                WARN,
                "not installed",
                "Turn the service on in Handy (or: handy-worker install)",
            )
        )
        return out
    ok, detail = launchd.lint(plist_path)
    if not ok:
        out.append(Check("plist", FAIL, detail, "handy-worker install"))
        return out
    with plist_path.open("rb") as f:
        plist = plistlib.load(f)
    out.append(Check("plist", OK, f"{plist_path} ({detail})"))
    expected = launchd.build_plist(paths, config, Path(plist["ProgramArguments"][0]))
    drift = [
        k
        for k in ("StartCalendarInterval", "EnvironmentVariables", "WorkingDirectory")
        if plist.get(k) != expected.get(k)
    ]
    if drift:
        out.append(
            Check(
                "plist settings",
                WARN,
                f"out of date: {', '.join(drift)}",
                "Re-save Handy's verbatim settings (or: handy-worker install)",
            )
        )
    if sys.platform == "darwin":
        if st.loaded:
            last = st.last_exit or "never run"
            status = OK if st.last_exit in (None, "0", "(never exited)") else WARN
            out.append(
                Check(
                    "launchd agent",
                    status,
                    f"loaded, last exit: {last}",
                    "" if status == OK else f"See {paths.log_dir}/launchd.err.log",
                )
            )
        else:
            out.append(
                Check(
                    "launchd agent",
                    FAIL,
                    f"plist present but not loaded ({st.detail})",
                    "handy-worker install",
                )
            )
    out.append(_launchd_env_selftest(plist))
    return out


def run_checks(paths: Paths, config: WorkerConfig, *, deep: bool = True) -> list[Check]:
    probe = conditions.default_probe()
    steps: list[Callable[[], Check | list[Check]]] = [
        _check_python,
        _check_uv,
        _check_sync,
        lambda: _check_model(paths, config),
        lambda: _check_permissions(paths),
        lambda: _check_db(paths),
        lambda: _check_resources(paths, config, probe),
        lambda: _check_runtime(paths),
        lambda: _check_launchd(paths, config),
    ]
    if deep:
        steps.insert(3, lambda: _check_model_stack(config))
    checks: list[Check] = []
    for step in steps:
        try:
            r = step()
        except Exception as e:  # a broken check must not hide the others
            r = Check(getattr(step, "__name__", "check"), FAIL, f"check crashed: {e}")
        checks.extend(r if isinstance(r, list) else [r])
    return checks


def render(checks: list[Check]) -> str:
    icon = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for c in checks:
        lines.append(f"{icon[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"    fix: {c.fix}")
    return "\n".join(lines)


def as_json(checks: list[Check]) -> dict[str, Any]:
    return {
        "ok": all(c.status != FAIL for c in checks),
        "checks": [asdict(c) for c in checks],
    }


def selftest(paths: Paths, config: WorkerConfig) -> list[str]:
    """What a launchd run needs, checked from inside that environment."""
    problems: list[str] = []
    if not paths.app_data.is_dir():
        problems.append(f"app data {paths.app_data} not found (HOME={os.environ.get('HOME')})")
    try:
        handy_db.read_schema_only(paths.history_db)
    except handy_db.HandyDbError as e:
        problems.append(str(e))
    for tool in (conditions.PMSET, conditions.CAFFEINATE) if sys.platform == "darwin" else ():
        if not Path(tool).exists():
            problems.append(f"{tool} missing")
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        problems.append("HF_HUB_OFFLINE is not 1")
    if config.transcriber == "crisperwhisper":
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError as e:
            problems.append(f"model stack not importable: {e}")
        if crisper.model_state(paths.models_dir, paths.model_pin)["state"] != "ready":
            problems.append("model not downloaded")
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        problems.append(f"log dir not writable: {e}")
    return problems
