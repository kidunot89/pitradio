Build me a small standalone push-to-talk dictation app for sim racing.

## What it does

I hold a key. The app sends configurable keystrokes to whatever application has
focus (to open the game's chat box), captures my speech, and on key release
transcribes it, types the text into the game, and sends configurable keystrokes
afterwards (to send the message). Example: in Le Mans Ultimate, Enter opens
chat and Enter sends it.

## Hard constraints

- Single standalone process. No server, no network component, no client/server
  split, nothing to deploy anywhere.
- Runs on Windows. I develop on macOS, so assume none of the Windows input code
  can be tested locally — it only runs on the target machine. Don't build
  cross-platform abstractions for testing; write Windows-specific code.
- Must work across all my racing sims, not one. Per-sim behaviour lives in a
  config file keyed on the focused executable name.
- Python. Keep it small — a handful of files, not a framework.

## Design decisions already made — implement these, don't relitigate them

- Use a low-level keyboard hook (`WH_KEYBOARD_LL`) via ctypes, NOT
  `RegisterHotKey`. I need key-down and key-up for hold semantics, and I need
  to swallow the trigger key so it never reaches the game and ends up in the
  chat box. Return 1 from the hook for the trigger key.
- Transcribe in one batch on key release, not streaming. Push-to-talk clips are
  short; batch is more accurate and keeps the state machine trivial.
- Use `faster-whisper` with `small.en` at int8, running on **CPU, not GPU**.
  The GPU is fully committed to the sim — a model grabbing VRAM mid-corner
  costs frames. CPU has headroom.
- Do NOT use Windows Voice Typing (Win+H). It's a toggle with no programmatic
  API, it stops the moment you press Enter or change focus, and it's cloud
  based with a floating overlay. All three break this design.
- Inject keys using hardware scan codes (`KEYEVENTF_SCANCODE`). Inject text
  using `KEYEVENTF_UNICODE`, one UTF-16 code unit at a time.
- Trigger key defaults to F13 — unbound in every sim. I map a Fanatec wheel
  button to F13 externally via JoyToKey, so from the app's point of view it's
  just a keyboard key.
- Ignore keyboard auto-repeat on the trigger key.
- Tag the app's own synthetic input via `dwExtraInfo` with a magic constant, and
  have the hook ignore anything carrying that tag.

## Non-obvious things that will break it if you miss them

- **UIPI**: if a sim or its launcher runs elevated, `SendInput` from a
  non-elevated process fails *silently* — no exception, nothing happens.
  Document that the app must run as administrator.
- The low-level hook needs a message pump (`GetMessageW` loop) on its own
  thread, or Windows silently unregisters it after a timeout.
- Each synthetic key must be held roughly 40ms between down and up. Games poll
  input once per frame; zero-length presses get dropped entirely.
- There must be a configurable delay after the pre-keys before anything is
  typed — the chat box needs several frames to open and take focus. Too short
  and the opening characters vanish.
- The hook callback must return fast. Push events onto a queue and do all real
  work (audio, transcription, injection) on a worker thread.
- If nothing was said, don't type or send. Fire configurable abort keys instead
  (e.g. Escape) so an empty message isn't posted.

## Config

JSON file, hot-reloaded on change so I can tune delays without restarting.
Structure: global trigger key, audio settings, whisper settings, a
`default_profile`, and a `profiles` map keyed on lowercase executable name that
overrides the default.

Per-profile settings: `pre_keys`, `post_keys`, `abort_keys`, `pre_delay_ms`,
`post_delay_ms`, `key_hold_ms`, `key_gap_ms`, `type_delay_ms`, `max_chars`.

Include a `initial_prompt` in the whisper settings seeded with racing
vocabulary (corner names, series terms, radio phrases) — it measurably improves
accuracy on proper nouns.

Resolve the focused executable via `GetForegroundWindow` →
`GetWindowThreadProcessId` → `OpenProcess` → `QueryFullProcessImageNameW`.

## Logging

Log the focused executable name on every trigger — I need it to discover the
right profile keys. Log timestamps for each stage (pre-keys done, transcription
duration, sent) so I can diagnose "chat box wasn't open yet" from the log rather
than by feel.

## Deliverables

The app, a config file with starter profiles for Le Mans Ultimate, iRacing,
ACC, AMS2 and rFactor2, a requirements file, and a README covering setup, the
run-as-admin requirement, how to tune a new sim, and what to try if nothing
types into the game (borderless vs exclusive fullscreen; per-character scan
codes if a game ignores Unicode injection; the Interception driver as a last
resort if a game rejects `SendInput` outright).

Start by laying out the file structure and confirming the approach, then build
it.
