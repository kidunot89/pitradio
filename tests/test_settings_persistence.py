"""Settings edited in the window must survive a restart.

Driven against the real App and the real Settings tab, writing to a real file
and reading it back with a second ConfigStore — which is what "close and
reopen the app" actually is. Nothing here is stubbed except the Windows-only
collaborators the GUI is already built to work without.

This exists because a save that silently does nothing is invisible: there is
no exception, the window closes normally, and the setting is simply back to
what it was next time. Asserting that `_save_settings` was called proves
nothing; only re-reading the file does.
"""

from __future__ import annotations

import contextlib
import tkinter as tk

import pytest

from pitradio import config as config_mod
from pitradio import state as state_mod
from pitradio.ui import gui, gui_settings


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:          # no display
        pytest.skip(f"no display: {exc}")
    window.withdraw()
    yield window
    # A test that called App.quit() has already destroyed it.
    with contextlib.suppress(tk.TclError):
        window.destroy()


@pytest.fixture
def app(root, tmp_path):
    """The real window over a real config file."""
    path = tmp_path / "config.json"
    config_mod.save(path, config_mod.Config())

    store = config_mod.ConfigStore(path)
    store.load()
    instance = gui.App(root, store, state_mod.AppState(), "0.0.0", use_tray=False)
    root.update()
    return instance


def reopened(app) -> config_mod.Config:
    """What the next launch would load."""
    store = config_mod.ConfigStore(app.store.path)
    store.load()
    return store.config


def test_the_trigger_key_survives_a_restart(app):
    app.v_trigger.set("ctrl+f9")
    gui_settings._save_settings(app)
    assert reopened(app).trigger_key == "ctrl+f9"


def test_the_send_and_clear_keys_survive_a_restart(app):
    """Optional bindings, and the ones most likely to be typed and forgotten."""
    app.v_send_key.set("f10")
    app.v_clear_key.set("f11")
    gui_settings._save_settings(app)

    cfg = reopened(app)
    assert cfg.review.send_key == "f10"
    assert cfg.review.clear_key == "f11"


def test_clearing_a_binding_survives_a_restart(app):
    """Unbinding is a save like any other. An empty string that falls back to
    the previous value would make a binding impossible to remove."""
    app.v_send_key.set("f10")
    gui_settings._save_settings(app)
    app.v_send_key.set("")
    gui_settings._save_settings(app)

    assert reopened(app).review.send_key == ""


def test_a_blank_trigger_key_keeps_the_previous_one(app):
    """Unlike send/clear, the talk trigger is not optional: emptying the box
    must not leave the app with no way to record."""
    app.v_trigger.set("")
    gui_settings._save_settings(app)

    assert reopened(app).trigger_key == "f13"


def test_saving_twice_keeps_the_first_save(app):
    """The second save must build on the first, not on a stale object graph.

    save_config() used to call store.load(), which swapped in a fresh Config.
    Anything holding a sub-object of the old one went on mutating an orphan.
    """
    app.v_trigger.set("ctrl+f9")
    gui_settings._save_settings(app)
    app.v_send_key.set("f10")
    gui_settings._save_settings(app)

    cfg = reopened(app)
    assert cfg.trigger_key == "ctrl+f9"
    assert cfg.review.send_key == "f10"


def test_settings_survive_a_save_from_another_tab(app):
    """Each tab writes the whole file, so a later save from anywhere must
    carry earlier edits with it rather than reverting them."""
    app.v_trigger.set("ctrl+f9")
    gui_settings._save_settings(app)
    gui_settings._save_audio(app)

    assert reopened(app).trigger_key == "ctrl+f9"


def test_quitting_does_not_revert_an_earlier_save(app):
    """quit() rewrites the config to persist the window geometry. Doing that
    from a stale object would undo everything saved during the session."""
    app.v_trigger.set("ctrl+f9")
    gui_settings._save_settings(app)
    app.quit()

    assert reopened(app).trigger_key == "ctrl+f9"
