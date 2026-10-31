# Handy Personal — Mac checklist

Everything on branch `personal-v1` was built and tested on **Linux only**.
The macOS-only code (pasteboard snapshot/restore, password-field detection,
frontmost-app lookup, notifications) was type-checked and clippy-clean for
`aarch64-apple-darwin`, but has **never run on a Mac**. This list is the
manual verification. Tick things off as you go; section 13 lists what to
watch most closely.

Paths used below:

```bash
APP="/Applications/Handy Personal.app"
HANDY="$APP/Contents/MacOS/handy"                      # CLI entry point
DATA="$HOME/Library/Application Support/com.baluvanan.handy-personal"
```

---

## 1. Pull and build

- [ ] Prerequisites: Xcode Command Line Tools, Rust (stable), Bun, CMake
      (`xcode-select --install`, `brew install cmake`).
- [ ] Get the branch:
  ```bash
  git fetch origin && git checkout personal-v1 && git pull
  ```
- [ ] Find your Apple Development certificate and export it:
  ```bash
  security find-identity -v -p codesigning
  export APPLE_SIGNING_IDENTITY="Apple Development: Your Name (TEAMID)"
  ```
  (Put the export in `~/.zshrc` so every rebuild uses it.)
- [ ] Build and install:
  ```bash
  scripts/build-mac.sh --install
  ```
  Expect `Signing with: Apple Development: ...`, a passing
  `codesign --verify`, and `Identifier=com.baluvanan.handy-personal`.
- [ ] Without the env var the script must print the big **AD-HOC** warning
      and still build (try once with `env -u APPLE_SIGNING_IDENTITY scripts/build-mac.sh`).

## 2. First launch and permissions

- [ ] `open "$APP"`. The window and Dock name say **Handy Personal**.
- [ ] If upstream Handy is installed, it keeps working side by side: both appear
      separately in System Settings → Privacy & Security, and their settings
      don't mix. Quit upstream Handy while testing so the two shortcuts don't
      fight.
- [ ] Grant **Microphone** when prompted.
- [ ] Grant **Accessibility** (System Settings → Privacy & Security →
      Accessibility → enable _Handy Personal_). Needed for pasting, password
      field detection and frontmost-app checks.
- [ ] If the shortcut does nothing, also enable _Handy Personal_ under
      **Input Monitoring**.
- [ ] Models: downloaded models live in the shared Hugging Face cache, so
      ones upstream Handy already downloaded should show as installed. If not,
      download one (e.g. Whisper Base English) in the Models page.
- [ ] **Permissions survive a rebuild:** run `scripts/build-mac.sh --install`
      again, relaunch, dictate. It must paste **without** re-granting
      Accessibility. (If it doesn't, the build was ad-hoc signed; check
      `codesign -dv "$APP"`.)

## 3. Updater is off

- [ ] Debug panel (⌘⇧D): "Check for updates" is shown as disabled by system
      configuration and can't be turned on.
- [ ] The tray menu has no "Check for updates" item.
- [ ] (Optional) `log stream --predicate 'process == "handy"'` shows no
      requests to `github.com/cjpais/Handy/releases`.

## 4. Pasting (the main event)

Settings → Advanced → Output now shows **Reliable Paste** (on by default)
and **Never paste into password fields** (on). Before each test, copy
something recognisable so you can check it was restored.

- [ ] **Slack** (message box): dictate → text appears once, no stray old
      clipboard content. Then press ⌘V: your previous clipboard comes back,
      not the transcript.
- [ ] **Chrome**: a Google Docs page, a plain `<textarea>` (e.g. a GitHub
      comment box) and the address bar. Each pastes once; clipboard restored.
- [ ] **Terminal** (zsh prompt): pastes; clipboard restored.
  - [ ] Turn on Terminal → _Secure Keyboard Entry_, dictate: **nothing is
        pasted**, a notice appears ("Not pasted into a password field"). Turn
        it back off. (Expected: Secure Input blocks the paste by design; turn
        off the setting if this bothers you.)
- [ ] **Password field** (Safari or Chrome login page `<input type=password>`,
      or the password box in System Settings → Users & Groups): dictate →
      **nothing is pasted**, the transcript is in History, a notice is shown
      (toast if the window is open, otherwise a macOS notification).
  - [ ] The macOS notification comes from "Script Editor" (it uses
        `osascript`). If nothing shows, allow Script Editor in System Settings →
        Notifications.
- [ ] **Image restore:** ⌃⇧⌘4 to copy a screenshot region, dictate somewhere,
      then in Preview use File → New from Clipboard: the screenshot is
      intact.
