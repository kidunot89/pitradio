# PitRadio

Push-to-talk voice dictation into sim racing chat boxes.

Hold a key, say what you want, let go. PitRadio opens the game's chat box,
transcribes what you said, types it, and sends it — without you taking a hand
off the wheel. Speech recognition runs locally on the CPU; nothing you say
leaves your machine.

Built for wheel-mounted buttons: bind a button to F13 with JoyToKey (or your
wheel's own software) and PitRadio sees it as an ordinary key.

> **Status:** early. Ships with a profile for Le Mans Ultimate. Other sims work
> by adding a profile, which takes about a minute — see
> [Adding your sim](#adding-your-sim).

---

## Install

Download the latest `pitradio-setup-*.exe` from
[Releases](https://github.com/kidunot89/pitradio/releases) and run it.

**Windows will warn you about it.** Two separate things cause this, and both
are expected:

- **SmartScreen: "Windows protected your PC".** These builds aren't
  code-signed, so Windows has no publisher to attribute them to. Choose **More
  info → Run anyway**.
- **Antivirus may flag or quarantine it.** PitRadio installs a global keyboard
  hook and synthesises keystrokes. That is a keylogger's behavioural signature.
  It is also precisely what push-to-talk dictation requires — there is no way to
  swallow your trigger key and type into a game without it.

If you'd rather not take that on trust: the source is here, the build is
reproducible from it, and [`SHA256SUMS`](https://github.com/kidunot89/pitradio/releases)
in each release lets you verify what you downloaded. You can also
[run from source](#running-from-source) and skip the binary entirely.

### It needs to run as administrator

The installed build requests this automatically. It matters because of a
Windows rule called UIPI: a normal-privilege process cannot send input to a
window owned by an elevated one. If your sim or its launcher runs elevated and
PitRadio doesn't, **every keystroke is silently discarded** — no error, no
exception, nothing typed. This is the single most common cause of "it does
nothing".

The Status tab warns you if the app isn't elevated.

---

## First run

1. Open PitRadio. On first launch it downloads the speech model (~250MB,
   once). The window shows the progress.
2. Go to **Audio**, pick your microphone, and press **Record 4s and
   transcribe**. Nothing is typed anywhere — this just proves the mic and the
   model work. If the level bar barely moves while you speak, raise
   **Microphone gain**; the bar shows the signal after gain, which is what
   Whisper actually receives.
3. Sort out a trigger key — see below.
4. Start your sim, hold the trigger, say something, release.

### About F13

The default trigger is **F13**, because no sim binds it, so holding it can never
also do something in the game. Almost no keyboard has an F13 key: the intended
route is to map a wheel button to F13 with **JoyToKey** (or your wheel's own
software), after which PitRadio sees it as an ordinary keypress.

You don't have to type key names. **Settings → Press a key…** binds whatever
you press next, including combinations like `Ctrl+F12`, and the press is
swallowed so binding Enter doesn't also do something behind the window.

**Settings → Press a button…** binds a wheel or gamepad button directly, which
removes the need for JoyToKey entirely. The key and the button work alongside
each other — either one triggers. Button detection uses Windows' built-in
joystick interface, which reports up to 32 buttons per device.

To try it at a desk with no wheel plugged in, `scrolllock` and `pause` are good
choices: present on most keyboards, rarely bound by sims.

Whatever you pick is **swallowed**: it never reaches the game, so don't use a
key the sim needs.

Closing the window minimises to the tray; the trigger key keeps working. Quit
from the tray menu to actually stop the app.

---

## Adding your sim

Profiles are keyed on the game's executable name, and PitRadio tells you what
that is:

1. With the sim focused, tap the trigger key once.
2. Alt-tab to PitRadio. The **Status** tab shows **Focused app** — that's the
   executable name.
3. Go to **Profiles → Add**, and it will offer that name.
4. Set the keys your sim uses for chat. For most sims that's Enter to open and
   Enter to send.

Then tune it. The setting that matters is **Delay after opening chat**
(`pre_delay_ms`): the chat box needs a few frames to open and take focus, and
if PitRadio starts typing too early the opening characters vanish. Start at
350ms; raise it if you lose the beginning of messages.

Config changes take effect on the next trigger — no restart. The file lives at
`%APPDATA%\pitradio\config.json` if you'd rather edit it directly; the GUI and
a text editor write the same file.

**Got a sim working?** A profile that works is genuinely useful to other people
— please open an issue with the executable name and the keys.

---

## Nothing is typed into the game

Work down this list; it's ordered by how often each one is the answer.

1. **Is PitRadio running as administrator?** See above. This is most of them.
2. **Is the game in borderless windowed mode?** Exclusive fullscreen swallows
   synthetic input in some titles. Borderless is worth trying before anything
   else here.
3. **Does the chat box open at all?** Check the log (Status → Open log folder).
   If you see `pre-keys sent` but no text appears, the keys are reaching the
   game and the problem is the typing. If the chat box never opens, the
   `pre_keys` are wrong for that sim.
4. **Are the first characters missing?** Raise `pre_delay_ms`.
5. **Does the chat box open but stay empty?** The game is ignoring Unicode
   input. Set that profile's **Text injection** to `scancode`, which types
   character by character using real key presses instead. Slower, and limited
   to what your keyboard layout can produce, but some games accept nothing else.
6. **Still nothing?** A few games read input below the level `SendInput` can
   reach — usually anti-cheat related. The
   [Interception driver](https://github.com/oblitum/Interception) is the only
   real workaround, and it's a kernel driver, so treat it as a last resort.
   PitRadio doesn't use it.

The log records the executable name and per-stage timings for every trigger —
when the chat box opened, how long transcription took, when the message was
sent. That turns "it felt wrong" into something you can actually read.

---

## Session plugins

A plugin reads live data from a sim. Today that means the driver list, which
PitRadio uses two ways: it feeds the names to Whisper so they're transcribed
correctly, and it can prefix them in the message — say "tell Tandy to box" and
send `tell @Tandy to box`.

**Le Mans Ultimate ships with one**, reading LMU's shared memory with no
game-side plugin required. Assign it in **Profiles → Session plugin**; the
bundled LMU profile already has it. The choice lives on the profile, so a plugin
that suits two games can be assigned to both.

Note on the `@`: it's plain text. Neither LMU nor rFactor 2 chat supports
markup, so there's no bold and the game attaches no meaning to it — it's a human
convention, like writing someone's name in caps.

The accuracy half is the more valuable one. Whisper mangles proper nouns it has
no reason to expect; telling it who's in the session beats any amount of
matching after the fact.

Plugins are compiled into the app — there's no way to add one after installing.
Adding a sim means a pull request, and it's two small steps: see
[plugins/README.md](plugins/README.md).

---

## Accuracy

The **Vocabulary** tab feeds Whisper a list of words to expect. It ships with
corner names, series terms and radio phrases, and it measurably improves proper
nouns. Add your regular team mates' names, your series' jargon, tracks you run
often.

Transcription runs on the **CPU, deliberately** — the GPU belongs to the sim. A
model grabbing VRAM mid-corner costs frames, and a few hundred milliseconds of
CPU transcription doesn't.

### Other languages

The **Language** tab configures which languages you want and how large a model
to use for each. Add a language, pick a size, press **Save and download**, and
the models are fetched into the cache.

Worth understanding, because it shapes the choices: **Whisper has no
per-language models.** There are English-only builds (`tiny.en` … `medium.en`)
and multilingual builds (`tiny` … `large-v3`), and every multilingual build
handles all the languages. Picking a size per language is still useful —
multilingual `small` is weaker than `small.en`, so a second language often wants
a bigger model than English does. "Medium Spanish, small English" means `medium`
when transcribing Spanish and `small.en` when transcribing English.

Only one language is active at a time. The others stay configured and
downloaded, so switching is instant.

Sizes trade accuracy against latency, and latency is what you feel mid-stint:

| Size | Download | Notes |
| --- | --- | --- |
| tiny | ~75 MB | fastest, least accurate |
| base | ~145 MB | fast |
| small | ~480 MB | the default; a good balance on CPU |
| medium | ~1.5 GB | noticeably slower on CPU |
| large | ~3 GB | often too slow to use between corners |

Also replace the **Vocabulary** text when you change language: it ships as
English racing terms, and a prompt in the wrong language works against you.

---

## Updates

PitRadio checks GitHub for new releases and can install them itself. Automatic
installs are **off by default**, and always deferred while a sim is in focus —
restarting the app mid-stint would be worse than updating a day later.

**What the verification does and doesn't prove.** Downloads are checked against
the `SHA256SUMS` published with the release before anything is run. That proves
the download arrived intact. It does not prove who produced it — the builds
aren't signed, so if the repository or a release were compromised, the updater
would install whatever was there, with administrator rights. That is why
auto-install is opt-in. Code signing would fix this properly and is the obvious
next step for the project.

Updating closes PitRadio, installs, and reopens it. Your config, logs and the
cached speech model live outside the install directory, so none of them are
touched — an update never re-downloads the model.

Turn the check off entirely with `--no-update-check`, or in
`config.json` under `updates`.

---

## Privacy

- Speech recognition runs entirely on your machine. Audio is never uploaded and
  never written to disk.
- The app makes exactly two kinds of network request: downloading the speech
  model on first run, and checking GitHub for updates.
- Transcription history is kept in memory only, and goes away when you quit.
- The keyboard hook only acts on the configured trigger key. Every other key is
  passed straight through untouched.

---

## Running from source

```bash
git clone https://github.com/kidunot89/pitradio.git
cd pitradio
pip install -r requirements.txt
python pitradio.py
```

Run your terminal as administrator, for the reason above.

Useful flags:

```bash
python pitradio.py --check-config    # validate config.json, resolve every key name
python pitradio.py --list-devices    # audio devices, for picking a mic
python pitradio.py --gui-only        # open the window with no hook, audio or model
python pitradio.py --self-test       # load every component; verifies a packaged build
python pitradio.py --no-update-check # never contact GitHub
```

`--check-config` and `--gui-only` also run on macOS and Linux — the config
layer and the GUI have no Windows dependencies, which is what makes the app
developable off Windows. Everything that actually types into a game does not.

### Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers config validation and hot-reload, key-name resolution, text
sanitising, path resolution, and the updater's verification logic. It runs on
any platform. The keyboard hook, key injection and the trigger cycle aren't
covered — they need Windows, and CI exercises them by building and running the
real binary on a Windows runner.

### Building the installer

```bash
pip install nuitka
python packaging/build.py
```

Then verify the result before trusting it:

```bash
.\build\pitradio.dist\pitradio.exe --self-test
```

That loads every component and opens a window, which is what catches a
dependency that failed to bundle. Then compile `packaging/pitradio.iss` with
Inno Setup. CI does both on every
tagged release, and builds on every push so that a broken native dependency
shows up there rather than on someone's rig.

---

## How it works

| Piece | What it does |
| --- | --- |
| `hook.py` | `WH_KEYBOARD_LL` hook on its own thread, with the message pump Windows requires. Swallows the trigger key so it never reaches the game. |
| `worker.py` | The trigger cycle: record → open chat → transcribe → type → send. All the slow work, off the hook's thread. |
| `inject.py` | `SendInput`. Scan codes for keys, UTF-16 for text — two different timing regimes, for good reasons documented in the file. |
| `speech.py` | Capture and faster-whisper. |
| `gui.py`, `gui_settings.py`, `tray.py` | The window and tray icon. |
| `updater.py` | Release checks and verified self-update. |
| `config.py`, `keys.py`, `paths.py` | Config, key names, and where things live. No Windows imports. |

---

## License

MIT. See [LICENSE](LICENSE).
