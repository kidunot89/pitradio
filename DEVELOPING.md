# Development guide

This is a **task guide**, not an architecture tour. Find the thing you want to
change, follow the recipe, ignore the rest of the app. You do not need to
understand the keyboard hook to add a sim, and you should not have to.

Every recipe is written test-first, because in this codebase that is not a
style preference. Almost everything here fails **silently** — a plugin that
resolves to nothing, a cache that returns no hits, a config field that never
loads. If you write the code first you will see the app "working" and have no
way to tell that it isn't. Writing the failing test first is how you find out
your test was testing nothing.

## First: can you run it?

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
pytest -q          # ~580 tests, a couple of seconds, no Windows needed
```

The suite runs on macOS, Linux and Windows with no sound card, no controller
and no speech model. If it doesn't, that's a bug — say so.

```bash
python -m pitradio --check-config   # validate config, resolve every key name
python -m pitradio --gui-only       # the real window, no hook, no audio, no model
```

`--gui-only` works on any platform. Use it constantly; it is much faster than
building.

## Where things live

You probably need one of these and not the others.

| I want to change… | Look at |
| --- | --- |
| what happens between press and release | `src/pitradio/worker.py` |
| a config field | `src/pitradio/config.py` |
| what a window looks like | `src/pitradio/ui/gui_settings.py` |
| colours, light/dark | `src/pitradio/ui/theme.py` |
| the icon | `src/pitradio/ui/logo.py` |
| driver names from a sim | `src/pitradio/plugins/` |
| turning a name into `@G.Taylor` | `src/pitradio/mentions.py` |
| tap / double-tap / hold | `src/pitradio/gestures.py` |
| which controllers are seen | `src/pitradio/input/joystick.py` |
| key names → scan codes | `src/pitradio/keys.py` |
| the build or the installer | `packaging/` |

Whole-system reasoning — why the hook needs its own message pump, why injection
has two timing regimes, why the cache is keyed the way it is — lives in
[CLAUDE.md](CLAUDE.md). Read it when something surprises you, not before.

---

## Recipe: support a new sim

**No code.** Profiles are config.

1. `python -m pitradio` with the sim running, tap the trigger, then look at
   Status → **Focused app**. That string is the profile key.
2. Profiles → **Add**, set the keys the sim uses to open and send chat.
3. If the first characters go missing, raise **Chat open delay**.

Then open an issue with the executable name and the keys so it can ship for
everyone. That is a genuinely useful contribution and costs you nothing.

---

## Recipe: add a session plugin

A plugin tells PitRadio who is in the session, so names transcribe correctly
and become mentions.

### 1. Write the test first

`tests/test_plugins.py`:

```python
def test_yoursim_reports_the_drivers_it_reads():
    plugin = yoursim.YourSimPlugin()
    plugin._snapshot = {"drivers": ["Geoff Taylor", "Nyck de Vries"]}

    assert plugin.drivers() == ["Geoff Taylor", "Nyck de Vries"]


def test_yoursim_survives_the_game_not_running():
    """Runs inside the trigger cycle: a fault must cost session data, not the message."""
    assert yoursim.YourSimPlugin().drivers() == []
```

```bash
pytest -q -k yoursim      # red: no module yet
```

### 2. Write the module

`src/pitradio/plugins/yoursim.py` — see
[plugins/README.md](src/pitradio/plugins/README.md) for the full interface.
Return `[]` from `drivers()` whenever the game isn't running.

### 3. Register it

Add the class to `BUILTIN` in `src/pitradio/plugins/__init__.py`. Registration
is static on purpose: Nuitka cannot follow a runtime-discovered import, and a
build that scanned a directory would ship with no plugins and no error.

```bash
pytest -q -k "yoursim or plugins"     # green
```

### The mistake to avoid

Don't raise. `PluginRegistry.drivers_for` swallows exceptions because it runs
inside the trigger cycle — but that means a bug in your plugin shows up as "no
driver names", not as a traceback. Handle what you can predict and log it.

---

## Recipe: add a config option

Say you want `whisper.beam_size` configurable per profile.

### 1. Test the shape first

`tests/test_config.py`:

```python
def test_beam_size_defaults_to_five():
    assert config.Profile().beam_size == 5


def test_beam_size_is_rejected_when_absurd():
    cfg = config.Config.from_dict({"profiles": {"x.exe": {"beam_size": 0}}})
    assert any("beam_size" in p for p in cfg.validate())


def test_a_profile_written_before_beam_size_still_loads():
    """Every config in the wild predates your field."""
    cfg = config.Config.from_dict({"profiles": {"x.exe": {"pre_keys": ["enter"]}}})
    assert cfg.profiles["x.exe"].beam_size == 5
