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
python -m pitradio                   # run the app (Windows, as administrator)
python -m pitradio --check-config    # validate config, resolve every key name
python -m pitradio --list-devices    # audio devices
python -m pitradio --gui-only        # window with no hook/audio/model
python -m pitradio --self-test       # import every component + open a Tk window
pytest -q                             # test suite (runs on any platform)
pytest tests/test_updater.py -q       # one file
pytest -q -k checksum                 # one case
ruff check .                          # lint (config in pyproject.toml)
python packaging/fetch_sdl3.py        # fetch the bundled SDL3.dll
python packaging/build.py             # Nuitka build
python packaging/build.py --version   # print __version__
python packaging/make_icon.py         # regenerate packaging/icon.ico
python packaging/checksums.py DIR     # write SHA256SUMS for release artifacts
```

`pytest` covers everything with no Windows or audio dependency: config
validation, merging and hot-reload; key-name resolution; text sanitising; path
resolution; and the updater's version comparison, host allowlist and checksum
verification. It runs on any platform — that it runs on Linux is itself the
check that nothing Windows-only has leaked into a portable module.

Not covered by pytest, and deliberately so: the hook, injection, the worker
cycle, and the GUI. Those need either Windows or a display. `--gui-only`
launches the real window against a stubbed backend for the GUI.

CI runs the *built binary's* `--check-config`, `--list-devices` and
`--self-test` on a Windows runner. **`--self-test` is the one that matters** —
the other two return before importing tkinter, the hook or the speech stack, so
they pass happily on a build that cannot transcribe a word. v0.1.0 shipped
exactly that way, missing `av.utils`. When you add a runtime dependency, add it
to the list in `cmd_self_test` or packaging will not notice when it goes
missing.

CI then **compiles the installer, silently installs it, runs the installed exe
from `C:\Program Files\PitRadio`, and uninstalls again**, asserting that the
uninstaller exists (the self-updater refuses to run without it) and that
uninstalling leaves `%APPDATA%\pitradio` intact. The release workflow does the
same and refuses to publish if any of it fails. Before v0.1.2 nothing had ever
executed the installer or the files it lays down — every check ran out of
`build\pitradio.dist`, which is not what users receive.

Note the limit: a `/VERYSILENT` install **skips `[Run]` entries** (they are
`skipifsilent`), so it cannot catch a broken post-install launch. That is what
[tests/test_installer.py](tests/test_installer.py) is for — it asserts the flags
statically instead.

When adding tests, keep them importable without `winapi` — a test that needs
Windows can't run in the place most of this gets developed.

## Layout

```
src/pitradio/            the app, as a package
  __init__.py            __version__, and nothing else that imports
  __main__.py            the CLI; `python -m pitradio`
  config paths state keys languages mentions gestures updater worker speech
  input/                 winapi hook inject joystick sdlinput sdl3input xinput
  ui/                    gui gui_settings gui_language tray
  plugins/               per-sim session data
