"""Audio loading: mono float32 at 16 kHz.

Handy writes 16-bit mono 16 kHz WAVs; its retention may later compress them
to FLAC. WAV is read with the standard library (no model stack needed);
anything else goes through ``soundfile`` (installed with the ``model``
extra). Files are only ever opened for reading.
"""

from __future__ import annotations

import array
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

TARGET_RATE = 16_000


class AudioError(RuntimeError):
    """The file cannot be read as audio."""


@dataclass
class Audio:
    samples: array.array[float]
    sample_rate: int = TARGET_RATE

    @property
    def duration_s(self) -> float:
        return len(self.samples) / float(self.sample_rate)

    def slice(self, start: int, end: int) -> Audio:
        return Audio(self.samples[start:end], self.sample_rate)


def _pcm_to_float(raw: bytes, width: int) -> array.array[float]:
    if width == 2:
        ints = array.array("h")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        return array.array("f", (v / 32768.0 for v in ints))
    if width == 1:
        return array.array("f", ((b - 128) / 128.0 for b in raw))
    if width == 4:
        ints = array.array("i")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        return array.array("f", (v / 2147483648.0 for v in ints))
    if width == 3:
        out = array.array("f")
        for i in range(0, len(raw) - 2, 3):
            v = int.from_bytes(raw[i : i + 3], "little", signed=True)
            out.append(v / 8388608.0)
        return out
    raise AudioError(f"unsupported WAV sample width: {width} bytes")


def _to_mono(samples: array.array[float], channels: int) -> array.array[float]:
    if channels == 1:
        return samples
    frames = len(samples) // channels
    return array.array(
        "f",
        (sum(samples[f * channels + c] for c in range(channels)) / channels for f in range(frames)),
    )


def resample_linear(
    samples: array.array[float], src_rate: int, dst_rate: int
) -> array.array[float]:
    """Linear-interpolation resampler, good enough for speech ASR input."""
    if src_rate == dst_rate or not samples:
        return samples
    n_out = int(len(samples) * dst_rate / src_rate)
    step = src_rate / dst_rate
    out = array.array("f", bytes(4 * n_out))
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = samples[min(j, last)]
        b = samples[min(j + 1, last)]
        out[i] = a + (b - a) * frac
    return out


def _load_wav(path: Path) -> Audio:
    try:
        with wave.open(str(path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise AudioError(f"{path.name}: not a readable PCM WAV ({e})") from e
    samples = _to_mono(_pcm_to_float(raw, width), channels)
    return Audio(resample_linear(samples, rate, TARGET_RATE))


def _load_soundfile(path: Path) -> Audio:
    try:
        import soundfile as sf
    except ImportError as e:  # pragma: no cover - depends on the model extra
        raise AudioError(
            f"{path.name}: reading {path.suffix} needs the model extra (uv sync --extra model)"
        ) from e
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:  # soundfile raises its own error types
        raise AudioError(f"{path.name}: cannot decode ({e})") from e
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    samples = array.array("f", mono.astype("float32").tobytes())
    return Audio(resample_linear(samples, int(rate), TARGET_RATE))


def load(path: Path) -> Audio:
    """Reads ``path`` (read-only) as mono float32 16 kHz."""
    if not path.is_file():
        raise AudioError(f"{path} does not exist")
    if path.stat().st_size == 0:
        raise AudioError(f"{path.name} is empty")
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError as e:
        raise AudioError(f"{path.name}: cannot read ({e})") from e
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _load_wav(path)
    return _load_soundfile(path)


def write_wav(
    path: Path, samples: list[float] | array.array[float], rate: int = TARGET_RATE
) -> None:
    """16-bit mono WAV writer (tests and fixtures)."""
    ints = array.array("h", (max(-32768, min(32767, round(s * 32767))) for s in samples))
    if sys.byteorder == "big":
        ints.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(ints.tobytes())


def split_points(audio: Audio, max_chunk_s: float = 28.0, search_s: float = 3.0) -> list[int]:
    """Chunk boundaries (sample indices, including 0 and the end) so no chunk
    exceeds ``max_chunk_s``, cutting at the quietest 20 ms window within the
    last ``search_s`` seconds of each chunk so words are not split."""
    n = len(audio.samples)
    rate = audio.sample_rate
    max_len = int(max_chunk_s * rate)
    if n <= max_len:
        return [0, n]
    window = rate // 50
    points = [0]
    start = 0
    while n - start > max_len:
        hi = start + max_len
        lo = max(start + window, hi - int(search_s * rate))
        best, best_energy = hi, float("inf")
        pos = lo
        while pos + window <= hi:
            energy = sum(s * s for s in audio.samples[pos : pos + window])
            if energy < best_energy:
                best, best_energy = pos + window // 2, energy
            pos += window
        points.append(best)
        start = best
    points.append(n)
    return points