- [ ] **Rich-text restore:** copy bold + a link from Safari or a TextEdit
      rich document. Dictate. Paste into a rich TextEdit document with ⌘V:
      bold and link are kept (all types were restored, not just plain text).
- [ ] **Multiple items:** select 2–3 files in Finder, ⌘C, dictate, then ⌘V in
      another Finder folder: all files are copied.
- [ ] **Your newer copy wins:** dictate into an app that ignores paste (e.g.
      click the Desktop so Finder has no text field), then within ~3 s copy
      some other text. After ~8 s, ⌘V pastes **your** copy, not the old
      clipboard.
- [ ] **Clipboard managers:** with Maccy/Raycast/Paste running, dictate a few
      times. The transcripts do **not** appear in the manager's history.
- [ ] **Legacy fallback:** turn Reliable Paste off, repeat Slack + image
      restore. Still works (fixed delay instead of read receipts). Turn it back
      on.
- [ ] Settings → Advanced → Output → _Clipboard handling: copy to clipboard_:
      after a paste, the transcript stays on the clipboard as plain text.

## 5. LM Studio cleanup

- [ ] Install LM Studio, download a small instruct model (e.g. Qwen2.5 3B
      Instruct or Llama 3.2 3B Instruct), Developer tab → **Start Server**
      (port 1234).
- [ ] Handy: Advanced → Experimental on → enable **Post-processing**. The
      sidebar now shows _Post Process_.
- [ ] Post Process: provider **LM Studio**, base URL
      `http://localhost:1234/v1`. Press refresh: the model list comes from
      LM Studio with **no API key**. Pick the model.
- [ ] Prompt: **Clean up dictation** is selected.
- [ ] Dictate with the post-processing shortcut (⌥⇧Space): fillers and false
      starts are gone, the meaning is unchanged. Ask a question in your
      dictation ("what time is the meeting tomorrow") and check it is pasted
      as a question, **not answered**.
- [ ] **Server off:** stop the LM Studio server, dictate with ⌥⇧Space: the
      rule-cleaned text is pasted almost immediately, plus a notice
      "Cleanup LLM unavailable (LM Studio: unreachable)". Dictation does not fail.
- [ ] **Slow model:** set _LLM timeout_ to 0.5 s and load a big model: the
      rule-cleaned text is pasted after about 0.5 s with a "timeout" notice.
      Reset it to 3 s.
- [ ] Notices are rate-limited: several dictations with the server off give
      one macOS notification per minute.

## 6. Dataset logging and export

- [ ] Advanced → Transcription: _Log dictations for training & analysis_ is
      on. Dictate 3–4 times in different apps.
- [ ] Export:
  ```bash
  "$HANDY" --export-dataset ~/handy-export
  head -n1 ~/handy-export/dataset.jsonl | python3 -m json.tool
  ```
  Check each line has `raw_asr_text`, `rule_cleaned_text`, `llm_text`
  (when post-processed), `final_text`, `model_id`, `speech_audio_ms`,
  `vad_segments`, `latency` (`recording_ms`, `asr_ms`, `cleanup_ms`,
  `llm_ms`, `paste_ms`, `total_ms`), `app_bundle_id` (e.g.
  `com.tinyspeck.slackmacgap`), `outcome`, `audio_path` and
  `audio_exists: true`.
- [ ] `afplay "<audio_path from a line>"` plays that dictation.
- [ ] `--since`: `"$HANDY" --export-dataset ~/x --since 1d` exports fewer
      lines; `--since 2026-01-01` works; `--since banana` errors with exit
      code 2.
- [ ] A dictation that was blocked by a password field has
      `"outcome": "blocked"`; one with the LLM server off has
      `"llm_fallback_reason": "unreachable"`.
- [ ] Set History limit to 5 and dictate 7 times: History deletes the old
      WAVs, but `$DATA/dataset/audio/` still has all of them.

## 7. Verbatim mode

- [ ] Select a **Whisper** model. Turn on _Verbatim transcription_.
- [ ] Dictate with deliberate fillers: "Um, so, uh, I- I think we should,
      like, go."
- [ ] Export again: `raw_asr_text` keeps the fillers/stutter;
      `final_text` has them removed.
- [ ] With a non-Whisper model (Parakeet/Moonshine) verbatim mode does
      nothing (see `docs/personal/verbatim.md`).

## 8. Speech analysis

- [ ] `"$HANDY" --analyze --since 7d | python3 -m json.tool | head -60`
      prints `totals`, `days`, `weeks` with WPM, fillers per minute and
      by word, pauses, false starts, repeats, hedges and MATTR.
