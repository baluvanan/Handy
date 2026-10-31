# handy-worker — verbatim analysis for Handy Personal

Handy's dictation model (Parakeet) drops fillers like "um" and "uh". That is
what you want in pasted text, but speech analysis needs them. `handy-worker`
re-transcribes saved dictations with a **verbatim** model
([CrisperWhisper](https://github.com/nyrahealth/CrisperWhisper)) while you're
not using the Mac, and Handy's Insights page uses those transcripts.

It is a background service owned by Handy: Handy installs it, configures it,
triggers it, watches its health and fixes it. Handy is the only thing that
ever changes Handy's data.

## Non-negotiables, and how they are enforced

| Rule                            | Mechanism                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dictation is never degraded** | Handy writes `handy-busy.json` while recording, transcribing, pasting or running a file job (on its own thread, from state it already keeps, nudged on recording start). A worker thread `stat`s it every 100 ms; the model checks a stop flag at **every decoding step** (`StoppingCriteria`), so work stops within a second. The worker then waits for a cooldown and resumes from a per-chunk checkpoint. It also runs at background priority (launchd `ProcessType=Background`, `LowPriorityIO`, `nice`, `taskpolicy -b`), skips runs under heavy load, and never holds a connection to Handy's database during transcription. |
| **No data is ever lost**        | `history.db` is opened read-only (`mode=ro` + `query_only`), only to list entries, then closed. Results go to a separate `verbatim/verbatim.db`. Retention (compress/delete) is implemented in **Handy**, in Rust, and deletes audio only when the worker has a _verified_ transcript. `uninstall --purge` removes only the worker's own data.                                                                                                                                                                                                                                                                                     |
| **Everything stays local**      | The only network access is the one-time, pinned model download (`model download`, or Fix in Handy). Every other command sets `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` before Hugging Face libraries load. Hugging Face telemetry is off (`HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`). The weekly coaching note uses the local LM Studio provider by default and sends aggregated metrics only.                                                                                                                                                                                                                              |

## Architecture

```
 Handy (Rust, always running)                      handy-worker (Python, on demand)
 ───────────────────────────                       ─────────────────────────────────
 recording/transcribing ──► handy-busy.json ─────► StopSignal (100 ms) ─► stop within 1 s
 Settings → Verbatim ──────► verbatim/config.json ─► run rules (schedule, battery, budget)
 toggle on/off ────────────► `handy-worker install/uninstall` ─► ~/Library/LaunchAgents/…plist
 idle ≥ N min, Run now ────► `handy-worker run --trigger idle|manual`
 status panel / health ◄──── `handy-worker status` (JSON)
 Fix / auto-update ────────► `uv sync --frozen --extra model` + `mark-synced`
                                                   launchd: nightly + RunAtLoad ─► `run --trigger launchd`

 history.db (Handy's) ──read-only, list only, closed──► queue ─► CrisperWhisper (MPS) ─►
 verbatim/verbatim.db (worker's) ◄── transcript + word timings + confidences, per entry
 Insights (Rust analysis) ◄── reads verbatim.db read-only, versioned metrics, estimates
 Retention (Rust) ◄── deletes audio only for verified transcripts; updates Handy's records
```

### Files

In Handy's app-data folder (`~/Library/Application Support/<bundle id>/`, the
bundle id read from `src-tauri/tauri.conf.json`):

| Path                                          | Owner                      | What                                                      |
| --------------------------------------------- | -------------------------- | --------------------------------------------------------- |
| `history.db`, `recordings/`, `dataset/audio/` | Handy                      | read-only for the worker                                  |
| `handy-busy.json`                             | Handy                      | "recording active" flag: `{pid, state, since, heartbeat}` |
| `insights.db`                                 | Handy                      | stored per-period metrics with their definition versions  |
| `coaching/notes.json`                         | Handy                      | weekly coaching notes                                     |
| `verbatim/verbatim.db`                        | worker                     | queue state, transcripts, run history (own migrations)    |
| `verbatim/config.json`                        | Handy writes, worker reads | run settings                                              |
| `verbatim/worker.lock`                        | worker                     | single-run lock (flock + PID)                             |
| `verbatim/models/`, `verbatim/model-pin.json` | worker                     | pinned model snapshot                                     |

Logs: `~/Library/Logs/<product name>/verbatim-worker.log` (rotating, 5 × 2 MB),
plus `launchd.out.log` / `launchd.err.log`. Settings → Verbatim analysis →
**Reveal logs** opens the folder.

### What is processed

Each row of the fork's `dictation_log` table that has audio. Upstream History
retention (default "keep the last 5") deletes `transcription_history` rows and
their WAVs within minutes, while the dictation log and its hard-linked audio
in `dataset/audio/` are kept. So the worker keys results by **dictation-log
entry id**, which is the history entry id that survives. The matching History
id is stored alongside when History still has the entry. File imports, which
are not your dictation, are not processed.

### The queue (`run`)

Every run reconciles the queue with Handy's list and processes **everything**
without a verbatim transcript, newest first, so missed runs simply catch up.
Per-entry state:

```
pending ──ok──► done                       (transcript stored, then verified)
pending ──error──► pending (attempts+1) ──3rd failure──► failed   (retry resets)
pending ──audio missing / unreadable / entry deleted──► skipped
skipped(missing audio) ──audio back──► pending
```

Each entry, and each finished ≤28 s chunk of a long entry, is committed
immediately, so an interruption loses at most the chunk in progress.
Verification re-reads the stored row and checks it before it counts. It
requires word timings when there is text, timings inside the audio, and no
empty text for long audio. Retention never deletes audio whose transcript did
not verify.

A run **does not start** when:

- another run holds the lock (a stale lock from a crashed run is taken over);
- Handy's schema is unknown (it stops and reports this in `status` and in the app);
- the queue is empty;
- the Mac is on battery (configurable), in Low Power Mode or thermally
  throttled;
- free disk or memory is below the threshold, or the load per core is too
  high.

A run **stops** when:

- Handy stays busy for more than 10 minutes;
- a nightly run reaches its time budget (07:00 by default);
- an idle-triggered run sees you're back;
- the Mac switches to battery or Low Power Mode;
- memory or disk runs low;
- launchd sends SIGTERM.

When it stops, it unloads the model and lets the Mac sleep again. On mains
power it prevents idle sleep with `caffeinate -i -w <pid>`.

### Triggers

All of these call the same idempotent `run`:

- launchd `StartCalendarInterval`, nightly at 02:00 by default (launchd runs
  a missed interval on wake);
- launchd `RunAtLoad` at login, to catch up after the Mac was off;
- Handy, after you've been idle for N minutes (default 20) with work queued;
- **Run now** on the Insights page.

### Versions

- `status.versions` reports the installed worker version, the checkout's
  `pyproject.toml` version, the protocol number, and the `uv.lock` hash
  against the hash recorded at the last sync (`mark-synced`).
- Handy is built with the worker version from `pyproject.toml` and the
  protocol number. Any mismatch counts as "out of date": Handy re-syncs
  automatically (if allowed) or shows **Fix**.
- `verbatim.db` has its own schema version. Handy reads only versions it
  knows.
- Every speech metric's definition is versioned in Rust
  (`analysis::METRIC_VERSIONS`) and stored with every saved period
  (`insights.db`). A period keeps its stored result while its input is
  unchanged. The charts mark where definitions or the verbatim model
  changed, so a calculation change never looks like a change in how you
  speak. **Recompute with current definitions** rewrites everything.

### Estimates for dictations without a verbatim transcript

The intended estimator counts stretches where the VAD says you were speaking
but Parakeet produced no word as probable fillers. It needs Parakeet's word
timestamps, which Handy does not log: the dictation pipeline keeps only text,
and changing it was out of scope. The estimator is implemented and tested
(`gap_estimate::from_word_gaps`), but it waits for a `word_timings` column in
the dictation log.

What runs today instead estimates from duration. Your own speaking rate,
calibrated on dictations that have a verbatim transcript, says how many
tokens the speech time should hold. The shortfall against Parakeet's words
counts as probable fillers, capped at 35 %. These estimates are kept separate
from measured fillers and drawn as dashed lines.

## Commands

```bash
handy-worker run [--trigger launchd|schedule|login|idle|manual] [--json] [--quiet]
handy-worker status                # JSON: last run, outcome, queue, failures, disk, model, versions, launchd
handy-worker install               # write + lint + load the launchd agent
handy-worker uninstall [--purge]   # remove the agent; --purge also deletes the worker's own data (never Handy's)
handy-worker doctor [--json] [--quick]   # every check, with fixes; re-runs itself under launchd's exact env
handy-worker retry <entry-id> | --all-failed
handy-worker model download [--revision SHA] | status
handy-worker spike [N | DIR]       # side-by-side comparison with Parakeet (see below)
handy-worker versions
```

Exit codes: 0 ok, 1 failed, 2 usage, 3 needs attention (blocked).

`doctor` checks:

- Python and the venv, and `uv` by absolute path (a GUI app and launchd don't
  see your shell PATH);
- that the environment is in sync with `uv.lock`;
- the model stack: torch, the pinned transformers fork, MPS;
- that the model files are present;
- permissions on app data, logs and audio;
- free disk and memory;
- Handy's DB schema compatibility;
- the lock and the busy flag;
- the plist (`plutil -lint`, drift from the current settings, loaded state,
  last exit code);
- the self-test under launchd's environment.

## Setup

Handy does all of this through Settings → Verbatim analysis (turn it on;
**Fix** if anything is missing). By hand:

```bash
cd analysis-worker
uv sync --frozen --extra model         # Python 3.11 + the locked CrisperWhisper stack
.venv/bin/python -m handy_worker mark-synced --extra model
.venv/bin/python -m handy_worker model download    # one-time, pinned
.venv/bin/python -m handy_worker doctor
.venv/bin/python -m handy_worker install
```

### The spike

```bash
uv run handy-worker spike            # your 5 most recent dictations
uv run handy-worker spike ~/clips    # every .wav/.flac in a folder (+ sibling .txt = Parakeet text)
```

It prints Parakeet's raw output next to the verbatim transcript with
**fillers** `[um]`, **false starts** `<I->` and **repetitions** `{we}`
highlighted, plus seconds per audio minute and peak memory (RSS and MPS).

## The model and the licence

- **Weights:** `nyrahealth/CrisperWhisper`, licensed **CC BY-NC 4.0**
  (non-commercial use only). They are never bundled or redistributed: the
  worker downloads them at runtime, once, from Hugging Face, into
  `verbatim/models/`.
- **Pinned revision:** set `PINNED_REVISION` in
  `src/handy_worker/transcribers/crisper.py`, or pass
  `model download --revision <sha>`. If neither is set, the first download
  resolves `main` to its exact commit and records it in `model-pin.json`;
  every later load uses that commit offline.
- **transformers fork:** the model card requires nyrahealth's
  `crisper_whisper` branch for accurate word timestamps. `pyproject.toml`
  pins commit `8eb900e81d43576d2ddf69cceb1c2bb8b0330bc1` (transformers
  4.37.2), which brings `tokenizers<0.19`, `numpy<2` and torch 2.5.
- **Runtime:** Apple's MPS backend in float16, falling back to CPU float32
  (`PYTORCH_ENABLE_MPS_FALLBACK=1`). English by default; CrisperWhisper also
  handles German.
- **Swapping models:** the transcriber is an interface
  (`transcribers/base.py`). Add a backend and register it in
  `transcribers/__init__.py`. Nyra has since released CrisperWhisper 2.0,
  under a different non-commercial research licence and API; it would be a
  separate backend.

## Development

```bash
uv sync                    # core + dev tools, no model
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy                # strict
uv run --extra model pytest tests/test_crisper_backend.py   # real backend on a tiny random model
```

Tests use a fake transcriber and build Handy's database from
`tests/fixtures/handy_schema.sql`. The Rust test `personal::schema_fixture`
regenerates that file from Handy's real migrations
(`UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture`) and fails if the two
drift.
