"""Light and dark, and the widgets that ttk styles cannot reach.

Built against a real Tk. The interesting failures here are not exceptions —
they are a window that opens with a black-on-black log pane, or a Listbox left
bright white in the middle of a dark window, because ttk styles apply to ttk
widgets only and plain tk widgets take their colours as constructor options.
Nothing catches that except asking the widget what colour it ended up.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from pitradio import config as config_mod
from pitradio import paths
from pitradio import state as state_mod
from pitradio.ui import theme


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:          # no display
        pytest.skip(f"no display: {exc}")
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def app(root, monkeypatch):
    """The real window, in a chosen theme."""
    from pitradio.ui import gui

    def build(mode="dark"):
        store = config_mod.ConfigStore(paths.config_path())
        store.load()
        store.config.gui.theme = mode
        return gui.App(root, store, state_mod.AppState(), "0.0.0-test",
                       use_tray=False)

    return build


# -- resolving a mode -----------------------------------------------------


def test_the_modes_resolve_to_their_palettes():
    assert theme.resolve(theme.LIGHT) is theme.LIGHT_PALETTE
    assert theme.resolve(theme.DARK) is theme.DARK_PALETTE


def test_system_resolves_to_one_of_them(monkeypatch):
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.resolve(theme.SYSTEM) is theme.DARK_PALETTE

    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve(theme.SYSTEM) is theme.LIGHT_PALETTE


def test_an_unknown_mode_falls_back_to_the_system(monkeypatch):
    """A hand-edited config must not leave the window unstyled."""
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.resolve("chartreuse") is theme.LIGHT_PALETTE


def test_detection_never_raises(monkeypatch):
    """This runs during startup; no colour scheme is worth failing to open over."""
    import subprocess

    def explode(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(subprocess, "run", explode)
    assert theme.system_prefers_dark() in (True, False)


# -- the palettes themselves ---------------------------------------------


@pytest.mark.parametrize("palette", [theme.LIGHT_PALETTE, theme.DARK_PALETTE])
def test_every_colour_is_a_hex_triplet(palette):
    from dataclasses import fields

    for field in fields(palette):
        value = getattr(palette, field.name)
        if field.name == "name":
            continue
        assert value.startswith("#") and len(value) == 7, f"{field.name}={value}"


@pytest.mark.parametrize("palette", [theme.LIGHT_PALETTE, theme.DARK_PALETTE])
def test_text_contrasts_with_its_background(palette):
    """A dark-on-dark hint is unreadable and nothing else would catch it."""

    def luminance(hex_colour):
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                    for c in (r, g, b)]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def ratio(a, b):
        first, second = sorted((luminance(a), luminance(b)), reverse=True)
        return (first + 0.05) / (second + 0.05)

    # 4.5:1 is the WCAG floor for body text; muted hints are still text.
    assert ratio(palette.text, palette.window) >= 4.5
    assert ratio(palette.text, palette.surface) >= 4.5
    assert ratio(palette.text_muted, palette.window) >= 4.5
    assert ratio(palette.accent_text, palette.accent) >= 3.0


def test_the_two_palettes_define_the_same_roles():
    from dataclasses import fields

    assert ({f.name for f in fields(theme.LIGHT_PALETTE)}
            == {f.name for f in fields(theme.DARK_PALETTE)})


# -- the window ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_window_takes_the_palette_background(app, mode):
    window = app(mode)
    palette = theme.PALETTES[mode]
    assert str(window.root.cget("background")) == palette.window


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_log_pane_is_not_left_white_on_a_dark_window(app, mode):
    """tk.Text ignores ttk styling entirely; this is the widget that shows it."""
    window = app(mode)
    palette = theme.PALETTES[mode]

    assert str(window.log_text.cget("background")) == palette.surface
    assert str(window.log_text.cget("foreground")) == palette.text


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_plain_tk_widget_is_themed(app, mode):
    """Anything ttk cannot style has to be coloured at its call site.

    Walks the real widget tree rather than listing them, so a Text or Listbox
    added later fails here instead of shipping bright white.
    """
    window = app(mode)
    palette = theme.PALETTES[mode]
    window.root.update_idletasks()

    unstyled = []

    def walk(widget):
        for child in widget.winfo_children():
            if child.winfo_class() in ("Text", "Listbox", "Canvas"):
                background = str(child.cget("background"))
                if background not in (palette.surface, palette.window,
                                      palette.surface_alt):
                    unstyled.append(f"{child.winfo_class()} {child} = {background}")
            walk(child)

    walk(window.root)
    assert not unstyled, "plain tk widgets left unthemed: " + ", ".join(unstyled)


def test_the_palette_is_reachable_from_any_widget(app):
    """Helpers are handed a container, not the App, and still need colours."""
    window = app("dark")
    assert getattr(window.root, "_pitradio_palette", None) is window.palette


# -- config ----------------------------------------------------------------


def test_the_theme_defaults_to_following_the_system():
    assert config_mod.Config().gui.theme == "system"


@pytest.mark.parametrize("mode", ["system", "light", "dark"])
def test_valid_themes_are_accepted(mode):
    cfg = config_mod.Config.from_dict({"gui": {"theme": mode}})
    assert [p for p in cfg.validate() if "theme" in p] == []


@pytest.mark.parametrize("mode", ["Dark", "solarized", "", 7])
def test_an_unknown_theme_is_reported(mode):
    cfg = config_mod.Config.from_dict({"gui": {"theme": mode}})
    assert any("gui.theme" in p for p in cfg.validate())