- [ ] Sidebar → **Insights**: stat tiles and six trend charts render in
      light and dark mode. Hovering a chart shows the crosshair tooltip.
      7d/30d/90d/All and Daily/Weekly switch the data. "Show as table" works.
- [ ] **Weekly coaching note** (needs the LM Studio server on): returns a
      short, encouraging note with 2–3 things to practise. Optional privacy
      check: watch LM Studio's server log for the request. It contains only
      numbers and filler/hedge words, never your sentences.

## 9. Per-app profiles

Advanced → **App profiles** ships with Slack (casual), Mail (formal) and
terminals (no LLM). "Always" needs post-processing enabled (section 5).

- [ ] Slack, **plain** shortcut (⌥Space): the text goes through the LLM with
      the casual prompt.
- [ ] Mail: the formal prompt.
- [ ] Terminal with the **post-processing** shortcut: no LLM, just the
      rule-cleaned text.
- [ ] Set _Auto-submit: on_ for Slack: the message is sent after pasting. In
      other apps auto-submit follows the global setting.
- [ ] The bundle id field suggests apps you have dictated into (from the
      dataset).

## 10. Evaluation harness

- [ ] `python3 -m unittest discover -s eval -v` passes.
- [ ] `"$HANDY" --list-models` shows installed model ids (✓).
- [ ] `python3 eval/run_eval.py --models "<id1>,<id2>" -v` prints WER and
      latency per model for the 3 synthetic clips (robot voice, so expect
      non-zero WER).
- [ ] Record 10+ of your own clips as described in `eval/README.md` and
      compare models.

## 11. Upstream merges later

```bash
git remote add upstream https://github.com/cjpais/Handy.git   # once
git fetch upstream && git merge upstream/main
```

Fork code lives in `src-tauri/src/personal/`, `src/components/personal/`,
`eval/`, `docs/personal/` and `scripts/build-mac.sh`. Upstream files only get
small hooks; expect conflicts mainly in `actions.rs`, `clipboard.rs`,
`paste_tx/macos.rs`, `settings.rs`, `lib.rs` and `src/bindings.ts`
(regenerate it with `bun run tauri dev`).

## 12. If something breaks

- Pasting misbehaves → turn off **Reliable Paste** (legacy path).
- Password guard too eager (e.g. Terminal with Secure Keyboard Entry) →
  turn off **Never paste into password fields**.
- Logs: `~/Library/Logs/com.baluvanan.handy-personal/` (or Debug panel, ⌘⇧D).
- Reset Accessibility for just this app:
  `tccutil reset Accessibility com.baluvanan.handy-personal`.

## 13. Not verifiable on Linux — confidence

| Area                                                             | What wasn't run                                                                                   | Confidence                                                |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Signed build (`build-mac.sh`)                                    | Never run; Tauri honours `APPLE_SIGNING_IDENTITY`                                                 | Medium-high                                               |
| Grants survive rebuilds                                          | Depends on a stable designated requirement from the cert                                          | High with a real cert                                     |
| Full pasteboard snapshot/restore (`NSPasteboardItem` per type)   | Type-checked for macOS only. Some apps' lazily promised types, and file promises, may not restore | Medium                                                    |
| Session marker + changeCount guard                               | Logic unit-tested; the native marker read was not run                                             | Medium-high                                               |
| `TransientType` respected by clipboard managers                  | Convention only, manager-dependent                                                                | Medium                                                    |
| Password-field detection (AX `AXSecureTextField` + Secure Input) | FFI not run. Chrome/Safari password inputs should report it; Electron apps may not                | Medium                                                    |
| Frontmost-app bundle id (`NSWorkspace`)                          | Not run                                                                                           | High                                                      |
| Notifications via `osascript`                                    | Needs Script Editor notifications allowed                                                         | Medium                                                    |
| Reliable paste as default                                        | Upstream's beta code path, now on by default; marker write added                                  | Medium-high                                               |
| LM Studio model listing and cleanup quality                      | No LM Studio here; client path tested against fake local servers                                  | Medium-high (listing), unknown (prompt quality per model) |
| Verbatim initial prompt effect                                   | No model available (Hugging Face blocked here); API wiring unit-tested                            | Medium (wiring), low-medium (actual filler retention)     |
| VAD segment timestamps                                           | Unit-tested on synthetic frames; not with a real mic                                              | Medium-high                                               |
| Real transcription through `eval/`                               | Could not download a model; tested with a fake binary                                             | Medium-high                                               |
| Insights UI in the real app window                               | Rendered in a browser harness with mocked data                                                    | High                                                      |
