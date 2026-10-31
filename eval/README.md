# Evaluation harness

Measures **word error rate (WER)** and **latency** per model on your own
recordings, using the exact transcription path the app uses
(`handy --transcribe-file clip.wav --model <id> --json`).

```bash
# 1. Build (or install) the app first: scripts/build-mac.sh --install
# 2. See which models are installed (✓) and their ids
"/Applications/Handy Personal.app/Contents/MacOS/handy" --list-models

# 3. Run on the bundled synthetic samples
python3 eval/run_eval.py --models handy-computer/whisper-base.en-gguf/whisper-base.en-Q8_0.gguf

# 4. Compare several models on your own clips
python3 eval/run_eval.py \
  --clips ~/handy-eval \
  --models "<model-id-1>,<model-id-2>" \
  --ignore-fillers -v --json-out results.json
```

Output (one row per model):

```
model                              clips fail     WER  mean ms   p90 ms    RTF
------------------------------------------------------------------------------
whisper-base.en-Q8_0                  25    0    6.8%      410      620  14.2x
```

- **WER** is corpus-level: total substitutions + deletions + insertions over
  total reference words. A clip that fails to transcribe counts as fully
  wrong, so a crashing model can't look good.
- **mean/p90 ms** is pure transcription time (`best_ms`; model load is
  excluded and reported per clip in `--json-out`). **RTF** = audio seconds per
  second of compute.
- Text is normalized before scoring: lowercase, punctuation stripped, number
  words unified with digits ("twelve percent" == "12%").
- `--ignore-fillers` drops um/uh/er/... from both sides. Use it when your
  references are clean but you run with **verbatim mode** on (or vice versa).
- `--handy PATH` picks the binary; by default the script looks for
  `/Applications/Handy Personal.app`, then `src-tauri/target/{release,debug}`.
- `--max-wer 0.1` exits non-zero if any model is above 10% (CI gating).

Each clip runs as its own `handy` process, so the model is loaded per clip.
That's slow for big models but keeps the numbers honest (no warm caches
between clips).

## Adding your own recordings

1. Make a folder, e.g. `~/handy-eval/` (keep personal audio **out of the
   repo**).
2. For each clip add `name.wav` and `name.txt`:
   - **WAV: 16 kHz, mono, 16-bit PCM** (what `--transcribe-file` accepts).
     Convert anything else with
     `ffmpeg -i in.m4a -ar 16000 -ac 1 -c:a pcm_s16le name.wav`
     or `afconvert -f WAVE -d LEI16@16000 -c 1 in.m4a name.wav` (built into
     macOS).
   - **TXT: exactly what you said**, one clip per file. Decide whether you
     transcribe fillers verbatim; be consistent and use `--ignore-fillers`
     when comparing against verbatim output.
3. Easiest source: your own dictations. Handy Personal keeps every
   dictation's audio in `~/Library/Application Support/com.baluvanan.handy-personal/dataset/audio/`
   and its texts in `handy --export-dataset <dir>` (`dataset.jsonl`). Copy a
   few WAVs, take `final_text` (or `raw_asr_text`) as a starting point and
   **correct it by hand** — an uncorrected model output is not a reference.
   Note: dictation WAVs have silence removed by VAD.
4. Aim for 20+ clips across the situations you actually dictate in (quiet
   room, headset, laptop mic, fast speech, names and jargon).

## Bundled samples & CI

`samples/` holds three ~6 s synthetic clips generated with eSpeak NG (robotic
voice, so absolute WER on them is not representative of real speech; they
exist so the pipeline can be exercised end to end). CI
(`.github/workflows/personal-eval.yml`) runs the unit tests and a smoke run
over the samples with `fake_handy.py`, a stand-in that echoes the reference,
because CI has no model download. To run the tests locally:

```bash
python3 -m unittest discover -s eval -v
```
