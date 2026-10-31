"""Runs the real CrisperWhisper backend code (transformers fork, generate
with word timestamps, stop criteria) against a tiny random model. Skipped
unless the model extra is installed (`uv sync --extra model`)."""

from __future__ import annotations

import array
import json
import math
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from handy_worker.audio import Audio
from handy_worker.transcribe import transcribe_audio
from handy_worker.transcribers.base import Yielded
from handy_worker.transcribers.crisper import (
    CrisperWhisperTranscriber,
    model_state,
    snapshot_dir,
)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    from tests.tiny_whisper import make_tiny_whisper

    root = tmp_path_factory.mktemp("crisper")
    models = root / "models"
    make_tiny_whisper(snapshot_dir(models, "tinyrev"))
    pin = root / "model-pin.json"
    pin.write_text(json.dumps({"model_id": "nyrahealth/CrisperWhisper", "revision": "tinyrev"}))
    return models, pin


def _tone(seconds: float) -> Audio:
    n = int(seconds * 16000)
    return Audio(
        array.array("f", (0.3 * math.sin(2 * math.pi * 220 * i / 16000) for i in range(n)))
    )


def test_loads_offline_from_the_pinned_snapshot(tiny: tuple[Path, Path]) -> None:
    models, pin = tiny
    assert model_state(models, pin)["state"] == "ready"
    t = CrisperWhisperTranscriber(models, pin, device="cpu")
    assert t.is_available()
    t.load()
    try:
        assert t.info().revision == "tinyrev"
        full = transcribe_audio(t, _tone(35.0), lambda: False)
        assert full.chunks == 2
        for w in full.words:
            assert 0 <= w.start <= w.end <= 35.5
            assert w.confidence is None or 0 <= w.confidence <= 1
    finally:
        t.unload()


def test_stop_check_aborts_decoding(tiny: tuple[Path, Path]) -> None:
    models, pin = tiny
    t = CrisperWhisperTranscriber(models, pin, device="cpu")
    t.load()
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    try:
        with pytest.raises(Yielded):
            transcribe_audio(t, _tone(5.0), stop)
        assert calls["n"] <= 6  # stopped within a few decoding steps
    finally:
        t.unload()
