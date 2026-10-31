from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from handy_worker import launchd
from handy_worker.config import WorkerConfig
from tests.conftest import HandyFixture


def test_plist_contents(handy: HandyFixture, tmp_path: Path) -> None:
    python = tmp_path / "repo" / "analysis-worker" / ".venv" / "bin" / "python"
    cfg = WorkerConfig(schedule_hour=3, schedule_minute=15)
    p = launchd.build_plist(
        handy.paths, cfg, python, worker_dir=tmp_path / "repo" / "analysis-worker"
    )
    assert p["Label"] == "com.example.handy-test.verbatim-worker"
    args = p["ProgramArguments"]
    assert args[0] == str(python) and Path(args[0]).is_absolute()
    assert args[1:] == ["-m", "handy_worker", "run", "--trigger", "launchd", "--quiet"]
    assert p["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert p["RunAtLoad"] is True
    assert p["ProcessType"] == "Background"
    assert p["LowPriorityIO"] is True and p["LowPriorityBackgroundIO"] is True
    assert Path(p["WorkingDirectory"]).is_absolute()
    env = p["EnvironmentVariables"]
    # launchd loads no shell profile: everything explicit and absolute.
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert env["HANDY_APP_DATA_DIR"] == str(handy.paths.app_data)
    assert env["HANDY_BUNDLE_ID"] == "com.example.handy-test"
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert env["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    assert Path(env["HOME"]).is_absolute()
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert p[key].startswith(str(handy.paths.log_dir))
    for value in env.values():
        assert "~" not in value and "$" not in value


def test_plist_round_trips_and_lints(handy: HandyFixture, tmp_path: Path) -> None:
    p = launchd.build_plist(handy.paths, WorkerConfig(), tmp_path / "python")
    target = tmp_path / "agent.plist"
    launchd.write_plist(target, p)
    with target.open("rb") as f:
        assert plistlib.load(f) == p
    ok, _ = launchd.lint(target)
    assert ok
    target.write_bytes(b"<plist><dict><key>broken")
    assert launchd.lint(target)[0] is False


def test_install_requires_a_synced_environment(handy: HandyFixture, tmp_path: Path) -> None:
    with pytest.raises(launchd.LaunchdError, match="uv sync"):
        launchd.install(handy.paths, WorkerConfig(), tmp_path / "nope" / "python")


def test_uninstall_keeps_data_unless_purged(
    handy: HandyFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    handy.add_dictation(100)
    handy.paths.worker_data.mkdir(parents=True, exist_ok=True)
    (handy.paths.worker_data / "verbatim.db").write_text("x")
    handy.paths.log_dir.mkdir(parents=True, exist_ok=True)
    (handy.paths.log_dir / "verbatim-worker.log").write_text("log")
    plist = handy.paths.launch_agent_plist
    launchd.write_plist(plist, {"Label": "x"})

    out = launchd.uninstall(handy.paths)
    assert not plist.exists() and str(plist) in out["removed"]
    assert (handy.paths.worker_data / "verbatim.db").exists()

    launchd.uninstall(handy.paths, purge=True)
    assert not handy.paths.worker_data.exists()
    assert not handy.paths.log_dir.exists()
    # Handy's own data is never touched, even with --purge.
    assert handy.paths.history_db.exists()
    assert (handy.paths.dataset_audio_dir / "handy-100.wav").exists()


def test_purge_leaves_a_shared_log_dir(handy: HandyFixture) -> None:
    handy.paths.log_dir.mkdir(parents=True, exist_ok=True)
    (handy.paths.log_dir / "something-else.log").write_text("keep")
    launchd.uninstall(handy.paths, purge=True)
    assert (handy.paths.log_dir / "something-else.log").exists()
