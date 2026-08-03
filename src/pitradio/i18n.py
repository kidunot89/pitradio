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
    if code == SOURCE:
        _catalogue, _active = {}, SOURCE
        return SOURCE

    path = LOCALE_DIR / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Empty values mean "not translated yet", which is how a partly
        # finished catalogue stays usable instead of showing blanks.
        _catalogue = {k: v for k, v in raw.items()
                      if isinstance(v, str) and v.strip()}
        _active = code
        log.info("interface language: %s (%d of %d strings translated)",
                 code, len(_catalogue), len(raw))
    except Exception as exc:
        log.warning("could not load the %s catalogue (%s); using English", code, exc)
        _catalogue, _active = {}, SOURCE
    return _active


def t(text: str, **fields) -> str:
    """The translation of `text`, or `text` itself.

    `fields` are substituted with str.format. A translation whose placeholders
    do not match the source would raise mid-render, so that falls back to
    English rather than taking the window down — and says so once, because a
    catalogue error nobody sees never gets fixed.
    """
    translated = _catalogue.get(text) or text
    if not fields:
        return translated
    try:
        return translated.format(**fields)
    except (KeyError, IndexError, ValueError):
        log.warning("placeholder mismatch in the %s translation of %r", _active, text)
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text


def placeholders(text: str) -> set[str]:
    """The `{name}` fields in a string, for checking a translation against it."""
    import string

    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text)
            if name}
