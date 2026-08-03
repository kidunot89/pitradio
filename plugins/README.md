# Sim plugins

A plugin supplies session data for a sim — currently the list of drivers, which
PitRadio uses to transcribe names correctly and to turn them into mentions.

Plugins are **compiled into the build**. There is no way to add one after
installation, and that is deliberate: Nuitka cannot follow an import it never
sees, so a build that discovered plugins at runtime would ship with none and
give no indication why. Contributing a sim means a pull request.

## Adding one

Two steps.

**1. Write the module.** `plugins/yoursim.py`:

```python
from plugins.base import SessionPlugin


class YourSimPlugin(SessionPlugin):
    id = "yoursim"                       # stored in profiles; never rename it
    name = "Your Sim"                    # shown in the GUI
    executables = ("yoursim.exe",)       # only used to pre-fill a new profile
    description = "Reads the driver list from Your Sim."

    def drivers(self) -> list[str]:
        return [...]                     # empty list when unavailable

    def vocabulary(self) -> list[str]:
        return [...]                     # optional; defaults to drivers()

    def status(self) -> str:
        return "connected"               # one line for the plugin list
```

`vocabulary()` is what gets fed to Whisper so it expects those words. It
defaults to `drivers()` because that is the common case, but override it when
the useful terms are not people — car names, teams, tracks, commentators. The
Vocabulary tab shows what every plugin currently supplies, which is how someone
debugs a term that keeps coming out wrong.

**2. Register it.** Add the class to `BUILTIN` in `plugins/__init__.py`.

That's all. The profile editor picks it up automatically.

## Rules

**Never raise into the trigger cycle.** A plugin fault must cost session data,
never the message. The registry catches exceptions, but handle what you can
predict and return an empty list — a game update that moves a struct offset
should degrade to "no driver names", not break dictation mid-race.

**`id` is permanent.** Profiles store it. Renaming one orphans every config that
references it.

**`executables` is only a default.** Which plugin a game uses is stored on the
*profile*, so one plugin can serve several games — assign it to each. The
executable list just pre-fills the picker for a newly added profile.

**Keep it importable off Windows.** Tests run on Linux in CI and development
happens on macOS. Guard platform-specific calls and fail with a clear reason
rather than letting an exception escape — see how `lmu.py` handles named shared
memory being a Windows-only concept.

**Prefer vendoring struct definitions to deriving them.** A wrong field offset
yields plausible garbage rather than an error. `vendor/pylmusharedmemory` is
TinyPedal's MIT-licensed LMU layout, taken rather than hand-written for exactly
that reason.

## Testing

`tests/test_plugins.py` covers the registry contract, including that a
misbehaving plugin is contained. Add cases for your own read path — the LMU
tests populate a zeroed struct and read it back, which catches a wrong field
name that the plugin's own error handling would otherwise swallow.
