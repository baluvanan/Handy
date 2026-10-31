from __future__ import annotations

import json
from pathlib import Path

import pytest

from handy_worker import cli, doctor, runner, status, versions
from handy_worker.config import WorkerConfig
from handy_worker.transcribers.fake import FakeTranscriber
from tests.conftest import HandyFixture
from tests.fakes import FakeProbe


def test_status_reports_queue_runs_and_failures(handy: HandyFixture) -> None:
    for ts in (100, 200, 300):
        handy.add_dictation(ts)
    cfg = WorkerConfig(transcriber="fake")
    fails = FakeTranscriber(fail=lambda a: "boom" if a.duration_s > 0 else None)
    r = runner.Runner(
        handy.paths, cfg, lambda: fails, probe=FakeProbe(), keep_awake=lambda: lambda: None
    )
    r.run(runner.MANUAL)
    s = status.collect(handy.paths, cfg)
    json.dumps(s, default=str)  # serialisable
    assert s["queue"]["pending"] == 3
    assert s["queue"]["dictations_with_audio"] == 3
    assert s["last_run"]["outcome"] == runner.ERROR
    assert len(s["failures"]) == 3 and s["failures"][0]["last_error"]
    assert s["schema"]["ok"] is True
    assert s["versions"]["protocol"] == versions.PROTOCOL
    assert s["running"] is None
    assert s["launchd"]["label"].endswith(".verbatim-worker")
    assert s["disk"]["results_db_bytes"] > 0


def test_status_flags_unknown_schema(handy: HandyFixture) -> None:
    conn = handy.connect()
    conn.execute("PRAGMA user_version = 42")
    conn.commit()
    conn.close()
    s = status.collect(handy.paths, WorkerConfig(transcriber="fake"))
    assert s["schema"]["ok"] is False and not s["ok"]
    assert any("schema" in a for a in s["attention"])


def test_status_and_retry_cli(handy: HandyFixture, capsys: pytest.CaptureFixture[str]) -> None:
    handy.add_dictation(100)
    base = ["--app-data", str(handy.paths.app_data), "--log-dir", str(handy.paths.log_dir)]
    assert cli.main([*base, "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["queue"]["pending"] == 1
    assert cli.main([*base, "retry", "--all-failed"]) == 0
    assert json.loads(capsys.readouterr().out) == {"requeued": 0}
    assert cli.main([*base, "retry", "12345"]) == 1


def test_run_cli_with_fake(handy: HandyFixture, capsys: pytest.CaptureFixture[str]) -> None:
    handy.add_dictation(100)
    base = ["--app-data", str(handy.paths.app_data), "--log-dir", str(handy.paths.log_dir)]
    code = cli.main([*base, "run", "--transcriber", "fake", "--json", "--quiet"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["processed"] in (0, 1)  # 0 when conditions skip (CI box)
    assert handy.paths.log_file.exists()  # rotating log written


def test_doctor_reports_actionable_fixes(handy: HandyFixture) -> None:
    checks = doctor.run_checks(handy.paths, WorkerConfig(), deep=False)
    by_name = {c.name: c for c in checks}
    assert by_name["Handy database"].status == doctor.OK
    model = by_name["model"]
    assert model.status == doctor.FAIL and "model download" in model.fix
    assert by_name["launchd agent"].status == doctor.WARN
    text = doctor.render(checks)
    assert "fix:" in text
    assert doctor.as_json(checks)["ok"] is False


def test_doctor_catches_a_broken_launchd_environment(handy: HandyFixture, tmp_path: Path) -> None:
    plist = {
        "ProgramArguments": [str(tmp_path / "missing" / "python"), "-m", "handy_worker"],
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin"},
        "WorkingDirectory": str(tmp_path),
    }
    c = doctor._launchd_env_selftest(plist)
    assert c.status == doctor.FAIL and "does not exist" in c.detail
    plist["ProgramArguments"] = ["python3", "-m", "handy_worker"]
    assert "absolute" in doctor._launchd_env_selftest(plist).detail


def test_selftest_under_a_launchd_like_environment(handy: HandyFixture, tmp_path: Path) -> None:
    import sys

    plist = {
        "ProgramArguments": [sys.executable, "-m", "handy_worker"],
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "HANDY_APP_DATA_DIR": str(handy.paths.app_data),
            "HANDY_WORKER_LOG_DIR": str(handy.paths.log_dir),
            "HANDY_BUNDLE_ID": "com.example.handy-test",
            "HF_HUB_OFFLINE": "1",
        },
        "WorkingDirectory": str(Path(__file__).parent.parent),
    }
    c = doctor._launchd_env_selftest(plist)
    # The model is not downloaded here, so the self-test must fail and say so.
    assert c.status == doctor.FAIL and "model" in c.detail


def test_versions_detect_an_unsynced_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock-a")
    (tmp_path / "pyproject.toml").write_text('version = "9.9.9"\n')
    rep = versions.report(tmp_path)
    assert not rep["up_to_date"] and rep["source_version"] == "9.9.9"
    versions.mark_synced(tmp_path)
    assert versions.report(tmp_path)["lock_synced"] == versions.lock_hash(tmp_path)
    (tmp_path / "uv.lock").write_text("lock-b")
    assert any("uv.lock" in p for p in versions.report(tmp_path)["problems"])
