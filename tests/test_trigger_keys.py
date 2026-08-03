"""Trigger keys you can actually press.

F13 is the default because no sim binds it, but no ordinary keyboard has one
either — it is meant to be reached by mapping a wheel button to it externally.
Anyone testing at a desk has to change it first, which makes the change path
matter more than it looks.
"""

import pytest

from pitradio import config, keys

# Keys that exist on a normal keyboard and that sims rarely bind, so they work
# as a stand-in when there is no way to send F13.
DESK_FRIENDLY = ["scrolllock", "pause", "insert", "home", "end", "pagedown"]


@pytest.mark.parametrize("name", DESK_FRIENDLY)
def test_suggested_alternatives_resolve(name):
    assert keys.parse_key(name) > 0


@pytest.mark.parametrize("name", DESK_FRIENDLY)
def test_alternatives_are_valid_as_a_configured_trigger(name):
    cfg = config.Config()
    cfg.trigger_key = name
    assert [p for p in cfg.validate() if "trigger_key" in p] == []


def test_f13_through_f24_all_resolve():
    """The whole unbound range should be available, not just F13."""
    for n in range(13, 25):
        assert keys.parse_key(f"f{n}") == 0x6F + n


def test_changing_the_trigger_key_reaches_the_hook():
    """Saving a new trigger key must apply immediately, not on the next trigger.

    The worker re-reads config at the start of a trigger, so a new key would
    otherwise only take effect once the *old* key was pressed — impossible when
    you changed it precisely because you couldn't press the old one.
    """

    class FakeHook:
        def __init__(self):
            self.vk = keys.VK["f13"]

        def set_trigger(self, vk, modifiers=None):
            self.vk = vk
            self.modifiers = list(modifiers or [])

    class FakeStore:
        def __init__(self):
            self.config = config.Config()

    # Exercised without constructing a window: bind the real method to a stub.
    from pitradio.ui import gui

    app = object.__new__(gui.App)
    app.hook = FakeHook()
    app.store = FakeStore()
    app.store.config.trigger_key = "scrolllock"

    gui.App._apply_trigger_key(app)
    assert app.hook.vk == keys.VK["scrolllock"]
    assert app.hook.modifiers == []


def test_an_invalid_trigger_key_leaves_the_hook_alone():
    """A typo must not disarm the app."""

    class FakeHook:
        def __init__(self):
            self.vk = keys.VK["f13"]

        def set_trigger(self, vk, modifiers=None):
            self.vk = vk
            self.modifiers = list(modifiers or [])

    class FakeStore:
        def __init__(self):
            self.config = config.Config()

    from pitradio.ui import gui

    app = object.__new__(gui.App)
    app.hook = FakeHook()
    app.store = FakeStore()
    app.store.config.trigger_key = "not-a-key"

    gui.App._apply_trigger_key(app)
    assert app.hook.vk == keys.VK["f13"]


def test_no_hook_is_harmless():
    """--gui-only has no hook behind it."""
    from pitradio.ui import gui

    app = object.__new__(gui.App)
    app.hook = None
    gui.App._apply_trigger_key(app)


def test_a_combo_trigger_reaches_the_hook_with_its_modifiers():
    """ctrl+f12 must arrive as both the key to watch and the modifier to check."""

    class FakeHook:
        def __init__(self):
            self.vk = keys.VK["f13"]
            self.modifiers = []

        def set_trigger(self, vk, modifiers=None):
            self.vk = vk
            self.modifiers = list(modifiers or [])

    class FakeStore:
        def __init__(self):
            self.config = config.Config()

    from pitradio.ui import gui

    app = object.__new__(gui.App)
    app.hook = FakeHook()
    app.store = FakeStore()
    app.store.config.trigger_key = "ctrl+f12"

    gui.App._apply_trigger_key(app)
    assert app.hook.vk == keys.VK["f12"]
    assert app.hook.modifiers == [keys.VK["ctrl"]]


# -- capture into profile key lists --------------------------------------


class _FakeButton:
    def __init__(self):
        self.text = "Add key…"
        self.enabled = True

    def configure(self, **kwargs):
        self.text = kwargs.get("text", self.text)

    def state(self, flags):
        self.enabled = "!disabled" in flags


class _FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeRoot:
    def after(self, _ms, _fn=None):
        return "timer"

    def after_cancel(self, _timer):
        pass


class _FakeApp:
    def __init__(self):
        self.root = _FakeRoot()
        self.hook = None


def _capture(var, *, append):
    from pitradio.ui import gui_settings

    return gui_settings.KeyCapture(_FakeApp(), var, _FakeButton(), append=append)


def test_capture_appends_to_a_key_sequence():
    """Profile key fields are sequences; capturing should add, not replace."""
    var = _FakeVar("enter")
    _capture(var, append=True)._apply("ctrl+enter")
    assert var.get() == "enter, ctrl+enter"


def test_capture_appends_to_an_empty_field():
    var = _FakeVar("")
    _capture(var, append=True)._apply("escape")
    assert var.get() == "escape"


def test_capture_replaces_for_the_trigger_key():
    """Only one trigger key can be armed, so capture replaces there."""
    var = _FakeVar("f13")
    _capture(var, append=False)._apply("ctrl+f12")
    assert var.get() == "ctrl+f12"


def test_a_captured_combo_is_a_valid_profile_key():
    """What capture writes must survive config validation."""
    var = _FakeVar("")
    _capture(var, append=True)._apply("ctrl+shift+enter")

    cfg = config.Config()
    cfg.default_profile.pre_keys = [
        part.strip() for part in var.get().split(",") if part.strip()]
    assert cfg.validate() == []


def test_capture_restores_the_button_when_it_finishes():
    from pitradio.ui import gui_settings

    button = _FakeButton()
    capture = gui_settings.KeyCapture(
        _FakeApp(), _FakeVar(), button, append=True, label="Add key…")
    button.configure(text="Press a key… 3")
    capture.finish()
    assert button.text == "Add key…"
    assert button.enabled
