#!/usr/bin/env python3
"""Stand-in for `handy --transcribe-file X.wav --json` used by the tests/CI.

Echoes the clip's reference transcript (or a corrupted one with
FAKE_HANDY_MODE=noisy, or fails with FAKE_HANDY_MODE=fail), so the eval
pipeline can be exercised without a model download.
"""
import json
import os
import sys
import wave
from pathlib import Path

args = sys.argv[1:]
wav = Path(args[args.index("--transcribe-file") + 1])
model = args[args.index("--model") + 1] if "--model" in args else "fake-model"
mode = os.environ.get("FAKE_HANDY_MODE", "perfect")
if mode == "fail":
    print("error: load_model('fake') failed: not installed", file=sys.stderr)
    sys.exit(1)
text = wav.with_suffix(".txt").read_text().strip()
if mode == "noisy":
    words = text.split()
    words[0] = "uh"  # one substitution
    text = " ".join(words[:-1])  # one deletion
with wave.open(str(wav)) as w:
    secs = w.getnframes() / w.getframerate()
print("[INFO] log line on stdout should be tolerated")
print(json.dumps({"model": model, "audio_secs": secs, "load_ms": 5,
                  "transcribe_ms": [120], "best_ms": 120, "rtf": secs / 0.12,
                  "text": text}))
