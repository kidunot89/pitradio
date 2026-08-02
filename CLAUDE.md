# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows push-to-talk dictation app for sim racing, in Python. Hold a key →
swallow it, record, fire the profile's keys to open the game's chat box → on
release, transcribe with faster-whisper on CPU, type the text, fire the send
keys. Per-sim behaviour is a JSON config keyed on the focused executable name.

Published publicly under MIT at `github.com/kidunot89/pitradio`, distributed as
an Inno Setup installer built by CI, with a self-updater.

[PROMPT.md](PROMPT.md) is the original specification and still the authority on
*why* the design is what it is.

## Commands

```bash
python pitradio.py                   # run the app (Windows, as administrator)
python pitradio.py --check-config    # validate config, resolve every key name
python pitradio.py --list-devices    # audio devices
python pitradio.py --gui-only        # window with no hook/audio/model
pytest -q                             # test suite (runs on any platform)
pytest tests/test_updater.py -q       # one file
pytest -q -k checksum                 # one case
ruff check .                          # lint (config in pyproject.toml)
python packaging/build.py             # Nuitka build
python packaging/build.py --version   # print __version__
python packaging/make_icon.py         # regenerate packaging/icon.ico
```

`pytest` covers everything with no Windows or audio dependency: config
validation, merging and hot-reload; key-name resolution; text sanitising; path
resolution; and the updater's version comparison, host allowlist and checksum
verification. It runs on any platform — that it runs on Linux is itself the
check that nothing Windows-only has leaked into a portable module.

Not covered by pytest, and deliberately so: the hook, injection, the worker
cycle, and the GUI. Those need either Windows or a display. `--gui-only`
launches the real window against a stubbed backend for the GUI, and CI runs the
*built binary's* `--check-config` and `--list-devices` on a Windows runner,
which is the only way the native-dependency bundling gets verified at all.

When adding tests, keep them importable without `winapi` — a test that needs
Windows can't run in the place most of this gets developed.

## Development happens off Windows

The Windows input path cannot be exercised on the development Mac. Two rules
keep as much as possible testable anyway, and both are load-bearing:

- **`config.py`, `keys.py`, `paths.py`, `state.py`, `updater.py` must not
  import `winapi`** (nor anything that does). This is what makes
  `--check-config` work on any platform.
- **`gui.py` and `gui_settings.py` must not import `winapi` either.** That is
  what makes `--gui-only` able to launch the real window locally. They reach
  Windows-only functionality through the objects passed into `App`, all of
  which may be `None`.

`pitradio.py` imports `winapi`, `hook`, `worker` and `inject` *inside*
functions, never at module scope, for the same reason.

## Architecture

Four threads, and mixing them up is how this breaks:

| Thread | Owns |
| --- | --- |
| main | tkinter `mainloop()`. **The only thread that may touch a widget.** |
| hook | `WH_KEYBOARD_LL` + its `GetMessageW` pump |
| worker | audio, transcription, injection — everything slow |
| tray | pystray's blocking `run()` |

Worker and hook never call into Tk. They publish onto `AppState.events`, which
the GUI drains on a `root.after(100, …)` tick. Log lines reach the GUI through
the same queue via `state.QueueLogHandler` — one mechanism, not two. pystray
callbacks arrive on the tray thread and marshal back with `root.after(0, …)`.

Config hot-reload is mtime-based, checked by the worker at the start of each
trigger. There is no watcher thread. The GUI's editor saves by writing
`config.json`, so in-GUI edits and hand edits take the identical path back in.
`config.save` writes through a temp file and `os.replace` so a half-written file
can never be picked up.

## Things that fail silently if you get them wrong

These are the reasons the code looks the way it does. Changing any of them
produces a bug with no error message.

- **UIPI.** `SendInput` into a higher-integrity process is discarded. The
  installed build self-elevates via manifest; `inject._send` checks the return
  value and logs `ERROR_ACCESS_DENIED` once, which is the only signal there is.
- **The hook needs its own message pump.** Without `GetMessageW`, Windows
  unregisters the hook after `LowLevelHooksTimeout` and the app keeps running
  while doing nothing.
- **The `HOOKPROC` object must be kept alive** (`KeyboardHook._proc`). If it's
  collected, the next keypress crashes the process.
- **Two injection timing regimes.** Scan-code keys are held `key_hold_ms`
  (~40ms) because games poll input once per frame. Unicode text characters are
  *not* — they go through the message queue, and 40ms/char would make a
  200-character message take 8 seconds. See the module docstring in
  [inject.py](inject.py).
- **Extended keys** (arrows, Insert/Delete/Home/End/PgUp/PgDn, right Ctrl/Alt)
  need `KEYEVENTF_EXTENDEDKEY` or they become their numpad twins.
- **`INPUT`'s union must declare `MOUSEINPUT`** even though only keyboard
  events are sent, or the struct is the wrong size and every call fails.
- **Recording starts before `pre_keys`**, or speech during `pre_delay_ms` is
  lost.
- **Injected input is tagged** with `winapi.INJECT_TAG` in `dwExtraInfo`, and
  the hook passes tagged events through. Otherwise the Enter we send to open
  chat re-triggers the app.
- **Installed builds cannot write next to the exe.** Config goes to `%APPDATA%`,
  logs and the model cache to `%LOCALAPPDATA%` — see [paths.py](paths.py). Under
  Program Files, writes land in UAC's VirtualStore and hot-reload silently stops
  working.
- **Frozen GUI builds may have `sys.stdout is None`.** `pitradio.out()` and the
  console log handler both guard for it.

## Packaging

Nuitka `--standalone` (never `--onefile`: the extract-to-temp pattern is a large
part of why packed Python apps get quarantined) → Inno Setup installer. Builds
are **unsigned**, so SmartScreen warns on every release and some AV will flag
the binary — the README says so up front rather than letting users discover it.

The fragile part is native dependencies: `ctranslate2`, `onnxruntime` and
PortAudio ship shared libraries that standalone mode doesn't always collect.
They're named explicitly in [packaging/build.py](packaging/build.py), and CI
runs the built exe specifically to catch it when that stops being enough.

`--windows-console-mode=attach`, not `disable`, so `--check-config` still
produces output when run from a terminal.

## Config

Ships with **one profile: Le Mans Ultimate**. Don't add speculative profiles for
sims you can't verify — the Status tab logs the real executable name, and that
is the intended way to add one. `profiles` keys are lowercase executable names;
each profile only overrides what it names, with the rest falling through to
`default_profile`.

`text_mode` (`unicode` | `scancode`) is an addition beyond the spec's field
list: it makes the "game ignores Unicode injection" fallback a config flip
rather than a code change.
