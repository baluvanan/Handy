"""``handy-worker spike``: try the verbatim model on a few real recordings.

Prints each recording's Parakeet raw output next to the verbatim transcript
with fillers, false starts and repetitions highlighted, plus processing time
per minute of audio and peak memory. Reads Handy's database read-only and
never writes anything except the one-time model download.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from handy_worker import audio as audio_mod
from handy_worker import disfluency, handy_db
from handy_worker.paths import Paths
from handy_worker.transcribe import transcribe_audio
from handy_worker.transcribers.base import Transcriber

AUDIO_SUFFIXES = (".wav", ".flac")


@dataclass
class SpikeItem:
    label: str
    audio_path: Path
    parakeet_text: str | None
    created_at: int | None = None
    entry_id: int | None = None


@dataclass
class ItemResult:
    label: str
    audio_s: float
    processing_s: float
    parakeet: str | None
    verbatim: str
    verbatim_counts: dict[str, int]
    parakeet_counts: dict[str, int] | None
    words: int
    error: str | None = None


@dataclass
class SpikeReport:
    transcriber: str
    model_id: str
    revision: str | None
    device: str | None
    load_s: float
    items: list[ItemResult] = field(default_factory=list)
    peak_rss_bytes: int = 0
    peak_gpu_bytes: int | None = None

    @property
    def audio_s(self) -> float:
        return sum(i.audio_s for i in self.items if i.error is None)

    @property
    def processing_s(self) -> float:
        return sum(i.processing_s for i in self.items if i.error is None)

    @property
    def seconds_per_audio_minute(self) -> float:
        return self.processing_s / (self.audio_s / 60) if self.audio_s > 0 else 0.0


def peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    return int(rss if sys.platform == "darwin" else rss * 1024)


def items_from_history(paths: Paths, count: int) -> list[SpikeItem]:
    """The ``count`` most recent dictations whose audio still exists."""
    snapshot = handy_db.read_snapshot(paths.history_db)  # connection closed on return
    items: list[SpikeItem] = []
    for c in snapshot.candidates:
        found = handy_db.find_audio(c, paths.app_data)
        if found is None:
            continue
        when = datetime.fromtimestamp(c.created_at).strftime("%Y-%m-%d %H:%M")
        items.append(
            SpikeItem(
                label=f"#{c.entry_id}  {when}  ({c.model_id or 'unknown model'})",
                audio_path=found,
                parakeet_text=c.raw_asr_text,
                created_at=c.created_at,
                entry_id=c.entry_id,
            )
        )
        if len(items) >= count:
            break
    return items


def items_from_folder(folder: Path, paths: Paths | None) -> list[SpikeItem]:
    """Audio files in ``folder``. Parakeet's text comes from a sibling .txt
    or, when the file came from Handy, from the dictation log."""
    by_name: dict[str, str] = {}
    if paths is not None and paths.history_db.exists():
        try:
            for c in handy_db.read_snapshot(paths.history_db).candidates:
                if c.audio_file:
                    by_name[c.audio_file] = c.raw_asr_text
        except handy_db.HandyDbError:
            pass
    items: list[SpikeItem] = []
    for f in sorted(folder.iterdir()):
        if f.suffix.lower() not in AUDIO_SUFFIXES or not f.is_file():
            continue
        txt = f.with_suffix(".txt")
        text = txt.read_text(encoding="utf-8").strip() if txt.is_file() else by_name.get(f.name)
        items.append(SpikeItem(label=f.name, audio_path=f, parakeet_text=text))
    return items


def run_spike(
    transcriber: Transcriber, items: list[SpikeItem], out: TextIO, color: bool, as_json: bool
) -> SpikeReport:
    started = time.perf_counter()
    transcriber.load()
    load_s = time.perf_counter() - started
    info = transcriber.info()
    report = SpikeReport(
        transcriber=info.name,
        model_id=info.model_id,
        revision=info.revision,
        device=info.device,
        load_s=load_s,
    )
    gpu_peak = getattr(transcriber, "peak_gpu_bytes", None)
    try:
        for item in items:
            result = _run_item(transcriber, item)
            report.items.append(result)
            if callable(gpu_peak):
                now = gpu_peak()
                if now is not None:
                    report.peak_gpu_bytes = max(report.peak_gpu_bytes or 0, now)
            if not as_json:
                _print_item(out, result, color)
    finally:
        transcriber.unload()
    report.peak_rss_bytes = peak_rss_bytes()
    if as_json:
        out.write(json.dumps(_report_dict(report), indent=2) + "\n")
    else:
        _print_summary(out, report)
    return report


def _run_item(transcriber: Transcriber, item: SpikeItem) -> ItemResult:
    try:
        audio = audio_mod.load(item.audio_path)
    except audio_mod.AudioError as e:
        return ItemResult(item.label, 0, 0, item.parakeet_text, "", {}, None, 0, error=str(e))
    t0 = time.perf_counter()
    try:
        full = transcribe_audio(transcriber, audio, lambda: False)
    except Exception as e:  # the spike reports and moves on
        return ItemResult(
            item.label, audio.duration_s, 0, item.parakeet_text, "", {}, None, 0, error=str(e)
        )
    elapsed = time.perf_counter() - t0
    return ItemResult(
        label=item.label,
        audio_s=audio.duration_s,
        processing_s=elapsed,
        parakeet=item.parakeet_text,
        verbatim=full.text,
        verbatim_counts=disfluency.summary(full.text),
        parakeet_counts=disfluency.summary(item.parakeet_text) if item.parakeet_text else None,
        words=len(full.words),
    )


def _fmt_counts(c: dict[str, int] | None) -> str:
    if not c:
        return "—"
    return (
        f"fillers {c['fillers']} · false starts {c['false_starts']} · "
        f"repetitions {c['repetitions']}"
    )


def _print_item(out: TextIO, r: ItemResult, color: bool) -> None:
    out.write(f"\n── {r.label}  ({r.audio_s:.1f} s audio)\n")
    if r.error:
        out.write(f"   error: {r.error}\n")
        return
    parakeet = disfluency.highlight(r.parakeet, color) if r.parakeet else "— (not available)"
    out.write(f"   Parakeet raw : {parakeet}\n")
    out.write(f"   Verbatim     : {disfluency.highlight(r.verbatim, color) or '— (no speech)'}\n")
    out.write(f"   found        : {_fmt_counts(r.verbatim_counts)}\n")
    out.write(f"   Parakeet had : {_fmt_counts(r.parakeet_counts)}\n")
    per_min = r.processing_s / (r.audio_s / 60) if r.audio_s > 0 else 0.0
    out.write(f"   speed        : {r.processing_s:.2f} s ({per_min:.1f} s per audio minute)\n")


def _print_summary(out: TextIO, rep: SpikeReport) -> None:
    ok = [i for i in rep.items if i.error is None]
    out.write("\n══ Summary\n")
    out.write(f"   model        : {rep.model_id} @ {rep.revision or 'unpinned'} on {rep.device}\n")
    out.write(f"   recordings   : {len(ok)} ok, {len(rep.items) - len(ok)} failed\n")
    out.write(f"   model load   : {rep.load_s:.1f} s\n")
    out.write(
        f"   processing   : {rep.processing_s:.1f} s for {rep.audio_s:.1f} s of audio "
        f"= {rep.seconds_per_audio_minute:.1f} s per audio minute\n"
    )
    out.write(f"   peak memory  : {rep.peak_rss_bytes / 2**30:.2f} GiB RSS")
    if rep.peak_gpu_bytes is not None:
        out.write(f", {rep.peak_gpu_bytes / 2**30:.2f} GiB GPU (MPS driver)")
    out.write("\n")
    totals = {"fillers": 0, "false_starts": 0, "repetitions": 0}
    parakeet_totals = dict.fromkeys(totals, 0)
    for i in ok:
        for k in totals:
            totals[k] += i.verbatim_counts.get(k, 0)
            parakeet_totals[k] += (i.parakeet_counts or {}).get(k, 0)
    out.write(f"   verbatim     : {_fmt_counts(totals)}\n")
    out.write(f"   Parakeet     : {_fmt_counts(parakeet_totals)}\n")
    out.write("   legend       : [filler] <false start> {repetition} (colours on a terminal)\n")


def _report_dict(rep: SpikeReport) -> dict[str, Any]:
    d = asdict(rep)
    d["audio_s"] = rep.audio_s
    d["processing_s"] = rep.processing_s
    d["seconds_per_audio_minute"] = rep.seconds_per_audio_minute
    return d
