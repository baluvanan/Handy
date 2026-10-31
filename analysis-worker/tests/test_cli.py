from __future__ import annotations

import os

import pytest

from handy_worker import cli
from tests.conftest import HandyFixture


def test_offline_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    cli._privacy_env(allow_download=False)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"


def test_spike_cli_with_fake_transcriber(
    handy: HandyFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    handy.add_dictation(100, "hello")
    code = cli.main(
        [
            "--app-data",
            str(handy.paths.app_data),
            "spike",
            "3",
            "--transcriber",
            "fake",
            "--no-color",
        ]
    )
    assert code == 0
    assert "Parakeet raw : hello" in capsys.readouterr().out


def test_spike_cli_reports_an_empty_history(
    handy: HandyFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--app-data", str(handy.paths.app_data), "spike", "--transcriber", "fake"])
    assert code == 1
    assert "No recordings" in capsys.readouterr().err
