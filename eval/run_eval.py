#!/usr/bin/env python3
"""Word error rate and latency per model, using Handy's own --transcribe-file.

For every `clip.wav` in the clips folder that has a matching `clip.txt`
reference transcript, run

    handy --transcribe-file clip.wav --model <id> --json

and score the result. Prints a table per model (corpus WER, mean/p90
latency, real-time factor) and optionally writes per-clip JSON.

Stdlib only (Python 3.9+). See eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Fillers dropped from both sides with --ignore-fillers, so a model that keeps
# disfluencies (verbatim) is not penalised against a clean reference, or vice
# versa.
FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm"}

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "percent": "%",
}


def normalize(text: str, ignore_fillers: bool = False) -> list[str]:
    """Lowercase, strip punctuation and accents-insensitive quotes, split.

    Number words and digits are unified ("twelve percent" == "12%"), since
    whether a model writes digits is a formatting choice, not a recognition
    error.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("%", " percent ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 1,000 -> 1000
    text = re.sub(r"[^\w\s']", " ", text)
    words = []
    for w in text.split():
        w = w.strip("'")
        if not w:
            continue
        if ignore_fillers and w in FILLERS:
            continue
        words.append(NUMBER_WORDS.get(w, w))
    return words


def edit_distance(ref: list[str], hyp: list[str]) -> tuple[int, int, int]:
    """(substitutions, deletions, insertions) of the minimum word alignment."""
    # dp[j] = (cost, subs, dels, ins) for ref[:i] vs hyp[:j]
    prev = [(j, 0, 0, j) for j in range(len(hyp) + 1)]
    for i in range(1, len(ref) + 1):
        cur = [(i, 0, i, 0)] + [None] * len(hyp)
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur[j] = prev[j - 1]
                continue
            s, d, n = prev[j - 1], prev[j], cur[j - 1]
            cur[j] = min(
                (s[0] + 1, s[1] + 1, s[2], s[3]),
                (d[0] + 1, d[1], d[2] + 1, d[3]),
                (n[0] + 1, n[1], n[2], n[3] + 1),
            )
        prev = cur
    _, subs, dels, ins = prev[-1]
    return subs, dels, ins


def wer(reference: str, hypothesis: str, ignore_fillers: bool = False) -> tuple[float, int, int]:
    """(WER, errors, reference words)."""
    ref = normalize(reference, ignore_fillers)
    hyp = normalize(hypothesis, ignore_fillers)
    errors = sum(edit_distance(ref, hyp))
    if not ref:
        return (0.0 if not hyp else 1.0), errors, 0
    return errors / len(ref), errors, len(ref)


@dataclass
class ClipResult:
    clip: str
    model: str
    reference: str
    hypothesis: str
    wer: float
    errors: int
    ref_words: int
    audio_secs: float
    transcribe_ms: int
    load_ms: int
    rtf: float
    error: str | None = None


@dataclass
class ModelSummary:
    model: str
    clips: int
    failed: int
    wer: float
    mean_ms: float
    p90_ms: float
    mean_rtf: float
    results: list[ClipResult] = field(default_factory=list)


def find_handy(explicit: str | None) -> list[str]:
    """The command that runs Handy (a list, so tests can pass a fake)."""
    if explicit:
        return explicit.split()
    candidates = [
        "/Applications/Handy Personal.app/Contents/MacOS/handy",
        str(REPO / "src-tauri/target/release/bundle/macos/Handy Personal.app/Contents/MacOS/handy"),
        str(REPO / "src-tauri/target/release/handy"),
        str(REPO / "src-tauri/target/debug/handy"),
    ]
    for c in candidates:
        if os.access(c, os.X_OK):
            return [c]
    found = shutil.which("handy")
    if found:
        return [found]
    sys.exit("error: Handy binary not found; pass --handy /path/to/handy")


def discover_clips(folder: Path) -> list[tuple[Path, str]]:
    clips = []
    for wav in sorted(folder.glob("*.wav")):
        ref = wav.with_suffix(".txt")
        if ref.exists():
            clips.append((wav, ref.read_text(encoding="utf-8").strip()))
        else:
            print(f"skip {wav.name}: no {ref.name}", file=sys.stderr)
    return clips


def transcribe(handy: list[str], wav: Path, model: str | None, timeout: float) -> dict:
    cmd = [*handy, "--transcribe-file", str(wav), "--json"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        raise RuntimeError(tail[0])
    # stdout carries only the JSON object (logs go to stderr), but be lenient.
    out = proc.stdout.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"no JSON in output: {out[:200]!r}")
    return json.loads(out[start:])


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[round((len(s) - 1) * p)]


def evaluate(handy: list[str], clips, models: list[str | None], ignore_fillers: bool,
             timeout: float) -> list[ModelSummary]:
    summaries = []
    for model in models:
        results: list[ClipResult] = []
        for wav, reference in clips:
            try:
                out = transcribe(handy, wav, model, timeout)
                text = out.get("text", "")
                w, errors, n = wer(reference, text, ignore_fillers)
                results.append(ClipResult(
                    clip=wav.name, model=out.get("model", model or "?"),
                    reference=reference, hypothesis=text, wer=w, errors=errors,
                    ref_words=n, audio_secs=float(out.get("audio_secs", 0.0)),
                    transcribe_ms=int(out.get("best_ms", 0)),
                    load_ms=int(out.get("load_ms", 0)), rtf=float(out.get("rtf", 0.0)),
                ))
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                ref_words = len(normalize(reference, ignore_fillers))
                results.append(ClipResult(
                    clip=wav.name, model=model or "?", reference=reference, hypothesis="",
                    wer=1.0, errors=ref_words, ref_words=ref_words, audio_secs=0.0,
                    transcribe_ms=0, load_ms=0, rtf=0.0, error=str(e),
                ))
        ok = [r for r in results if r.error is None]
        total_ref = sum(r.ref_words for r in results)
        name = ok[0].model if ok else (model or "selected")
        summaries.append(ModelSummary(
            model=name,
            clips=len(results),
            failed=len(results) - len(ok),
            # Corpus WER: total errors / total reference words (failures count
            # as all-deleted, so a crashing model can't look good).
            wer=(sum(r.errors for r in results) / total_ref) if total_ref else 0.0,
            mean_ms=statistics.fmean([r.transcribe_ms for r in ok]) if ok else 0.0,
            p90_ms=percentile([r.transcribe_ms for r in ok], 0.9),
            mean_rtf=statistics.fmean([r.rtf for r in ok]) if ok else 0.0,
            results=results,
        ))
    return summaries


def print_table(summaries: list[ModelSummary], verbose: bool) -> None:
    header = f"{'model':<34} {'clips':>5} {'fail':>4} {'WER':>7} {'mean ms':>8} {'p90 ms':>8} {'RTF':>6}"
    print(header)
    print("-" * len(header))
    for s in summaries:
        print(f"{s.model:<34} {s.clips:>5} {s.failed:>4} {s.wer*100:>6.1f}% "
              f"{s.mean_ms:>8.0f} {s.p90_ms:>8.0f} {s.mean_rtf:>5.1f}x")
        if verbose:
            for r in s.results:
                status = f"ERROR {r.error}" if r.error else f"{r.wer*100:5.1f}%  {r.transcribe_ms} ms"
                print(f"    {r.clip:<40} {status}")
                if not r.error and r.errors:
                    print(f"      ref: {r.reference}\n      hyp: {r.hypothesis}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clips", type=Path, default=HERE / "samples",
                    help="folder of .wav + .txt pairs (default: eval/samples)")
    ap.add_argument("--models", default="",
                    help="comma-separated model ids (see `handy --list-models`); "
                         "default: the model selected in the app")
    ap.add_argument("--handy", default=None, help="path to the handy binary")
    ap.add_argument("--ignore-fillers", action="store_true",
                    help="drop um/uh/... from reference and hypothesis before scoring")
    ap.add_argument("--json-out", type=Path, help="write per-clip results here")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per clip")
    ap.add_argument("--max-wer", type=float, default=None,
                    help="exit 1 if any model's WER (0-1) exceeds this (for CI)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    clips = discover_clips(args.clips)
    if not clips:
        print(f"error: no .wav/.txt pairs in {args.clips}", file=sys.stderr)
        return 2
    models: list[str | None] = [m.strip() for m in args.models.split(",") if m.strip()] or [None]
    handy = find_handy(args.handy)
    summaries = evaluate(handy, clips, models, args.ignore_fillers, args.timeout)
    print_table(summaries, args.verbose)
    if args.json_out:
        args.json_out.write_text(json.dumps([asdict(s) for s in summaries], indent=2))
        print(f"\nwrote {args.json_out}")
    if args.max_wer is not None and any(s.wer > args.max_wer for s in summaries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
