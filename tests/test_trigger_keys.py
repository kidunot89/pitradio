"""Trigger keys you can actually press.

F13 is the default because no sim binds it, but no ordinary keyboard has one
either — it is meant to be reached by mapping a wheel button to it externally.
Anyone testing at a desk has to change it first, which makes the change path
matter more than it looks.
"""

import pytest

import config
import keys

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

        def set_trigger(self, vk):
            self.vk = vk

    class FakeStore:
        def __init__(self):
            self.config = config.Config()

    # Exercised without constructing a window: bind the real method to a stub.
    import gui

    app = object.__new__(gui.App)
    app.hook = FakeHook()
    app.store = FakeStore()
    app.store.config.trigger_key = "scrolllock"

    gui.App._apply_trigger_key(app)
    assert app.hook.vk == keys.VK["scrolllock"]


def test_an_invalid_trigger_key_leaves_the_hook_alone():
    """A typo must not disarm the app."""

    class FakeHook:
        def __init__(self):
            self.vk = keys.VK["f13"]

        def set_trigger(self, vk):
            self.vk = vk

    class FakeStore:
        def __init__(self):
            self.config = config.Config()

    import gui

    app = object.__new__(gui.App)
    app.hook = FakeHook()
    app.store = FakeStore()
    app.store.config.trigger_key = "not-a-key"

    gui.App._apply_trigger_key(app)
    assert app.hook.vk == keys.VK["f13"]


def test_no_hook_is_harmless():
    """--gui-only has no hook behind it."""
    import gui

    app = object.__new__(gui.App)
    app.hook = None
    gui.App._apply_trigger_key(app)
