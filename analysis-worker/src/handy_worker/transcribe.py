"""Chunked transcription of one recording, with resumable progress."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from handy_worker.audio import Audio, split_points
from handy_worker.transcribers.base import ChunkResult, StopCheck, Transcriber, Word, Yielded

MAX_CHUNK_S = 28.0


@dataclass
class DoneChunk:
    index: int
    #: Offset of the chunk in the recording, seconds.
    offset: float
    result: ChunkResult


@dataclass
class FullTranscript:
    text: str
    words: list[Word] = field(default_factory=list)
    chunks: int = 0


def plan_chunks(audio: Audio, max_chunk_s: float = MAX_CHUNK_S) -> list[tuple[int, int]]:
    points = split_points(audio, max_chunk_s=max_chunk_s)
    return list(itertools.pairwise(points))


def transcribe_audio(
    transcriber: Transcriber,
    audio: Audio,
    should_stop: StopCheck,
    *,
    done: Sequence[DoneChunk] = (),
    on_chunk: Callable[[DoneChunk, int], None] | None = None,
    max_chunk_s: float = MAX_CHUNK_S,
) -> FullTranscript:
    """Transcribes ``audio`` chunk by chunk, skipping chunks already in
    ``done`` (a checkpoint from an interrupted run). ``should_stop`` is
    checked before every chunk and passed into the model, which polls it
    during decoding; ``Yielded`` propagates with all finished chunks already
    reported through ``on_chunk(chunk, total_chunks)``."""
    plan = plan_chunks(audio, max_chunk_s)
    finished = {c.index: c for c in done if c.index < len(plan)}
    for index, (start, end) in enumerate(plan):
        if index in finished:
            continue
        if should_stop():
            raise Yielded()
        result = transcriber.transcribe_chunk(audio.slice(start, end), should_stop)
        chunk = DoneChunk(index=index, offset=start / audio.sample_rate, result=result)
        finished[index] = chunk
        if on_chunk is not None:
            on_chunk(chunk, len(plan))
    texts: list[str] = []
    words: list[Word] = []
    for index in range(len(plan)):
        chunk = finished[index]
        if chunk.result.text:
            texts.append(chunk.result.text.strip())
        words.extend(w.shifted(chunk.offset) for w in chunk.result.words)
    return FullTranscript(text=" ".join(t for t in texts if t), words=words, chunks=len(plan))
