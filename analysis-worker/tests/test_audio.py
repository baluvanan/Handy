from __future__ import annotations

import array
import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from handy_worker import audio
from tests.conftest import tone


def test_reads_handy_wavs(wav_factory: Callable[[str, float], Path]) -> None:
    a = audio.load(wav_factory("a.wav", 1.5))
    assert a.sample_rate == 16_000
    assert abs(a.duration_s - 1.5) < 0.01
    assert max(a.samples) == pytest.approx(0.3, abs=0.01)


def test_resamples_other_rates(tmp_path: Path) -> None:
    p = tmp_path / "b.wav"
    audio.write_wav(p, tone(1.0)[:8000], rate=8000)
    a = audio.load(p)
    assert abs(a.duration_s - 1.0) < 0.01


def test_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "c.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
    with pytest.raises(audio.AudioError):
        audio.load(p)
    empty = tmp_path / "d.wav"
    empty.write_bytes(b"")
    with pytest.raises(audio.AudioError):
        audio.load(empty)
    with pytest.raises(audio.AudioError):
        audio.load(tmp_path / "missing.wav")


def test_split_points_cap_chunk_length() -> None:
    samples = tone(70.0)
    # A quiet gap around 26 s should be preferred as a cut.
    for i in range(26 * 16000, int(26.2 * 16000)):
        samples[i] = 0.0
    a = audio.Audio(array.array("f", samples))
    points = audio.split_points(a, max_chunk_s=28.0)
    assert points[0] == 0 and points[-1] == len(samples)
    for lo, hi in itertools.pairwise(points):
        assert 0 < hi - lo <= 28 * 16000
    assert 26 * 16000 <= points[1] <= 26.2 * 16000


def test_short_audio_is_one_chunk() -> None:
    a = audio.Audio(array.array("f", tone(3.0)))
    assert audio.split_points(a) == [0, len(a.samples)]
