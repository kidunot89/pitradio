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
python -m pitradio --telemetry       # what the sim is publishing, for 10s
python packaging/engineer_demo.py     # hear the engineer against a fake session
pytest -q                             # test suite (runs on any platform)
pytest tests/test_updater.py -q       # one file
pytest -q -k checksum                 # one case
ruff check .                          # lint (config in pyproject.toml)
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

Keep them independent of **what is running on the machine**, too. Two LMU tests
asserted "not connected" without forcing it: off Windows that is the only
possible outcome, so they looked fine, and on the one machine where the plugin
can be exercised for real they failed the moment the sim was open. `lmu_absent`
in [tests/test_plugins.py](tests/test_plugins.py) stubs the mapping instead. A
suite that only passes when the sim is closed is no use to whoever is racing.

## Layout

```
src/pitradio/            the app, as a package
  __init__.py            __version__, and nothing else that imports
  __main__.py            the CLI; `python -m pitradio`
  config paths state keys languages mentions gestures updater worker speech
  input/                 winapi hook inject
  ui/                    gui gui_settings gui_language tray
  plugins/               per-sim session data
  engineer/              the voice that talks back; see docs/engineer.md
vendor/                  third-party modules that are deliberately not deps
packaging/               build, installer, icon, checksums
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
  `recorder`, `transcriber`, `worker` — all of which may be `None`,
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

Voice and the engineer add two more each — a relay/poll thread and a playback
thread — for the same reason: the hook has a deadline, the worker is holding
somebody's trigger, and the GUI owns the widgets. None of them may block those.

**Controllers are not read at all.** PitRadio once had four joystick backends
— SDL3, SDL2, XInput and the legacy multimedia API — merged and deduplicated
by device identity. All of it was removed in v0.1.27.

It is worth knowing why, because "just add SDL back" is the obvious wrong
instinct:

- A Fanatec rim enumerated with **79 inputs** and never reported a press
  through any of the four.
- A Steam Controller was only visible at all with `SDL_JOYSTICK_HIDAPI_STEAM`,
  which *takes the device away from Steam* — breaking the owner's own desktop
  shortcuts and surfacing touchpad and grip sensors as buttons that chatter or
  sit permanently down. Turning it off, as the owner rightly insisted, made
  the controller invisible instead.
- Both devices are claimed by software that will not share them. Reading them
  anyway means seizing them, and a dictation app has no business doing that.

The supported route is **JoyToKey**: the user maps a wheel button to a keyboard
key, and the existing `WH_KEYBOARD_LL` hook sees it as an ordinary keypress.
That path has always worked, needs no bundled DLLs, and costs nothing to
maintain. The Settings → Trigger section says so in the window.

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
- **All sound leaves through one held-open stream.** [audio.py](src/pitradio/audio.py)
  owns it; nothing else calls `sd.play`. Three separate silences came from not
  doing this. WASAPI shared mode accepts *only* the endpoint's configured rate
  and refuses anything else outright, so the rate is read back off the opened
  stream rather than asked of `query_devices` — what that reports and what the
  endpoint accepts are not always the same number. Shared mode is requested
  explicitly (`WasapiSettings(exclusive=False)`), because a dictation app that
  seized an output device would silence the game it exists to talk over. And
  opening a WASAPI endpoint is a negotiation rather than a function call, so
  doing it per beep was rolling the dice per beep.
- **MME truncates every device name to 31 characters, silently.** A config
  holding one can only ever match MME again, and MME's writes succeed and
  produce no sound while another process holds the endpoint. `speech._matches`
  forgives the truncation so the host API preference gets to prefer WASAPI.
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
- **`ConfigStore.mark_saved`, not `store.load()`, after the GUI writes.**
  Reloading builds an equal-but-distinct object graph, so any tab still
  holding a sub-object of the old one — a `Profile`, a `CueConfig` — goes on
  mutating an orphan that no later save writes. The edit vanishes with no
  error, which is indistinguishable from "the setting doesn't persist".
- **The self-updater runs the installer *visibly*, via `os.startfile`.** Two
  separate mistakes, both of which shipped. `/SILENT /SUPPRESSMSGBOXES` means
  a failed install leaves no window, no dialog and no exit code anyone reads —
  the app closes, Setup does nothing, and the version never changes, which is
  precisely what v0.1.25 and v0.1.26 did. And `subprocess.Popen` is
  CreateProcess, which refuses to start a `requireAdministrator` binary from a
  non-elevated process (ERROR_ELEVATION_REQUIRED, 740); the installer is
  `PrivilegesRequired=admin`. This repo had already recorded the second lesson
  — it is why the installer's own `[Run]` entry carries `shellexec`.

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
`review.clear_key`, both shown in Settings → Trigger. These are *momentary*:
the hook posts one event on the press edge and nothing on release, so the
worker never has to pair them up. They work alongside the gestures rather than
replacing them.

The event kind strings (`TRIGGER_DOWN`/`UP`/`SEND`/`CLEAR`) live in
[state.py](state.py) and are re-exported by `hook`. They cannot
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
window. It has caught a missing `av.utils` and a GUI that could not
construct.

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

`plugins/` supplies per-sim session data: the driver list, the standings, and
everything the engineer reads.
**Registration is static** (`BUILTIN` in `plugins/__init__.py`) because Nuitka
cannot follow a runtime-discovered import — a build that scanned a directory
would ship with no plugins and no error. Adding a sim is one module plus one
line; see [plugins/README.md](plugins/README.md).

**Opening the block goes through `shared_memory.open_existing`, never
`mmap.mmap(tagname=…)`.** On Windows mmap *creates* the mapping when it is
absent, which fabricates a page-file block of zeros under the game's own name —
so the plugin reports itself connected to a session that does not exist. One
shared helper because the rule is silent when broken and two copies of it would
drift; `test_the_lmu_plugin_never_creates_the_mapping` walks every plugin's AST
for `tagname=`.

**Sims differ more than they look, and a plugin says so.** LMU publishes every
car's world position; **iRacing publishes none** — only `CarIdxLapDistPct`, how
far round the lap each car is — so a geometric spotter cannot be built for it
at all. It publishes `CarLeftRight` instead, its own call from the real car
bodies, which is a better answer than the geometry rather than a worse one.
That is why `SessionInfo.alongside` exists and why `SpotterNotification`
overrides `supported` to accept either route.

iRacing also publishes **no speed for any car but the player's**, so
`iracing.Speeds` derives it from lap distance over the session clock. The guard
there is a *speed* check, not a distance one: a car sent to the pits jumps
hundreds of metres between reads and comes out of the subtraction as a
well-formed enormous speed — a teleport from 4000m to 100m wrapped to 1100m and
read as 1100 m/s.

**iRacing sector times are not implemented**, and the plugin does not claim
`PROVIDES_SECTORS`. `SplitTimeInfo` gives the boundaries but iRacing publishes
no per-car splits, so they would have to be timed in the plugin. Until then the
sector behaviours are skipped with a line in the log, which is the whole point
of the capability gate.

The iRacing session string is YAML and is parsed by hand in `irsdk.py` rather
than with a dependency. It has one shape a flat stack-based parser gets wrong,
and it is the one the driver list is in: iRacing indents a list **level with
the key that introduced it**, so "less indented, close the block" would drop
every driver.

**Assetto Corsa is the most limited of the three, and the shape follows from
it.** `acpmf_static`/`graphics`/`physics` give every car's world position, and
lap data for the player *only* — no driver names for anybody else at all. So
the trainers work against your own best lap, which is what a practice session
is anyway, and standings and mentions cannot work. It does not claim
`PROVIDES_FIELD`, which is what stops "somebody has taken the fastest lap"
firing when you beat your own with a field of one.

Three things in `acpmf.py` are the traps: strings are **UTF-16** (`wchar_t`),
so decoding as UTF-8 yields a first letter and rubbish; "no time" is a
**sentinel of 99999999ms**, which left alone becomes an eleven-hour best lap
that the trainers then target; and the layout stops at the fields the three
games agree on, because past `sectorCount` sits a `bool` whose padding is a
compiler's business and one byte shifts everything after it. `plausible()`
refuses pages whose values are not Assetto Corsa-shaped, turning a layout
change into "not connected" rather than confident nonsense.

Track length there is **measured, not read**: distance covered over fraction of
a lap covered, which holds whether `distanceTraveled` counts the lap or the
session, and avoids an offset nobody can check. `elapsed` is the **current lap
time**, because these pages have no session clock — only the time *left*, which
counts down and is zero in a lap-limited race. A per-lap clock is not a
compromise: a trace is one lap and every question asked of it is a subtraction
between two points on the same one.

**The Project CARS block covers three games**, which is why `pcars2.py` reads
only the *head* of it: the participant array gives names, world positions, lap
distance in metres, place and lap counts, and everything past it — the timing
arrays — is where the layout is least certain from outside. `ParticipantInfo`
has a `bool` then a 64-byte name then a float array, so the name ends at 65 and
the position starts at **68**; the offsets are written out rather than summed
because three bytes of padding turns a grid into noise that still looks like
numbers.

Lap times there are a **stopwatch**, not a field: the clock when the lap
counter went up minus the clock when it last did. That clock is this machine's,
so a lap spanning a pause comes out long — which fails safe, since an inflated
lap never becomes a reference. Their sector enum could not be pinned down, so
`PROVIDES_SECTORS` is not claimed.

**Automobilista 2 is a subclass with its own id**, not a shared entry. A
profile picks a plugin by name and should be able to name the game being run;
plugin settings are stored against the id, so the two get separate spotter
geometry; and AMS2 has been diverging from the Project CARS API, so the
override has somewhere to live. It is listed **before** its parent in
`BUILTIN`, because `for_executable` returns the first match and the generic one
would otherwise claim AMS2's executables.

`derive.Speeds` is shared by iRacing and the Project CARS plugins, both of
which publish distance and no usable speed. The lap book records nothing from a
stationary car, so a speed left at zero means no trace samples at all and a
trainer that never sees a lap.

**Only LMU has been run against its game.** Every other reader is tested
against a block built by hand, which catches a wrong width, a bad sentinel,
a mis-decoded string or a padding mistake, and cannot catch a wrong assumption
about what the sim puts in a field. `--telemetry` is how that gets settled.

Which plugin a game uses lives on the **profile**, not the plugin, so one plugin
can serve several games. `executables` on the plugin only pre-fills the picker.

Plugins expose options via `settings: tuple[PluginSetting, ...]`, rendered in
the profile editor when assigned. Values live on the **profile**
(`Profile.plugin_settings`), not the plugin, for the same reason the plugin
choice does. Defaults are read from the plugin at merge time, so adding a
setting never requires migrating configs.

`PluginRegistry.drivers_for` swallows every exception: it runs inside the
trigger cycle, and a plugin fault must cost session data, never the message.

**Standings are multi-class.** Endurance grids run several classes at once, so
"P3" has three answers and only one of them is the overall order. Plugins return
a `Standings` — `overall` plus `by_class` — from a *single* read, because two
calls would be two snapshots of a block that updates many times a second. LMU's
block has no in-class field at all: class order is derived by sorting a class's
members on their overall `mPlace`.

The spoken class name is matched by `mentions.class_aliases`, which also strips
a manufacturer prefix so "LMGT3" answers to "GT3" — but only when four or more
characters remain, or "LMP2" would answer to "P2" and "LMP2 P4" would resolve
the wrong driver entirely. An alias two classes share is dropped rather than
guessed at. The class group in `_POSITION_RE` is **lazy**: greedy, "P1 and P2"
matches once with the class group swallowing "P1 and", and the leader never
resolves.

`vendor/pylmusharedmemory` is TinyPedal's MIT-licensed LMU struct layout,
vendored rather than depended on. A wrong field offset produces plausible
garbage instead of an error, so the layout is worth taking from a maintained
source — and it is pure Python, so vendoring costs no bundling risk. Pinned at
commit `3968c15`.

## The engineer

`engineer/` is a named voice that watches the sim and talks back — lap times, a
spotter, and routines started by saying a phrase.
[docs/engineer.md](docs/engineer.md) is the guide; this is what would otherwise
be rediscovered.

**A command must never be invented.** The trigger key is the one that sends
messages to the whole session, so `EngineerService.handle` returning True throws
somebody's words away. Two narrow paths in: addressed by name, or the whole
sentence is a phrase. **A phrase taking a `{driver}` argument is only ever
matched on the addressed path** — its argument has no end, so unaddressed
"target time is a twenty three" was swallowed whole and never reached the chat
box. `worker._for_engineer` swallows every exception and returns False for the
same reason: a fault in an optional feature must not cost a message.

**Corners are found in the data, not looked up.** A track map would need a file
per circuit, would go stale on layout changes, and would leave the feature
working on four tracks. A corner is where the reference lap slowed and sped up
again, which holds everywhere; the cost is that they are numbered, not named.

**Time is read off the trace, never integrated.** Each sample carries the sim's
clock as well as the distance, so a segment time is one subtraction between
interpolated points. Integrating ds/v would accumulate every sample's error, and
at the rate the scoring block publishes that error is larger than the
differences being reported. `time_between` returns None across a gap rather than
interpolating over it — a plausible number here is indistinguishable from a real
one and would be acted on.

**Silence is the default.** Below `coach_threshold` there is no call. An
engineer that speaks at every corner is one nobody listens to.

**TTS is a PowerShell host, not a binding.** `System.Speech` is on every Windows
10/11 machine; `pywin32` and `comtypes` are two more native dependencies in a
build that has already shipped four releases broken by one. It is passed as
`-EncodedCommand` so execution policy cannot block it, base64 in both directions
so an accented driver name and a `C:\Users\José` temp path both survive the
console code page, and it **synthesises to a WAV** rather than speaking — that
is what gives the engineer the same output-device setting as voice chat instead
of landing on whatever Windows considers default mid-race.

**Voice packs use Crew Chief's folder layout**, so a `crew-chief-autovoicepack`
output drops straight in. Phrase ids are *derived* from the words
(`slug("two tenths")` → `two_tenths`), so nothing maintains a mapping and a pack
built for an older version keeps working. `speaking.read_wav` is hand-rolled
because `wave` raises on IEEE float, which is exactly what that generator emits.

**Four personas, not four recordings.** A generated pack is 1-2GB; four would be
an 8GB download to replace what Windows already has. A persona is a name, a
preferred voice, a pace and a verbosity, resolved against installed voices.

**The engineer's language follows `whisper.language`, not `gui.language`.** The
commands arrive through Whisper, so an engineer listening for English phrases
while Whisper produces Spanish would never hear one — and nothing about that
failure points at a language setting. `i18n.Catalogue` exists for this: a *held*
language, separate from the global one the window uses.

**Numbers are words in English and digits elsewhere.** Number grammar is
per-language and doing it half-well produces confident nonsense in somebody's
own language; digits hand it to the speech voice for that language, which is
correct. Consequence: a non-English pack cannot cover numbers.

**The spotter's left/right could not be verified off a track.** The geometry is
sound but the sign depends on the sim's handedness, so it is
`spotter_swap_sides` on the plugin rather than a guess in the code. Heading
comes from two consecutive positions, not `mOri`, precisely because the
orientation matrix's convention is the thing that could not be checked.

Session data grew rather than gaining a parallel type: `Car` carries lap
distance, speed, laps and lap times, and `SessionInfo` carries `track_length`
and `elapsed`, all from the same single read. **`SessionInfo.has_data` is not
`__bool__`** — `__bool__` asks "is there a room to be in", which needs a game
server, and offline practice is exactly where a coaching routine is most wanted.
`PluginRegistry.any_telemetry` is the engineer's entry point for that reason.

Routines are registered statically in `routines.BUILTIN` for the same reason
plugins are. Their trigger phrases live on the **config**, not the routine —
what a routine is called is not the routine.

**A plugin declares what it `provides`, and a behaviour needing something
absent is skipped and says so once.** Sims differ more than they look: LMU
hands over every car's world position, iRacing hands over lap-distance
percentage and has its own left/right field instead, so a spotter can be built
from one and not the other. A behaviour left switched on and permanently silent
is indistinguishable from a bug — `Runner.run` logs which capability is missing
rather than letting that happen. `provides=None` means "nobody said" and is
read as "everything", so a plugin written before this existed keeps working.

**The spotter's geometry is per-sim**, on the plugin's settings:
`spotter_swap_sides`, `spotter_metres`, `spotter_width_metres`. Car lengths and
axis conventions differ per game, so a number that suits one is wrong in the
next. `service._context_for` is the single place a Context is built — there
were two, and they had already drifted, so the same car counted as alongside or
not depending on which path built the tick.

**`--telemetry` is how a sim gets verified.** The failure it exists for is not
a plugin reading nothing, which is obvious; it is a block that is *published
but frozen* — the sim connected, every field plausible, and none of it ever
moving. It compares consecutive reads including the sim's own clock and says
so. Found on the first real run: five identical snapshots two seconds apart
with a car sitting at 77 m/s, because LMU was paused.

## Voice, and what is not in this repository

Voice chat sends the push-to-talk clip to the other PitRadio users in your
session. [docs/voice.md](docs/voice.md) is the design and the authority on why.

Three things about the split, because getting them wrong leaks something:

- **The relay server, its Terraform and its Ansible are in a private
  repository.** This one is public. The Terraform/Ansible there provision a
  *racer-provided* host, not the base one.
- **The base relay address is written in at build time**, into
  [endpoints.py](src/pitradio/endpoints.py), whose committed value is empty. A
  source checkout therefore has no relay and voice is unavailable — a working
  state, and far better than every fork aiming a microphone at a server nobody
  volunteered. Nothing else may hardcode an address.
- **`config.validate` treats an empty relay as a problem only when voice is
  enabled.** Otherwise `--check-config` fails for everyone working from source.
  A *malformed* relay is always reported, including plaintext `ws://` to
  anything but localhost — the payload is a recording of somebody's voice
  crossing a stranger's machine, and plaintext would keep working while doing
  it in the clear.

