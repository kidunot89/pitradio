# Contributing to PitRadio

Thanks for looking. Three kinds of contribution are worth more than anything
else here, and none of them needs you to understand the Win32 plumbing:

| | |
| --- | --- |
| [A profile for a sim](#the-quickest-useful-contribution-a-profile) | no code — an issue with an executable name and two keys |
| [A translation](#adding-a-language) | one JSON file, no tooling; partial is fine |
| [A session plugin](#adding-a-sim-plugin) | one module and one line, ~30 lines of code |

The interface ships in English only. It is fully extracted and ready to
translate, so a language really is a file — see
[Adding a language](#adding-a-language).

## Contents

- [The quickest useful contribution: a profile](#the-quickest-useful-contribution-a-profile)
- [Getting set up](#getting-set-up)
- [Before you open a pull request](#before-you-open-a-pull-request)
- [What gets asked in review](#what-gets-asked-in-review)
- [Adding a language](#adding-a-language)
- [Adding a sim plugin](#adding-a-sim-plugin)
- [Commit messages](#commit-messages)
- [Reporting a bug](#reporting-a-bug)
- [Licence](#licence)

## The quickest useful contribution: a profile

If you got PitRadio working with a sim that isn't listed, that's worth sharing
and takes a minute:

1. Status tab → **Focused app** shows the executable name while the sim is
   focused.
2. Open an issue with that name and the keys your sim uses to open and send
   chat.

That's it. You don't need to open a pull request — the executable name and the
keys are the whole contribution.

## Getting set up

```bash
git clone https://github.com/kidunot89/pitradio
cd pitradio
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The suite runs on **macOS, Linux and Windows** and needs no sound card, no
controller and no speech model. That is deliberate and worth preserving — see
[DEVELOPING.md](DEVELOPING.md) for how, and why it keeps costing less than it
saves.

There is nothing else to install: the suite runs on any platform, with no
Windows and no audio hardware.

## Before you open a pull request

```bash
ruff check .
pytest -q
```

CI runs both on Linux and Windows, then builds the Windows binary, installs it,
runs the installed exe and uninstalls it. A red build is not a formality: most
of what it catches has been a real packaging bug that only appears in a
compiled, installed app.

**If you changed anything under `src/pitradio/input/`, `packaging/`, or the
Nuitka flags, build it locally first.** A CI build is ~20 minutes warm and ~50
cold, and almost every packaging mistake reproduces in minutes on a Windows
machine:

```bash
python packaging/build.py
.\build\pitradio.dist\pitradio.exe --self-test
```

## What gets asked in review

- **Does it fail loudly?** Nearly every bug this project has shipped was
  silent: a cache that returned no hits, a hook that stopped receiving events,
  a plugin that resolved to nothing. If your change can fail, make it say so.
- **Is there a test that would have caught it?** Not coverage for its own sake
  — a test for the specific way the thing goes wrong. If it can only be tested
  on Windows with hardware, say so in the PR and explain what you did check.
- **Do the comments explain *why*?** The code says what. Comments here are for
  the constraint that made it look like that, because the next person will
  otherwise "simplify" it back into the bug.

## Adding a language

The interface is English, and the machinery for translating it is already
there — a language is a JSON file, no tooling required.

1. Copy the template:

   ```bash
   cp src/pitradio/locale/template.json src/pitradio/locale/es.json
   ```

   Use the ISO 639-1 code (`es`, `de`, `pt`, `fr`, …). `template.json` is
   generated; don't translate it in place.

2. Fill in the values. The **key is the English text**, so it reads as what it
   renders:

   ```json
   {
     "Trigger key": "Tecla de activación",
     "Save": "Guardar",
     "Rescan controllers": ""
   }
   ```

3. Leave anything you're unsure of as `""`. An empty value means "not
   translated yet" and falls back to English — a half-finished catalogue is
   useful and ships fine. There is no need to do all of it.

4. Check it:

   ```bash
   pytest -q tests/test_i18n.py
   ```

   That verifies your file has no keys the app stopped using, and that any
   `{placeholder}` you kept still matches the English. A renamed placeholder
   would raise while the window drew — in a language the maintainer cannot
   read, on a machine they do not have — so it is checked rather than trusted.

Then select it in **Settings → Appearance → Interface language**. `Match the
system` picks it automatically for anyone whose desktop is set to that
language.

Two things worth knowing:

- The **interface** language is separate from the **transcription** language.
  A Spanish window with English chat is an ordinary thing to want.
- If you add a *string* to the app rather than a translation, regenerate the
  template or CI will fail:

  ```bash
  python packaging/extract_strings.py
  ```

## Adding a sim plugin

A plugin supplies session data — the driver list, so PitRadio can transcribe
names correctly and turn them into `@G.Taylor` mentions. See
[src/pitradio/plugins/README.md](src/pitradio/plugins/README.md); it is two
steps and about thirty lines.

Plugins are **compiled into the build**. There is no runtime loading, because
Nuitka cannot follow an import it never sees and a build that scanned a
directory would ship with no plugins and no error.

## Commit messages

Explain the reasoning, not the diff. The diff is already in the commit. What
is not recoverable later is why the obvious approach didn't work — and in this
codebase that is usually the interesting part.

## Reporting a bug

Include the log: **Status → Open log folder**, then
`%LOCALAPPDATA%\pitradio\logs\pitradio.log`. It records the trigger, the
matched profile, each stage's timing and the backend in use, which answers most
questions immediately.

For a controller that isn't detected, **Settings → Rescan controllers** lists
every backend and what each one found. Paste that.

## Licence

MIT. By contributing you agree your work ships under it.
