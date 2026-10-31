from __future__ import annotations

import array

import pytest

from handy_worker import audio
from handy_worker.transcribe import DoneChunk, transcribe_audio
from handy_worker.transcribers import Yielded
from handy_worker.transcribers.base import Word
from handy_worker.transcribers.crisper import adjust_pauses
from handy_worker.transcribers.fake import FakeTranscriber
from tests.conftest import tone


def _audio(seconds: float) -> audio.Audio:
    return audio.Audio(array.array("f", tone(seconds)))


def test_long_audio_is_chunked_and_offsets_words() -> None:
    t = FakeTranscriber("hello there")
    t.load()
    full = transcribe_audio(t, _audio(60.0), lambda: False)
    assert full.chunks == 3
    assert full.text == "hello there hello there hello there"
    starts = [w.start for w in full.words]
    assert starts == sorted(starts)
    assert full.words[-1].end == pytest.approx(60.0, abs=0.05)


def test_resumes_from_a_checkpoint() -> None:
    t = FakeTranscriber("x")
    t.load()
    done: list[DoneChunk] = []
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return len(done) == 1  # yield after the first chunk

    with pytest.raises(Yielded):
        transcribe_audio(t, _audio(60.0), stop, on_chunk=lambda c, n: done.append(c))
    assert len(done) == 1 and t.chunks_done == 1
    full = transcribe_audio(t, _audio(60.0), lambda: False, done=done)
    assert t.chunks_done == 3  # only the two missing chunks ran
    assert full.chunks == 3


def test_fake_polls_stop_during_a_chunk() -> None:
    t = FakeTranscriber(work_s=5.0)
    t.load()
    with pytest.raises(Yielded):
        transcribe_audio(t, _audio(2.0), lambda: t.steps >= 3)
    assert t.steps == 3


def test_adjust_pauses_matches_the_model_card() -> None:
    words = [Word("a", 0.0, 1.0), Word("b", 1.05, 2.0), Word("c", 3.0, 4.0)]
    out = adjust_pauses(words, 0.12)
    assert out[0].end == pytest.approx(1.025)
    assert out[1].start == pytest.approx(1.025)
    assert out[1].end == pytest.approx(2.06)
    assert out[2].start == pytest.approx(2.94)