The relay never parses a clip: it checks the size, checks the room, and forwards
opaque bytes. So the wire format (`encode_clip`/`decode_clip` in
[voice.py](src/pitradio/voice.py)) is agreed between clients alone and changing
it never needs the server redeployed. `decode_clip` returns `None` for
everything malformed and raises for nothing — it parses bytes from a stranger's
machine on the audio path.

Proximity is **in-game distance**, decided on the listener's machine from the
sim's own data, and nothing positional is ever published. It is per-sim, so it
lives on the plugin (`proximity_only`), not in `VoiceConfig`.

It is measured from **the car on screen**, which while spectating is not your
own. LMU's shared memory does not say which that is — `playerVehicleIdx` stays
on the player's parked car, `mOptionsLocation` reads 0, and
`$rFactor2SMMP_Graphics$` is published but never populated (all zeros bar its
version counter; LMU does not call the callback that fills it). All three were
checked against a live spectated session. What does say is LMU's own HTTP API at
`127.0.0.1:6397/rest/watch/standings`, whose `hasFocus` marks the watched car
and whose `slotID` equals shared memory's `mID`. It is part of the game, not a
plugin, so it needs nothing installed.

That call is on the trigger cycle, so it is short-timeout and cached — including
its **failures**, or a closed game costs a timeout on every press. Tests must
stub `lmu._fetch_standings`; left alone it is answered for real on any machine
with LMU running, and every focus assertion silently depends on what the person
at that desk is watching.

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