```

That third test is the one that matters and the one people forget. Config files
are never rewritten on upgrade.

```bash
pytest -q -k beam_size    # red
```

### 2. Add the field

One line on the dataclass in `config.py`, with a default. `from_dict` ignores
unknown keys and fills missing ones, so old configs keep working for free —
that test proves it rather than assuming it.

### 3. Validate it

Add a check in `Config.validate`. It returns a list of problems rather than
raising, so the GUI can show all of them at once and one bad field doesn't stop
the app starting.

### 4. Use it, and put it in the GUI

`_profile_vars` in `gui_settings.py` builds the editor; `_read_profile_vars`
reads it back. Add to both — a field added to one and not the other silently
discards what the user typed.

---

## Recipe: add a field to the Settings window

The trap: **three places**, and missing one loses data with no error.

```python
# tests/test_theme.py or a new tests/test_gui_*.py
def test_the_new_field_round_trips(app):
    window = app()
    window.v_my_field.set("42")
    gui_settings._save_settings(window)

    assert window.store.load().my_field == "42"
```

Build the real window and save through it. There is no mocking here worth
doing — the failure mode is a variable that isn't wired, and only the real save
path shows that.

1. Create the `tk.StringVar` where the tab is built.
2. Place it with `_row(...)`, or `_field_grid(...)` if it's short — a
   millisecond value does not need the full window width.
3. Read it back in `_save_settings`.

If it must take effect **immediately** rather than on the worker's next
trigger, add it to `App.save_config`. Anything that arms hardware belongs
there: changing the trigger key would otherwise need the *old* key pressed to
take effect, which is a trap when the reason you changed it is that you can't
press it.

---

## Recipe: add a user-facing string

Wrap it in `t()` and regenerate the template. That is the whole recipe.

```python
from pitradio.i18n import t

ttk.Label(parent, text=t("Rescan controllers"))
```

```bash
python packaging/extract_strings.py    # adds it to locale/template.json
pytest -q tests/test_i18n.py           # green
```

The English text is the key, so an untranslated string renders as itself and a
language that lacks it is not broken by it.

`t()` needs a **literal** — the extractor reads the source, so a computed
string can never be translated. It refuses rather than skipping quietly. For
values, use fields:

```python
t("{count} drivers in session", count=len(drivers))
```

Forgetting to regenerate is caught by `test_the_template_is_up_to_date`,
because a translator's only clue would otherwise be one label mysteriously in
English.

---

## Recipe: change what happens on a trigger

`worker.py` is testable end to end, and the tests need no Windows.
`tests/test_worker.py` shows the pattern:

```python
def test_it_does_the_new_thing(worker_setup):
    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)

    assert sent == [("keys", ("enter",)), ("text", "box this lap"), ("keys", ("enter",))]
```

`sent` records what `inject` was *asked* to send, so you assert on the decision
rather than on Win32. Real in those tests: the config store and its file, the
profile lookup, sanitising, mentions, gesture timing. Standing in: the two
`inject` functions, `foreground_exe`, and the recorder and transcriber — which
arrive through the constructor, the seam the Worker was designed around.

Write the assertion for the sequence you want, watch it fail on the current
sequence, then change the worker. The diff in that failure message is the
specification.

---

## Recipe: add a controller backend

`joystick.py` combines backends rather than choosing one. A backend is a class
with `start / stop / list_devices / guid / button_mask / label / name` — see
`xinput.py`, which is the smallest.

Test it against a stand-in first (`StubBackend` in
`tests/test_joystick_binding.py`), then add it to `_ensure_started`. If it can
be driven for real — SDL3 can, through its virtual joystick API — do that
instead; `tests/test_sdl3.py` shows how, and it caught things a mock could not.

---

## The invariants that will bite you

Four rules. Each has a test that fails if you break it, so you'll find out —
but knowing why saves the confusion.

**`src/pitradio/ui/` and the config layer must not import `winapi`.** That is
what keeps `--gui-only` and `--check-config` working off Windows. `winapi`
itself now imports anywhere — the DLL handles are stand-ins that raise when
called — so this is an architecture rule, not an import-mechanics one.
→ `test_portable_modules_do_not_reach_winapi`

**Only the main thread may touch a widget.** The worker and hook publish onto
`AppState.events`; the GUI drains it on a timer. Tray callbacks marshal back
with `root.after(0, …)`.

**Every `subprocess` call must redirect `stdin`.** A GUI build launched from a
shortcut has invalid standard handles and process creation fails with
`[WinError 6]`. This broke every shortcut launch through v0.1.2.
→ `test_subprocess_safety.py`

**`self.foo()` must exist.** Worker and hook methods run on their own threads
and several are called from `except` blocks, so a typo is a thread that dies in
silence. v0.1.22 shipped exactly that.
→ `test_every_self_call_resolves`

---

## When you have to build

Only for packaging changes. `--gui-only` and `pytest` cover everything else.

```bash
python packaging/fetch_sdl3.py
python packaging/build.py
.\build\pitradio.dist\pitradio.exe --self-test
```

`--self-test` is the one that matters: it loads every component, opens a real
window, and checks SDL2 and SDL3 actually load. It has caught a missing
`av.utils`, a missing `SDL2.dll` and a GUI that could not construct — none of
which any other check noticed.

Run it **without a console** too (`Start-Process` with no `-NoNewWindow`).
That is how a Start Menu shortcut launches it, and it is not the same code
path.
