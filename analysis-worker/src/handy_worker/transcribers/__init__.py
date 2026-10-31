"""Transcriber registry. Add a backend here to swap the verbatim model."""

from __future__ import annotations

from pathlib import Path

from handy_worker.transcribers.base import (
    ChunkResult,
    ModelInfo,
    ModelUnavailableError,
    Transcriber,
    Word,
    Yielded,
)

NAMES = ("crisperwhisper", "fake")
DEFAULT = "crisperwhisper"


def create(name: str, models_dir: Path, pin_file: Path, language: str = "en") -> Transcriber:
    if name == "crisperwhisper":
        from handy_worker.transcribers.crisper import CrisperWhisperTranscriber

        return CrisperWhisperTranscriber(models_dir, pin_file, language=language)
    if name == "fake":
        from handy_worker.transcribers.fake import FakeTranscriber

        return FakeTranscriber()
    raise ValueError(f"unknown transcriber '{name}' (choose from: {', '.join(NAMES)})")


__all__ = [
    "DEFAULT",
    "NAMES",
    "ChunkResult",
    "ModelInfo",
    "ModelUnavailableError",
    "Transcriber",
    "Word",
    "Yielded",
    "create",
]
