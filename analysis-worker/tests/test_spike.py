from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

from handy_worker import spike
from handy_worker.transcribers.fake import FakeTranscriber
from tests.conftest import HandyFixture


def test_spike_over_history_prints_side_by_side(handy: HandyFixture) -> None:
    handy.add_dictation(100, "so I think we should ship it")
    handy.add_dictation(200, "second one", with_audio=False)  # skipped: no audio
    handy.add_dictation(300, "third one", seconds=2.0)
    items = spike.items_from_history(handy.paths, 5)
    assert [i.parakeet_text for i in items] == ["third one", "so I think we should ship it"]
    out = io.StringIO()
    report = spike.run_spike(FakeTranscriber(), items, out, color=False, as_json=False)
    text = out.getvalue()
    assert "Parakeet raw : third one" in text
    assert "[um]" in text and "<I->" in text and "{we}" in text
    assert "per audio minute" in text
    assert report.audio_s == 3.0
    assert report.items[0].verbatim_counts["fillers"] == 2


def test_spike_over_a_folder_reads_sibling_text(
    tmp_path: Path, wav_factory: Callable[[str, float], Path]
) -> None:
    wav_factory("clip.wav", 1.0)
    (tmp_path / "clip.txt").write_text("reference text")
    items = spike.items_from_folder(tmp_path, None)
    assert len(items) == 1 and items[0].parakeet_text == "reference text"
    out = io.StringIO()
    spike.run_spike(FakeTranscriber(), items, out, color=False, as_json=True)
    data = json.loads(out.getvalue())
    assert data["items"][0]["parakeet"] == "reference text"
    assert data["seconds_per_audio_minute"] >= 0


def test_spike_reports_unreadable_audio(tmp_path: Path) -> None:
    (tmp_path / "bad.wav").write_bytes(b"RIFF....WAVEnope")
    items = spike.items_from_folder(tmp_path, None)
    out = io.StringIO()
    report = spike.run_spike(FakeTranscriber(), items, out, color=False, as_json=False)
    assert report.items[0].error
    assert "1 failed" in out.getvalue()
