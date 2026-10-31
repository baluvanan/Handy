# Verbatim analysis — Mac checklist

Everything on `verbatim-spike` and `verbatim-service` was built and tested on
**Linux only**. That covers Python tests (fake transcriber), Rust tests, and
the real CrisperWhisper code path on a tiny random model on CPU. launchd, MPS,
`pmset`/`caffeinate`/`osascript`, the idle detector and the app UI have
**never run on a Mac**. This is the manual verification, in order. The table
at the end lists what is unverified and how confident I am.

Paths used below:

```bash
REPO=~/path/to/your/Handy          # your checkout of the fork
W="$REPO/analysis-worker"
DATA="$HOME/Library/Application Support/com.baluvanan.handy-personal"
LOGS="$HOME/Library/Logs/Handy Personal"
LABEL=com.baluvanan.handy-personal.verbatim-worker
HW="$W/.venv/bin/python -m handy_worker"
```

---

## 0. Prerequisites

- [ ] `uv` installed at an absolute path Handy can find (`~/.local/bin/uv`
      or `/opt/homebrew/bin/uv`):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ls -l ~/.local/bin/uv
  ```
- [ ] Dataset logging is on (Settings → Advanced → Transcription), and you
      have dictated a few times since then (the worker reads `dictation_log`).
- [ ] About 5 GB of free disk for the model and environment.

## 1. Spike (branch `verbatim-spike`)

- [ ] Check out the spike branch and set up the environment:
  ```bash
  git fetch origin && git checkout verbatim-spike
  cd "$W" && uv sync --frozen --extra model
  ```
  Expect Python 3.11, torch 2.5.1 and transformers 4.37.2 from
  `git+https://github.com/nyrahealth/transformers@8eb900e…`.
- [ ] Run the spike on your last 5 dictations:

  ```bash
  uv run handy-worker spike 5
  ```

  - [ ] The first run downloads the model once (about 3 GB) and prints
        `Pinned revision <sha>`. **Write the sha down.** If you want it
        pinned in the code, set `PINNED_REVISION` in
        `src/handy_worker/transcribers/crisper.py`.
  - [ ] The summary line says `on mps`. If it says `on cpu`, MPS failed: look
        for "trying the next device" in the output.
  - [ ] Fillers show up as `[UM]`/`[UH]` in yellow where you actually said
        them, and Parakeet's line has fewer.
  - [ ] Word timings look sane: run `spike 1 --json` and check that the
        `words` numbers stay within the clip's length.
  - [ ] Note the **s per audio minute** and **peak memory**. Expect roughly
        5–20 s per minute on Apple Silicon. If memory is above about 6 GB,
        say so.

- [ ] Run the spike on a folder of clips:
  ```bash
  uv run handy-worker spike ~/some/clips
  ```
  `.txt` files with the same name as each clip are shown as Parakeet's text.
- [ ] Optional: run the real backend's smoke test on a tiny model:
  ```bash
  uv run --extra model pytest tests/test_crisper_backend.py
  ```
- [ ] **Decide** whether CrisperWhisper is good enough on your voice before
      merging the service. If not, the transcriber is swappable; see the
      README.

## 2. Build Handy with the service (branch `verbatim-service`)

- [ ] Build and install:
  ```bash
  git checkout verbatim-service
  scripts/build-mac.sh --install
  ```
  `analysis-worker/` must stay in this checkout: Handy finds the worker
  there by default.
