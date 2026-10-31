"""The transcriber interface: swap the verbatim model without touching the
queue, storage or analysis.

A transcriber turns one chunk (at most ~30 s of 16 kHz mono audio) into text
plus word timings. It must poll ``should_stop`` often (every decoding step
for real models) and raise :class:`Yielded` when it returns True, so the
worker gets off the CPU/GPU within a second when dictation starts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from handy_worker.audio import Audio

StopCheck = Callable[[], bool]


class Yielded(Exception):
    """Work was abandoned because ``should_stop`` asked for it."""


class ModelUnavailableError(RuntimeError):
    """The model is not downloaded, or cannot be loaded."""


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    #: Mean token probability in [0, 1]; None when the model gives none.
    confidence: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "w": self.text,
            "s": round(self.start, 3),
            "e": round(self.end, 3),
            "p": None if self.confidence is None else round(self.confidence, 4),
        }

    @staticmethod
    def from_dict(d: dict[str, object]) -> Word:
        p = d.get("p")
        return Word(
            text=str(d["w"]),
            start=float(d["s"]),  # type: ignore[arg-type]
            end=float(d["e"]),  # type: ignore[arg-type]
            confidence=None if p is None else float(p),  # type: ignore[arg-type]
        )

    def shifted(self, offset: float) -> Word:
        return Word(self.text, self.start + offset, self.end + offset, self.confidence)


@dataclass
class ChunkResult:
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass(frozen=True)
class ModelInfo:
    """What produced a transcript (stored with every result)."""

    name: str
    model_id: str
    revision: str | None
    device: str | None = None


class Transcriber(Protocol):
    name: str

    def info(self) -> ModelInfo: ...

    def is_available(self) -> bool:
        """True when the model files are on disk (no download needed)."""
        ...

    def load(self) -> None:
        """Loads the model; raises ``ModelUnavailableError``."""
        ...

    def transcribe_chunk(self, audio: Audio, should_stop: StopCheck) -> ChunkResult: ...

    def unload(self) -> None:
        """Frees the model's memory (CPU and GPU)."""
        ...
