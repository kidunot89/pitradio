"""Translatable strings, and the catalogue contributors will edit.

The failure modes here belong to people who cannot test their own work: a
translator adds a language, and the only feedback they get is whether the
window looks right on their machine. So the mechanism has to be forgiving —
a missing key, a half-finished file, a broken placeholder — and the catalogue
has to be provably complete without running the GUI at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pitradio import i18n

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def english_again():
    """Leave the process in English; the catalogue is module state."""
    yield
    i18n.activate(i18n.SOURCE)


# -- falling back ---------------------------------------------------------


def test_an_untranslated_string_renders_as_itself():
    """The English source *is* the key, so a missing entry is not a blank."""
    i18n.activate(i18n.SOURCE)
    assert i18n.t("Trigger key") == "Trigger key"


def test_a_missing_catalogue_falls_back_rather_than_raising():
    assert i18n.activate("kl") == i18n.SOURCE
    assert i18n.t("Trigger key") == "Trigger key"


def test_an_untranslated_key_in_a_real_catalogue_falls_back(tmp_path, monkeypatch):
    """A part-finished translation stays usable instead of showing blanks."""
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    (tmp_path / "xx.json").write_text(
        json.dumps({"Trigger key": "Tecla", "Save": ""}), encoding="utf-8")

    assert i18n.activate("xx") == "xx"
    assert i18n.t("Trigger key") == "Tecla"
    assert i18n.t("Save") == "Save"            # empty means "not done yet"
    assert i18n.t("Never translated") == "Never translated"


def test_a_malformed_catalogue_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    (tmp_path / "xx.json").write_text("{not json", encoding="utf-8")

    assert i18n.activate("xx") == i18n.SOURCE
    assert i18n.t("Trigger key") == "Trigger key"


# -- placeholders ---------------------------------------------------------


def test_fields_are_substituted(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    (tmp_path / "xx.json").write_text(
        json.dumps({"{count} drivers": "{count} pilotos"}), encoding="utf-8")
    i18n.activate("xx")

    assert i18n.t("{count} drivers", count=8) == "8 pilotos"


def test_a_translation_with_the_wrong_placeholder_falls_back(tmp_path, monkeypatch):
    """A translator typos a field name; the window must still render.

    This is the one that would otherwise raise mid-draw, in a language the
    maintainer cannot read, on a machine they do not have.
    """
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    (tmp_path / "xx.json").write_text(
        json.dumps({"{count} drivers": "{cuenta} pilotos"}), encoding="utf-8")
    i18n.activate("xx")

    assert i18n.t("{count} drivers", count=8) == "8 drivers"


def test_placeholders_are_discoverable():
    assert i18n.placeholders("{count} of {total}") == {"count", "total"}
    assert i18n.placeholders("no fields here") == set()


# -- choosing a language --------------------------------------------------


def test_english_needs_no_catalogue():
    assert i18n.SOURCE in i18n.available()


def test_an_explicit_language_without_a_catalogue_uses_english():
    """Translations arrive one contributor at a time; most codes have none."""
    assert i18n.resolve("kl") == i18n.SOURCE


def test_system_follows_the_desktop_when_that_language_exists(monkeypatch):
    from pitradio import languages as languages_mod

    monkeypatch.setattr(i18n, "available", lambda: ["en", "es"])
    monkeypatch.setattr(languages_mod, "system_language", lambda default="en": "es")
    assert i18n.resolve(i18n.SYSTEM) == "es"


def test_system_falls_back_when_it_does_not(monkeypatch):
    from pitradio import languages as languages_mod

    monkeypatch.setattr(languages_mod, "system_language", lambda default="en": "kl")
    assert i18n.resolve(i18n.SYSTEM) == i18n.SOURCE


# -- the catalogue template ----------------------------------------------


def _template() -> dict:
    path = ROOT / "src" / "pitradio" / "locale" / "template.json"
    assert path.exists(), "run: python packaging/extract_strings.py"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_template_is_up_to_date():
    """Adding a string without regenerating ships a catalogue missing it.

    A translator would have no way to know: their file would simply lack the
    key and the window would show English in one place for no visible reason.
    """
    result = subprocess.run(
        [sys.executable, "packaging/extract_strings.py", "--check"],
        cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_template_has_no_translations_in_it():
    """It is the blank a contributor copies, not a language."""
    assert set(_template().values()) == {""}


def test_the_template_is_not_empty():
    assert len(_template()) > 50


@pytest.mark.parametrize("path", sorted(
    (ROOT / "src" / "pitradio" / "locale").glob("*.json")))
def test_every_catalogue_matches_the_template(path):
    """A shipped language must not carry keys the app never asks for.

    A stale key is a string that was reworded: the translation is dead and the
    window silently shows English. Catching it here is the only way anyone
    finds out.
    """
    if path.stem == "template":
        return

    template = _template()
    catalogue = json.loads(path.read_text(encoding="utf-8"))

    unknown = sorted(set(catalogue) - set(template))
    assert not unknown, (
        f"{path.name} has keys the app no longer uses: {unknown[:5]}"
    )


@pytest.mark.parametrize("path", sorted(
    (ROOT / "src" / "pitradio" / "locale").glob("*.json")))
def test_every_translation_keeps_its_placeholders(path):
    """`{count}` renamed in translation raises when the string renders."""
    if path.stem == "template":
        return

    catalogue = json.loads(path.read_text(encoding="utf-8"))
    for source, translated in catalogue.items():
        if not translated:
            continue
        assert i18n.placeholders(translated) == i18n.placeholders(source), (
            f"{path.name}: {source!r} -> {translated!r}"
        )