vendor/                  third-party modules that are deliberately not deps
packaging/               build, installer, icon, checksums, SDL3 fetch
tests/
```

`__version__` lives in `src/pitradio/__init__.py`, not in `__main__.py`, so
packaging and the updater can read it without importing the CLI — which drags
in tkinter and the speech stack.

`paths.install_dir()` resolves to the **repository root** from source, three
levels up from `src/pitradio/paths.py`. Pointing it at the package directory
puts a source run's config and logs inside the source tree and hides
`config.default.json`, and nothing fails — it just reports "not found; using
built-in defaults" and carries on.

## Development happens off Windows

The Windows input path cannot be exercised on the development Mac. Two rules
keep as much as possible testable anyway, and both are load-bearing:

- **`config.py`, `keys.py`, `paths.py`, `state.py`, `updater.py` must not
  import `winapi`** (nor anything that does). This is what makes
  `--check-config` work on any platform.
- **`ui/` must not import `winapi` either.** That is
  what makes `--gui-only` able to launch the real window locally. They reach
  Windows-only functionality through the objects passed into `App` — `hook`,
  `joystick`, `recorder`, `transcriber`, `worker` — all of which may be `None`,
  and every use site must cope with that.

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
| joystick | polls wheel/gamepad buttons; feeds the same queue as the hook |

Joystick input has **four backends, combined rather than chosen between**
(`joystick._ensure_started`, in preference order):

| Backend | Sees | Notes |
| --- | --- | --- |
| SDL3 ([sdl3input.py](sdl3input.py)) | devices SDL2 never covered, read over HIDAPI | bundled `SDL3.dll` |
| SDL2 ([sdlinput.py](sdlinput.py)) | the widest range of wheels, pedals, button boxes | bundled `SDL2.dll` |
| XInput ([xinput.py](xinput.py)) | four slots of anything presenting as an Xbox pad | no library to bundle |
| legacy (`winapi` via `joystick.LegacyPads`) | whatever the multimedia API reports | the floor |

A rig routinely spans more than one — a wheel on SDL2 and a pad on XInput is
ordinary — so `devices()` merges every backend's list and deduplicates by
identity, earlier backends winning. Picking a single backend, which is what the
SDL2-only path did, silently drops half the hardware.

**Each `Device` carries the `api` that found it**, and `diagnose()` prints it
per device. Which backend saw something is the first useful question when it
does not work: a pad only XInput can see has no real identity, and a device
missing from every backend is a driver or Steam problem rather than anything
this app can fix.

A backend that fails to load, or throws while enumerating, must cost only
itself — never the others and never the app. `--self-test` reports each one and
fails if SDL2, or SDL3 in a frozen build, did not load, so a bundle that drops
a DLL is caught rather than silently losing devices.

**`SDL3.dll` is fetched, not committed** — [packaging/fetch_sdl3.py](packaging/fetch_sdl3.py)
downloads the official libsdl-org release and verifies a pinned SHA256. There is
no Python package that ships an SDL3 Windows binary (PySDL3 is a pure wrapper
with no library in it), and a 2.8MB binary in the repo is something nobody
reviews. CI fetches it before building.
`tests/test_build_flags.py` parses the DLL's PE export table and asserts every
symbol `sdl3input` declares actually exists — SDL3 renamed most of the SDL2
joystick calls (`SDL_JoystickUpdate` became `SDL_UpdateJoysticks`), and a
misspelling would fail inside `start()`, be caught, and drop silently to SDL2.

`SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS` is not optional: without it SDL drops
joystick input whenever PitRadio isn't focused, which is always, while racing.
SDL is also initialised without a video subsystem, so it has no window and no
focus to gate on, and state polling asks the driver directly rather than going
through an event queue.

**`SDL_JOYSTICK_THREAD` is not optional either, and is less obvious.** SDL only
re-scans for devices when its device-change window receives `WM_DEVICECHANGE`,
and with that hint off SDL creates the window on whichever thread called
`SDL_Init` — ours, which runs a polling loop and never pumps messages. The
result is that anything connected *after* startup is never noticed, for the
life of the process. See `WINDOWS_JoystickDetect`, which returns immediately
unless `s_bWindowsDeviceChanged` is set.

**Bindings are stored against the device's SDL GUID, not its index** — see
`JoystickConfig` and `JoystickWatcher._resolve_index`. Indices are positional:
plug in a headset that enumerates as a controller, or start Steam, and every
index after it shifts, silently rebinding the trigger to a different device.
`device` is kept only as the fallback for bindings written before identities
were recorded. POV hats are folded into the button mask above the physical
buttons, because a wheel rim's D-pad is a hat and a binding UI that cannot see
it looks broken to the person pressing it.

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
- **The self-updater exits before the installer runs.** Inno's
  `/CLOSEAPPLICATIONS` uses the Windows Restart Manager, which needs the target
  to register and answer shutdown requests; a tkinter app does neither, so Setup
  stalls on "Closing applications" with a dialog. v0.1.13 shipped that. The
  handoff is now a PowerShell shim that waits on our PID, installs silently, and
  relaunches — see `updater.shim_command`.
- **Frozen GUI builds may have `sys.stdout is None`.** `pitradio.out()` and the
  console log handler both guard for it.
- **A GUI build launched from a shortcut has invalid std handles.** Every
  `subprocess` call must redirect `stdin` as well as stdout/stderr, or Windows
  fails process creation with `[WinError 6]`. This killed every shortcut launch
  through 0.1.2; `tests/test_subprocess_safety.py` walks the AST to enforce it.
- **Config changes that arm hardware must be applied on save**, not left to the
  worker's next-trigger reload — changing the trigger key would otherwise need
  the *old* key pressed to take effect. See `App._apply_trigger_key`.
- **The trigger's modifiers are checked, not tracked.** A low-level hook reports
  one key at a time, so `ctrl+f12` works by asking `GetAsyncKeyState` about Ctrl
  when F12 arrives. Only the main key is swallowed; swallowing Ctrl would break
  it system-wide.
- **Button capture waits for a press, not a held button.** Wheel rims and button
  boxes are covered in toggle switches and rotary encoders that read as
  permanently down, so "the first button that reads as down" captures one nobody
  touched. `JoystickWatcher._poll_capture` snapshots what is already held and
  waits for a change.

## Reviewing before sending

`Profile.auto_send` off types the message into the chat box and leaves it. The
trigger then means something else, and [gestures.py](gestures.py) decides what:
tap to send, tap twice to clear, hold to clear and re-record.

Two consequences worth knowing before changing it:

- **A tap cannot be acted on immediately** — until the double-tap window closes
  it might be the first half of one. That delay is `review.double_tap_ms`, and
  it is why the worker's queue `get()` takes a timeout rather than blocking
  forever. Nothing else would wake the worker to notice the window had passed.
- **A press while a message is pending starts recording straight away**, before
  it is known to be a tap or a hold. Waiting to find out would swallow the first
  words of a re-record; the buffer is discarded if it turns out to be a tap.

The timing logic is pure and lives outside `worker.py` precisely so it can be
tested — every case in [tests/test_gestures.py](tests/test_gestures.py) is one
that would otherwise only be found mid-race.

**Send and clear can also be bound directly** — `review.send_key`,
`review.clear_key`, `send_joystick` and `clear_joystick`, all shown in
Settings → Trigger. These are *momentary*: the hook and the joystick watcher
post one event on the press edge and nothing on release, so the worker never
has to pair them up. They work alongside the gestures rather than replacing
them.

The event kind strings (`TRIGGER_DOWN`/`UP`/`SEND`/`CLEAR`) live in
[state.py](state.py) and are re-exported by `hook` and `joystick`. They cannot
live in `hook.py`: the GUI names them when arming a binding, and importing
`hook` from `gui.py` drags in `winapi` and breaks `--gui-only` everywhere but
Windows. `test_portable_modules_do_not_reach_winapi` walks the import graph to
enforce that, because nothing did before and the mistake is invisible — the
module still imports fine and every test passes.

## The editing tabs scroll

Tab content outgrew the window and tkinter silently clips the overflow, so Save
sat below the bottom edge with nothing to indicate it existed. `scrolling_tab`
and `scrolling_pane` in [gui_settings.py](gui_settings.py) put the fields on a
scrolling canvas and pin the button to a footer. Adding a field to one of these
tabs is otherwise enough to push Save off-screen again at small window sizes,
which is what `test_gui_contracts.py::test_a_tab_with_a_save_button_scrolls`
guards.

## Build locally before pushing

A Windows CI build is ~13 minutes warm and ~40 cold, so discovering a packaging
mistake there is expensive. Several have been found that way and every one was
reproducible in minutes on a Windows machine:

```bash
python packaging/build.py
.\build\pitradio.dist\pitradio.exe --self-test
```

`--self-test` is the check that matters: it loads every component, opens a real
window, and verifies SDL2 actually loads. It has caught a missing `av.utils`, a
missing `SDL2.dll` and a GUI that could not construct.

Also run it **with no console** — `Start-Process` without `-NoNewWindow` — which
is how a Start Menu shortcut launches it and is not the same code path.

What *is* checkable without building, and should be kept that way: every
`--include-package` name resolving, every `--include-data-files` source
existing, and the vendored modules being importable through the build's own
PYTHONPATH. See [tests/test_build_flags.py](tests/test_build_flags.py) — each of
those pins a mistake that previously cost a full build.

## Packaging

Nuitka `--standalone` (never `--onefile`: the extract-to-temp pattern is a large
part of why packed Python apps get quarantined) → Inno Setup installer. Builds
are **unsigned**, so SmartScreen warns on every release and some AV will flag
the binary — the README says so up front rather than letting users discover it.

The fragile part is native dependencies: `ctranslate2`, `onnxruntime`, PyAV and
PortAudio ship shared libraries that standalone mode doesn't always collect.
They're named explicitly in [packaging/build.py](packaging/build.py), and CI
runs the built exe specifically to catch it when that stops being enough.

Things established the hard way, so nobody has to rediscover them at ~30
minutes per attempt:

- **`sounddevice` is a module, not a package.** `--include-package` on it is a
  fatal Nuitka error. Its PortAudio DLL comes from `_sounddevice_data`.
- **`av` (PyAV, with FFmpeg) is imported eagerly by `faster_whisper`** and
  cannot be excluded, even though we hand Whisper a numpy array and never use
  its file-decoding path. It's most of the build time and the dist size.
- **`av` needs an explicit `--include-package`.** Each `av` submodule ships as a
  prebuilt extension with a `.py` typing stub beside it. Nuitka uses the
  extension, and imports made *from inside* one are invisible to static
  analysis — so following imports alone silently drops `av.utils`, and the app
  dies with `No module named 'av.utils'` the first time it loads Whisper. This
  shipped in v0.1.0. The `Nuitka-Inclusion: Should decide --prefer-source-code`
  lines in the build log are this mechanism announcing itself.
- **`onnxruntime` is imported lazily** by the VAD, which `vad_filter: true`
  enables by default. Nuitka can't discover it by following code, which is why
  the explicit `--include-package` is load-bearing.
- **`--windows-console-mode=attach`, not `disable`**, so `--check-config` still
  produces output from a terminal. The consequence is a GUI-subsystem binary:
  PowerShell neither waits for it nor sets `$LASTEXITCODE` from it, so CI must
  use `Start-Process -Wait -PassThru` or it tests a stale exit code.
- **Nuitka's cache is persisted by CI, via `cache/restore` + `cache/save`.** The
  combined `actions/cache` action only writes on job success, so every failed
  build was discarding its compilation work and leaving the next run cold — and
  a failed build has still compiled most of the dependency tree, which is
  exactly what is worth keeping.

  **The save condition is `success() || failure()`, deliberately not
  `always()`.** `always()` also fires on *cancellation*, and a run cancelled
  early has compiled almost nothing. Because the restore step falls back to the
  newest entry matching its prefix, that stunted cache then becomes what every
  later build inherits. The numbers are unambiguous: caches saved by successful
  runs were 80MB and the next build got **1272 clcache hits out of 1277**;
  caches saved by cancelled runs were 44–46MB and the next build got **0 hits
  out of 1278**, turning a ~13-minute build into ~57. Superseding a tag cancels
  its release run, which happens constantly here, so `always()` was poisoning
  the cache more often than not.

  When a build is inexplicably slow, the line to look at is
  `Nuitka-Scons: Compiled N C files using clcache with H cache hits` — the C
  compile and link is ~52 minutes of a 57-minute cold build and essentially
  free when warm. A low hit count means the restored cache was bad, not that
  caching is not working.

`packaging/checksums.py` writes `SHA256SUMS`, in Python rather than a shell
pipeline because its format must match `updater._expected_hash` — a seam that
otherwise only gets exercised during a real release. `tests/test_checksums.py`
pins the two together.

## Sim plugins

`plugins/` supplies per-sim session data; today just the driver list.
**Registration is static** (`BUILTIN` in `plugins/__init__.py`) because Nuitka
cannot follow a runtime-discovered import — a build that scanned a directory
would ship with no plugins and no error. Adding a sim is one module plus one
line; see [plugins/README.md](plugins/README.md).

Which plugin a game uses lives on the **profile**, not the plugin, so one plugin
can serve several games. `executables` on the plugin only pre-fills the picker.

Plugins expose options via `settings: tuple[PluginSetting, ...]`, rendered in
the profile editor when assigned. Values live on the **profile**
(`Profile.plugin_settings`), not the plugin, for the same reason the plugin
choice does. Defaults are read from the plugin at merge time, so adding a
setting never requires migrating configs.

`PluginRegistry.drivers_for` swallows every exception: it runs inside the
trigger cycle, and a plugin fault must cost session data, never the message.

`vendor/pylmusharedmemory` is TinyPedal's MIT-licensed LMU struct layout,
vendored rather than depended on. A wrong field offset produces plausible
garbage instead of an error, so the layout is worth taking from a maintained
source — and it is pure Python, so vendoring costs no bundling risk. Pinned at
commit `3968c15`.

## Languages and models

Whisper has no per-language models: English-only builds (`tiny.en` …
`medium.en`) and multilingual builds (`tiny` … `large-v3`). [languages.py](languages.py)
owns that mapping — `model_name(language, size)` picks the `.en` build for
English where one exists, because it beats multilingual at the same size.

The Language tab writes `whisper.languages` (code → size), `whisper.language`
(active) and derives `whisper.model` from the two. **The worker and transcriber
never see any of this** — they load one plain model name, which is what keeps
the runtime path unchanged.

`config.validate` rejects an `.en` model paired with a non-English language:
faster-whisper only warns and then transcribes English anyway, so without the
check the config would be silently wrong.

## Config

Ships with **one profile: Le Mans Ultimate**. Don't add speculative profiles for
sims you can't verify — the Status tab logs the real executable name, and that
is the intended way to add one. `profiles` keys are lowercase executable names;
each profile only overrides what it names, with the rest falling through to
`default_profile`.

`text_mode` (`unicode` | `scancode`) is an addition beyond the spec's field
list: it makes the "game ignores Unicode injection" fallback a config flip
rather than a code change.
