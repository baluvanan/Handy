# handy-worker — verbatim analysis for Handy Personal

Handy's dictation model (Parakeet) drops fillers like "um" and "uh". That is
what you want in pasted text, but speech analysis needs them. `handy-worker`
re-transcribes saved dictations with a **verbatim** model
([CrisperWhisper](https://github.com/nyrahealth/CrisperWhisper)) so the
Insights page can count fillers, false starts and repetitions.

This first cut is the **spike**: a one-off comparison you run by hand. The
background service builds on it.

Everything stays on this Mac. The only network access is the one-time,
pinned model download; every other command runs with `HF_HUB_OFFLINE=1`, and
Hugging Face telemetry is switched off.

## Setup

```bash
cd analysis-worker
uv sync --extra model        # Python 3.11 + the CrisperWhisper stack (locked)
```

`uv sync` without `--extra model` installs only the standard-library core
(enough for the tests and the fake transcriber).

## The spike

```bash
uv run handy-worker spike            # your 5 most recent dictations
uv run handy-worker spike 10         # ... or 10
uv run handy-worker spike ~/clips    # every .wav/.flac in a folder
uv run handy-worker spike --json     # machine-readable
```

For each recording it prints Parakeet's raw output and the verbatim
transcript side by side, with **fillers** `[um]`, **false starts** `<I->` and
**repetitions** `{we}` highlighted (colours on a terminal), the counts found
in each, and the processing time per minute of audio. The summary adds model
load time, peak memory (RSS, and MPS driver memory on Apple Silicon) and
totals.

The first run downloads the model (~3 GB) into Handy's app-data folder; see
the licence note below. Use `--no-download` to fail instead.

In folder mode Parakeet's text comes from a sibling `.txt` file
(`clip.wav` + `clip.txt`), or from the dictation log when the file is one of
Handy's recordings.

## Where things are

- Handy's identity (bundle id `com.baluvanan.handy-personal`, product name
  "Handy Personal") is read from `../src-tauri/tauri.conf.json`, never
  hard-coded. Override with `--app-data` or `HANDY_APP_DATA_DIR`.
- Recordings: Handy's `history.db` (opened **read-only**, `mode=ro` +
  `query_only`, connection closed before any transcription) and the audio in
  `dataset/audio/` (the dictation log's own copies) or `recordings/`.
- Model: `<app data>/verbatim/models/`, revision recorded in
  `<app data>/verbatim/model-pin.json`.

The worker reads the fork's `dictation_log` table rather than upstream's
`transcription_history`: History retention (default "keep the last 5")
deletes History rows and their WAVs within minutes, while the dictation log
and its audio are kept.

## The model and the licence

- **Weights: `nyrahealth/CrisperWhisper`, licensed CC BY-NC 4.0** —
  non-commercial use only. They are never bundled or redistributed; the
  worker downloads them at runtime, once, from Hugging Face.
- **Pinned revision:** `PINNED_REVISION` in
  `src/handy_worker/transcribers/crisper.py`. If it is unset, the first
  download resolves `main` to its exact commit and records it in
  `model-pin.json`; every later load uses that commit offline. Use
  `handy-worker model download --revision <sha>` to pin a specific one.
- **transformers fork:** the model card requires nyrahealth's
  `crisper_whisper` branch of transformers for accurate word timestamps.
  `pyproject.toml` pins it to commit
  `8eb900e81d43576d2ddf69cceb1c2bb8b0330bc1` (transformers 4.37.2), which in
  turn pins `tokenizers<0.19` and `numpy<2`.
- Runs on Apple's MPS GPU backend in float16, falling back to CPU (float32)
  with `PYTORCH_ENABLE_MPS_FALLBACK=1`.
- English by default (`--language`); CrisperWhisper 1.0 also handles German.

The transcriber sits behind a small interface
(`src/handy_worker/transcribers/base.py`): `load`, `transcribe_chunk(audio,
should_stop)`, `unload`. To try another model, add a backend next to
`crisper.py` and register it in `transcribers/__init__.py`. (Nyra has since
published CrisperWhisper 2.0 under a different, non-commercial research
licence and a different API; it would be a separate backend.)

## How a recording is transcribed

1. Audio is loaded read-only as 16 kHz mono and cut into chunks of at most
   28 s, at the quietest 20 ms near each boundary.
2. Each chunk goes through `model.generate` with word-level timestamps from
   cross-attention DTW, as on the model card, then the card's pause
   adjustment. Token probabilities give each word a confidence.
3. A `StoppingCriteria` polls a stop check at every decoding step, so the
   worker can abandon a chunk within a fraction of a second (the background
   service uses this to yield to dictation).

## Development

```bash
uv sync                      # core + dev tools, no model
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Tests use a fake transcriber and build Handy's database from
`tests/fixtures/handy_schema.sql`, which the Rust test
`personal::schema_fixture` regenerates from Handy's real migrations
(`UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture`) and fails on drift.