- [ ] Open Handy → Settings → Advanced → **Verbatim analysis**. The health
      badge shows _Not set up_ (or _Healthy_ if you kept the spike's `.venv`).
- [ ] Turn on **Background verbatim analysis**. You should see "Working:
      install…", then _Healthy_ or _Needs attention: model not downloaded_. - [ ] If it needs attention, press **Fix**. It runs `uv sync`, then
      `mark-synced`, then `model download` (the one-time network
      access), then `install`.
- [ ] Check the agent is installed and valid:
  ```bash
  plutil -lint ~/Library/LaunchAgents/$LABEL.plist
  launchctl print gui/$(id -u)/$LABEL | head -30
  ```
  `ProgramArguments[0]` is `$W/.venv/bin/python`, the `PATH` in the
  environment is `/usr/bin:/bin:/usr/sbin:/sbin`, and `HF_HUB_OFFLINE` is 1.
- [ ] Run **Run checks** (doctor) in settings, or:
  ```bash
  $HW doctor
  ```
  Everything is ✓. Pay attention to the line _"launchd environment:
  self-test passes with launchd's exact environment"_.
- [ ] Bootstrapping the agent fires `RunAtLoad`. In
      `$LOGS/verbatim-worker.log` you see a run start with `trigger login` or
      `schedule`.

## 3. Status panel

- [ ] Insights shows the **Verbatim analysis** panel with:
  - last run, outcome and counts;
  - the waiting queue;
  - transcribed count;
  - model state and revision;
  - disk used and free;
  - versions ("worker 0.2.0 · Handy expects 0.2.0");
  - the schedule installed.
- [ ] Press **Run now**. The button shows _Running…_. When the run finishes,
      the panel and charts refresh on their own.
- [ ] Insights → _"N of M dictations measured from verbatim transcripts."_
      The Fillers chart has a solid line (measured) and, while some
      dictations are still unprocessed, a dashed line (with estimate). A ▼
      marker labelled "verbatim transcripts start" appears where verbatim data
      begins.

## 4. Dictating during an active run (non-negotiable #1)

- [ ] Queue some work: if everything is already done, retry it all with
  ```bash
  sqlite3 "$DATA/verbatim/verbatim.db" "UPDATE entries SET status='failed' WHERE rowid IN (SELECT rowid FROM entries LIMIT 20)"
  $HW retry --all-failed
  ```
- [ ] Press **Run now**, then watch:
  ```bash
  tail -f "$LOGS/verbatim-worker.log"
  ```
  Wait until it is transcribing; Activity Monitor shows `python` using the
  GPU or CPU.
- [ ] Dictate a long sentence. Within about a second the log shows
      `yielding to Handy (Handy is dictating)`. While you dictate, check
      that `cat "$DATA/handy-busy.json"` shows `"state":"dictating"`.
- [ ] **No noticeable lag**: start-of-recording sound, overlay and the paste
      feel exactly as with the service off. Do 5 dictations back to back.
- [ ] About 30 s after your last dictation (the cooldown), the log shows the
      worker resuming. Long entries resume from the chunk they stopped in.
- [ ] Quit Handy with ⌘Q while a run is going. `handy-busy.json` is gone,
      and the worker carries on without blocking. A Handy crash is also safe:
      the flag's PID is dead, so the worker ignores it.

## 5. Missed run and catch-up after login

- [ ] Set the nightly run to a few minutes from now (Settings → Nightly
      run). Doctor's "plist settings" line shows no drift, so the agent was
      re-installed.
- [ ] Shut the Mac down before that time. Log in after it.
- [ ] Within a minute of login, the log shows a run with `trigger login`
      that processes everything queued (`Queue: N new`, then `done …`), and
      the panel's last run updates.
- [ ] Set the time back to 02:00.

## 6. Battery, time budget, conditions

- [ ] Unplug, then press Run now → the outcome is **Skipped — on battery**.
- [ ] Plug in, start a run, then unplug mid-run → **Stopped early — switched
      to battery**. Entries finished so far are kept.
- [ ] Turn **Skip on battery** off → runs on battery work.
- [ ] Turn on Low Power Mode (System Settings → Battery) → skipped with
      "Low Power Mode is on".
- [ ] Time budget: set **Stop by** to 2 minutes from now, set the nightly run
      to now, and queue work.
  - [ ] Launch the job as launchd would:
        `launchctl kickstart gui/$(id -u)/$LABEL`.
  - [ ] It ends with **Stopped early — time budget reached (HH:MM)**, and
        `queue_after` > 0.
  - [ ] Restore the defaults afterwards.
- [ ] While a run is going on mains power,
      `pmset -g assertions | grep caffeinate` shows `PreventUserIdleSystemSleep`
      from caffeinate. It disappears when the run ends.
- [ ] `ps -o pid,nice,command -p <worker pid>` shows nice 10.
      `taskinfo <pid>` (if available) shows background QoS.
- [ ] After a run, Activity Monitor shows the Python process gone: the model
      was unloaded and the process exited.
- [ ] **Idle trigger:** set "Idle for" to 5 minutes, queue work, and leave
      the Mac untouched for 5 minutes. A run starts (`trigger idle`). Move
      the mouse and within a few seconds it stops with "you're back at the
      Mac".

## 7. Failure notification

- [ ] Simulate a missing model: `mv "$DATA/verbatim/models" "$DATA/verbatim/models.off"`.
- [ ] Press Run now → the outcome is **Needs attention — model unavailable**.
      A macOS notification appears **once** ("Handy Personal: Verbatim
      analysis needs attention"). It comes from Script Editor via osascript;
      allow Script Editor in System Settings → Notifications if needed.
- [ ] Press Run now again → no second notification.
- [ ] The panel shows the attention message, and **Fix** re-downloads the
      model. Instead of letting it, put the folder back:
      `mv "$DATA/verbatim/models.off" "$DATA/verbatim/models"`.
- [ ] Run now → **Done**; the attention message clears.
- [ ] Repeated failures: make one recording unreadable with
      `cp /dev/null "$DATA/dataset/audio/<one file>.wav"` on a **copy** you
      made first. It shows as _skipped (unreadable_audio)_, not failed. Put
      the original back, then press **Retry**.

## 8. Retention

- [ ] Settings → Retention rule → **Preview**.
  - [ ] The counts look right: nothing younger than 30 days is listed.
  - [ ] The delete list contains only recordings older than 180 days that
        have a verified transcript.
  - [ ] "Kept regardless" counts the unprocessed and unverified ones.
- [ ] Back up first: `cp -c -R "$DATA" "$DATA.bak"` (APFS clone; fast and
      takes no space).
- [ ] Turn retention on → the preview dialog → **Enable retention**. The
      first pass runs within a minute while you're not dictating.
- [ ] In `$DATA/dataset/audio`, recordings older than 30 days are now `.flac`
      and the WAVs are gone.
- [ ] Check the records point at the new files:
  ```bash
  sqlite3 "$DATA/history.db" "SELECT audio_file, audio_path FROM dictation_log ORDER BY id LIMIT 5"
  ```
- [ ] **Playback still works** for a compressed entry that History still
      shows: set History to keep everything (Settings → History → never
      delete), compress, then play it in History. If WebKit can't play FLAC,
      **tell me**. That would be the one thing to fix before relying on
      retention.
- [ ] Re-transcribe (History → retry) a compressed entry: it works.
- [ ] Run `$HW run --trigger manual` → no entries are re-queued because of
      compression.

## 9. Updating the worker

- [ ] Simulate pulling a new version: bump `version` in
      `analysis-worker/pyproject.toml`, then
      `cd "$W" && uv lock && cd "$REPO" && scripts/build-mac.sh --install`.
- [ ] Within a minute the panel shows _Out of date_. With **Update the
      worker automatically** on, it re-syncs by itself ("Working:
      update…"). With it off, **Fix** does it.
- [ ] Revert the version bump.

## 10. Weekly coaching note

- [ ] Start LM Studio's server with a model selected for the LM Studio
      provider (Settings → Post-processing).
- [ ] On a Monday, or by deleting `$DATA/coaching/notes.json`, the panel
      shows a "Weekly notes" entry for last week within a few minutes.
- [ ] Turn on LM Studio's request log: the request contains only aggregate
      JSON, never transcript text.

## 11. Clean uninstall that keeps your data

- [ ] Turn **Background verbatim analysis** off.
  - [ ] `launchctl print gui/$(id -u)/$LABEL` fails (not loaded), and the
        plist is gone.
  - [ ] `$DATA/verbatim/verbatim.db`, the model, `history.db`, `dataset/`
        and `recordings/` are all still there.
  - [ ] Insights still uses the verbatim transcripts.
- [ ] Optional full removal of the worker's own data only:
  ```bash
  $HW uninstall --purge
  ```
  It removes `$DATA/verbatim/` and the worker log folder. `history.db`,
  `dataset/`, `recordings/`, `insights.db` and `coaching/` stay.
- [ ] Turn it back on → Fix → everything is rebuilt from Handy's data.

---

## What I could not verify on Linux

| Area                                        | What's unverified                                                                                                                                                                                   | Confidence                                                                 |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| CrisperWhisper with the real weights        | Download, the pinned revision, transcript quality, filler style (`[UM]`/`[UH]`), word timing accuracy. The code path itself was run on a tiny random model through the pinned fork.                 | Medium–high for the code path; **unknown** for quality on your voice       |
| MPS                                         | float16 inference, the CPU fallback, peak memory and speed                                                                                                                                          | Medium (MPS + fp16 Whisper is common; transformers 4.37 is old)            |
| Aborting decoding within 1 s                | Each decoding step polls the stop check, so worst case is the 30 s encoder pass plus one step. On MPS the encoder should be well under 1 s for large-v2 class models, but this is not measured.     | Medium                                                                     |
| Handy's busy flag in the real app           | The flag is written from tray state, the recording state and the job flag, polled at 250 ms and nudged on start. The writer logic and file format are tested; the wiring in the running app is not. | Medium–high                                                                |
| No lag while dictating                      | Depends on GPU contention between ggml-Metal (Parakeet) and torch-MPS while the worker yields.                                                                                                      | Medium; this is the most important manual test (§4)                        |
| launchd                                     | bootstrap/bootout/kickstart, `RunAtLoad` at login, catching up a calendar interval on wake, `ProcessType`, `ExitTimeOut`                                                                            | Medium–high (standard keys; plist content is tested, `plutil` was not run) |
| `doctor`'s launchd self-test                | Runs `env -i` with the plist environment; tested on Linux only                                                                                                                                      | High                                                                       |
| `pmset` / `vm_stat` / `ioreg` parsing       | Parsers tested on sample output written from memory. The thermal output on Apple Silicon is the least certain.                                                                                      | Medium (thermal: low–medium)                                               |
| `caffeinate -i -w`, `taskpolicy -b`, `nice` | Standard tools, not run                                                                                                                                                                             | High                                                                       |
| Idle time (CoreGraphics FFI)                | `CGEventSourceSecondsSinceLastEventType` through a direct `extern "C"` call                                                                                                                         | Medium–high                                                                |
| Notifications                               | osascript, the same mechanism as the existing personal-v1 notices                                                                                                                                   | High that it works; it appears under Script Editor                         |
| FLAC playback in History                    | WebKit should play FLAC (Safari 11+). Symphonia decoding is tested, including the STREAMINFO fix.                                                                                                   | Medium–high                                                                |
| Retention on real data                      | Logic and safety tested on fixtures built from the real schema. Hard-link behaviour on APFS is not tested.                                                                                          | High for safety, medium for space savings                                  |
| Settings UI / status panel / charts         | TypeScript type-checks and lints; not rendered in a browser                                                                                                                                         | Medium                                                                     |
| Auto-update via `uv sync` from a GUI app    | uv found by absolute path, PATH set explicitly; uv's cache under `$HOME`                                                                                                                            | Medium–high                                                                |
| Coaching note scheduling                    | Uses the existing LLM client; the schedule logic is tested                                                                                                                                          | Medium–high                                                                |
