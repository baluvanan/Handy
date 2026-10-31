"""A deterministic transcriber for tests and dry runs (no model, no GPU)."""

from __future__ import annotations

import time
from collections.abc import Callable

from handy_worker.audio import Audio
from handy_worker.transcribers.base import (
    ChunkResult,
    ModelInfo,
    ModelUnavailableError,
    StopCheck,
    Word,
    Yielded,
)

DEFAULT_TEXT = "um so I- I think that that we we should uh ship it"


class FakeTranscriber:
    name = "fake"

    def __init__(
        self,
        text: str = DEFAULT_TEXT,
        *,
        work_s: float = 0.0,
        available: bool = True,
        fail: Callable[[Audio], str | None] | None = None,
        on_step: Callable[[int], None] | None = None,
    ) -> None:
        """``work_s`` simulates compute per chunk in 10 ms steps, polling
        ``should_stop`` between steps like a real decoder. ``fail`` returns an
        error message to raise for a given chunk. ``on_step`` lets a test act
        mid-chunk (e.g. create Handy's recording flag)."""
        self.text = text
        self.work_s = work_s
        self.available = available
        self.fail = fail
        self.on_step = on_step
        self.loaded = False
        self.load_count = 0
        self.chunks_done = 0
        self.steps = 0

    def info(self) -> ModelInfo:
        return ModelInfo(
            name=self.name, model_id="fake/verbatim", revision="fake-rev-1", device="cpu"
        )

    def is_available(self) -> bool:
        return self.available

    def load(self) -> None:
        if not self.available:
            raise ModelUnavailableError("fake model is marked unavailable")
        self.loaded = True
        self.load_count += 1

    def unload(self) -> None:
        self.loaded = False

    def transcribe_chunk(self, audio: Audio, should_stop: StopCheck) -> ChunkResult:
        if not self.loaded:
            raise RuntimeError("transcribe_chunk before load")
        steps = max(1, int(self.work_s / 0.01)) if self.work_s > 0 else 1
        for _ in range(steps):
            self.steps += 1
            if self.on_step is not None:
                self.on_step(self.steps)
            if should_stop():
                raise Yielded()
            if self.work_s > 0:
                time.sleep(0.01)
        if self.fail is not None:
            message = self.fail(audio)
            if message:
                raise RuntimeError(message)
        self.chunks_done += 1
        tokens = self.text.split()
        duration = max(audio.duration_s, 0.01)
        per = duration / max(1, len(tokens))
        words = [
            Word(text=t, start=i * per, end=(i + 1) * per, confidence=0.9)
            for i, t in enumerate(tokens)
        ]
        return ChunkResult(text=self.text, words=words)
