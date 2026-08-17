"""Translatable strings.

The **English text is the key**, as gettext does it. `t("Trigger key")` reads
as what it renders, a missing translation falls back to something correct
rather than to `settings.trigger.label`, and nobody has to invent key names.

Catalogues are plain JSON, one file per language, in `locale/`. Not gettext
`.po`/`.mo`, for two reasons: compiling `.mo` adds a build step whose output is
a binary this project would then have to get bundled correctly — a class of bug
it has already paid for four times — and a contributor adding a language should
need a text editor, not `msgfmt`.

Missing keys, missing files and broken placeholders all fall back to English.
A window in the wrong language is a nuisance; a window that raises is not a
window.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from pitradio import languages as languages_mod

log = logging.getLogger(__name__)

def _locale_dir() -> Path:
    """Where the catalogues are.

    Beside the module when running from source. A compiled build sets
    `__file__` to somewhere inside the dist, which usually works — but this
    project has shipped four separate releases where a bundled data file was
    not where the code looked, each time silently, so the executable's own
    directory is checked as well.
    """
    beside_module = Path(__file__).resolve().parent / "locale"
    if beside_module.is_dir():
        return beside_module
    return Path(sys.executable).resolve().parent / "pitradio" / "locale"


LOCALE_DIR = _locale_dir()

#: The language the source strings are written in, which needs no catalogue.
SOURCE = "en"

SYSTEM = "system"

_catalogue: dict[str, str] = {}
_active = SOURCE


def read(code: str) -> dict[str, str] | None:
    """One catalogue off disk. None means it could not be read.

    None rather than an empty dict, because the two are different and the
    difference decides which language is reported as active: a catalogue with
    nothing translated in it yet is still that language, and one that failed to
    parse is English.

    Split out from `activate` because the window is no longer the only thing
    that needs a language: the engineer speaks out loud, and what it says can
    reasonably be in a different language from the window it is configured in —
    a driver talking to the sim in Spanish wants a Spanish engineer whatever
    language the tabs are in. `Catalogue` below is that second consumer, and
    both go through this so there is still only one file format and one place
    that reads it.
    """
    if code == SOURCE:
        return {}
    path = LOCALE_DIR / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load the %s catalogue (%s); using English", code, exc)
        return None
    # Empty values mean "not translated yet", which is how a partly finished
    # catalogue stays usable instead of showing blanks.
    return {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}


class Catalogue:
    """A language, held rather than made global.

    The module-level `t()` renders the window and there is only ever one of
    those, so it stays a global. Anything that needs its own — today the
    engineer — holds one of these instead of fighting over the same one.
    """

    def __init__(self, code: str = SOURCE, entries: dict[str, str] | None = None):
        loaded = entries if entries is not None else read(code)
        # A catalogue that would not load is English, not a broken version of
        # the language it claimed to be — otherwise `english` reads False and
        # the engineer stops spelling numbers out for no visible reason.
        self.code = SOURCE if loaded is None else code
        self.entries = loaded or {}

    @classmethod
    def for_setting(cls, setting: str) -> Catalogue:
        code = resolve(setting)
        return cls(code)

    @property
    def english(self) -> bool:
        """Whether this is the language the source strings are written in.

        Asked by the engineer, which spells numbers out in English and reads
        them as digits everywhere else — see `engineer/lines.py`.
        """
        return self.code == SOURCE

    def t(self, text: str, **fields) -> str:
        return _render(self.entries, self.code, text, **fields)

    def translate(self, text: str, **fields) -> str:
        """`t`, for a string that is not a literal at the call site.

        A separate name rather than a habit, because `packaging/extract_strings`
        rejects `t(variable)` — and rightly: a source string it cannot see is
        one no translator is ever offered. These calls are the exception it is
        guarding against, where the literal lives somewhere the extractor has
        already found it (a routine's default phrases, the engineer's fixed
        lines) and this is only where it gets looked up.
        """
        return self.t(text, **fields)


def available() -> list[str]:
    """Language codes that have a catalogue, English first."""
    if not LOCALE_DIR.is_dir():
        return [SOURCE]
    found = sorted(
        path.stem for path in LOCALE_DIR.glob("*.json")
        if path.stem not in (SOURCE, "template")
    )
    return [SOURCE, *found]


def active() -> str:
    return _active


def resolve(setting: str) -> str:
    """The language to use for a configured value.

    `system` follows the desktop and falls back to English when that language
    has no catalogue — which is the normal case, since translations arrive one
    contributor at a time.
    """
    if setting and setting != SYSTEM:
        return setting if setting in available() else SOURCE

    detected = languages_mod.system_language(SOURCE)
    return detected if detected in available() else SOURCE


def activate(setting: str = SYSTEM) -> str:
    """Load a catalogue. Returns the language actually in use."""
    global _catalogue, _active

    code = resolve(setting)
    entries = read(code)
    _catalogue = entries or {}
    _active = SOURCE if entries is None or code == SOURCE else code
    if _active != SOURCE:
        log.info("interface language: %s (%d strings translated)",
                 _active, len(_catalogue))
    return _active


def _render(entries: dict[str, str], code: str, text: str, **fields) -> str:
    translated = entries.get(text) or text
    if not fields:
        return translated
    try:
        return translated.format(**fields)
    except (KeyError, IndexError, ValueError):
        log.warning("placeholder mismatch in the %s translation of %r", code, text)
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text


def t(text: str, **fields) -> str:
    """The translation of `text`, or `text` itself.

    `fields` are substituted with str.format. A translation whose placeholders
    do not match the source would raise mid-render, so that falls back to
    English rather than taking the window down — and says so once, because a
    catalogue error nobody sees never gets fixed.
    """
    return _render(_catalogue, _active, text, **fields)


def placeholders(text: str) -> set[str]:
    """The `{name}` fields in a string, for checking a translation against it."""
    import string

    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text)
            if name}
