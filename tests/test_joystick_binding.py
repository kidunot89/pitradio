"""Backend aggregation, binding resolution and button capture.

Driven by a **real SDL3** with virtual devices, not by fakes. `joystick.py`'s
own logic — merging backends, resolving a binding to a device, edge-detecting a
capture, polling the trigger — runs against real enumeration and real button
state, so these cover the seam between the app and the library as well as the
app itself.

Nothing is stubbed. `winapi` imports off Windows and raises only if something
calls into Win32, which none of this does. Stand-in backends appear in exactly
three tests, for the cases a single real backend cannot produce: two backends
seeing the same device, two seeing different ones, and one that throws.

Every failure here is silent in production. A binding that resolves to the
wrong device, a capture that grabs a latched switch, and a trigger that never
fires all look identical from the driver's seat: nothing happens.
"""

from __future__ import annotations

import queue

import pytest


@pytest.fixture
def joystick(monkeypatch, sdl):
    """`joystick` with the real SDL3 backend as its only backend."""
    from pitradio.input import joystick as module

    # Bypass _ensure_started so the developer's own controllers, and SDL2,
    # stay out of it. The backend itself is real.
    monkeypatch.setattr(module, "_STARTED", True)
    monkeypatch.setattr(module, "_BACKENDS", [sdl])
    return module


def guid_of(joystick, name):
    for device in joystick.devices():
        if device.name == name:
            return device.guid
    raise AssertionError(f"no device named {name!r}")


def watcher(module, **kwargs):
    return module.JoystickWatcher(queue.Queue(), lambda: True, **kwargs)


def settle(module, w):
    """Poll out the capture settling window.

    Capture ignores everything active during it, which is what stops a Steam
    Controller's touchpad and grip sensors — permanently active buttons 20 and
    22 on a real one — from taking the binding before a key is pressed.
    """
    for _ in range(module.CAPTURE_SETTLE_POLLS):
        w._poll_capture()


def confirm(module, w):
    """Poll long enough for a held press to be confirmed."""
    for _ in range(module.CAPTURE_CONFIRM_POLLS):
        w._poll_capture()


def drain(events):
    return [events.get_nowait()[0] for _ in range(events.qsize())]


# -- enumeration through the aggregate ------------------------------------


def test_a_device_is_reported_with_the_api_that_found_it(joystick, pad):
    pad(name="Fanatec CSL Elite", buttons=20)

    devices = joystick.devices()
    assert len(devices) == 1
    assert devices[0].name == "Fanatec CSL Elite"
    assert devices[0].api == "SDL3"
    assert devices[0].guid


def test_the_detector_names_the_api_per_device(joystick, pad):
    pad(name="Fanatec CSL Elite", buttons=20)

    lines = joystick.diagnose()
    assert any("[SDL3]" in line and "Fanatec CSL Elite" in line for line in lines)


def test_hats_are_offered_as_bindable_inputs(joystick, pad):
    pad(name="Rim", buttons=10, hats=1)
    assert joystick.devices()[0].buttons == 14


def test_nothing_attached_says_so(joystick):
    assert joystick.devices() == []
    assert any("no controllers detected" in line for line in joystick.diagnose())


def test_a_binding_is_described_with_its_api(joystick, pad):
    pad(name="Rim", buttons=10, hats=1)
    index = joystick.devices()[0].index

    assert joystick.describe(index, 3) == "Rim - button 3 (SDL3)"
    assert joystick.describe(index, 11) == "Rim - POV up (SDL3)"


# -- resolving a binding to a device --------------------------------------


def test_a_binding_follows_its_device_when_the_order_changes(joystick, pad):
    """The whole reason identities are stored.

    Plugging in anything that enumerates first shifts every index after it, so
    resolving by position would bind the trigger to the new device.
    """
    pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    w = watcher(joystick, device=0, button=13, guid=wheel)
    assert w._resolve_device().name == "Wheel"

    # A second device appears; the binding must still find the wheel.
    pad(name="Headset", buttons=2)
    resolved = w._resolve_device()
    assert resolved is not None and resolved.name == "Wheel"


def test_a_missing_device_resolves_to_nothing(joystick, pad):
    """Better inactive than bound to whichever device took its place."""
    pad(name="Pedals", buttons=4)

    w = watcher(joystick, device=0, button=13, guid="0" * 32)
    assert w._resolve_device() is None


def test_a_binding_with_no_identity_falls_back_to_its_index(joystick, pad):
    """Configs written before identities were recorded must keep working."""
    pad(name="Wheel", buttons=20)

    w = watcher(joystick, device=0, button=4, guid=None)
    assert w._resolve_device().name == "Wheel"


# -- capture ---------------------------------------------------------------


def test_capture_ignores_a_switch_that_is_already_held(joystick, pad):
    """Rims and button boxes carry latched switches and rotary encoders.

    Taking the first button that reads as down captures one nobody touched,
    which is indistinguishable from capture being broken.
    """
    device = pad(name="Wheel", buttons=20)
    device.press(0)                        # a latched switch, held throughout

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    settle(joystick, w)                    # fold the latched switch into noise
    assert captured == []

    device.press(4)                        # a real press
    confirm(joystick, w)
    assert captured == [5]


def test_capture_takes_a_press_on_any_device(joystick, pad):
    """A wheel base, its rim and the pedals enumerate separately."""
    pad(name="Wheel", buttons=20)
    box = pad(name="Button Box", buttons=32)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.name, button)))

    settle(joystick, w)
    box.press(12)
    confirm(joystick, w)

    assert captured == [("Button Box", 13)]


def test_a_hat_can_be_captured(joystick, pad):
    """A rim's D-pad is a hat, and was invisible to capture before."""
    from pitradio.input import sdl3input

    device = pad(name="Rim", buttons=10, hats=1)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    settle(joystick, w)
    device.hat(0, sdl3input.SDL_HAT_UP)
    confirm(joystick, w)

    assert captured == [11]                # first input after the 10 buttons


def test_a_released_switch_can_be_bound_by_flicking_it(joystick, pad):
    """Otherwise a toggle switch could never be bound at all."""
    device = pad(name="Wheel", buttons=20)
    device.press(0)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    settle(joystick, w)                    # held
    device.press(0, down=False)
    w._poll_capture()                      # released — stops counting as held
    device.press(0)
    confirm(joystick, w)                   # pressed again

    assert captured == [1]


def test_capture_fires_once(joystick, pad):
    device = pad(name="Wheel", buttons=20)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    settle(joystick, w)
    device.press(0)
    for _ in range(10):
        w._poll_capture()

    assert captured == [1]


# -- the trigger itself ----------------------------------------------------


def test_pressing_and_releasing_posts_one_cycle(joystick, pad):
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid=wheel)

    w._poll_binding()
    device.press(12)
    w._poll_binding()
    w._poll_binding()                      # still held — no repeat
    device.press(12, down=False)
    w._poll_binding()

    assert drain(events) == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


def test_a_hat_direction_works_as_a_trigger(joystick, pad):
    from pitradio.input import sdl3input

    device = pad(name="Rim", buttons=10, hats=1)
    rim = guid_of(joystick, "Rim")

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=11, guid=rim)

    w._poll_binding()
    device.hat(0, sdl3input.SDL_HAT_UP)
    w._poll_binding()
    device.hat(0, 0)
    w._poll_binding()

    assert drain(events) == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


def test_a_controller_unplugged_mid_press_closes_the_cycle(joystick, pad, sdl):
    """A DOWN with no UP leaves the worker recording until something else fires."""
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid=wheel)

    device.press(12)
    w._poll_binding()
    sdl._lib.SDL_DetachVirtualJoystick(device.instance_id)
    w._poll_binding()

    assert drain(events) == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


def test_a_disabled_app_posts_nothing(joystick, pad):
    """The enable toggle has to reach the wheel, not only the keyboard."""
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: False, device=0, button=13, guid=wheel)

    w._poll_binding()
    device.press(12)
    w._poll_binding()

    assert events.empty()


# -- send / clear buttons --------------------------------------------------


def test_an_action_button_fires_once_per_press(joystick, pad):
    """Momentary, not held: the worker never has to pair press with release."""
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: (wheel, 0, 5)})

    w._poll_actions()
    device.press(4)
    w._poll_actions()
    w._poll_actions()                      # still held — no second event
    device.press(4, down=False)
    w._poll_actions()
    device.press(4)
    w._poll_actions()

    assert drain(events) == [joystick.TRIGGER_SEND, joystick.TRIGGER_SEND]


def test_send_and_clear_are_independent(joystick, pad):
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({
        joystick.TRIGGER_SEND: (wheel, 0, 1),
        joystick.TRIGGER_CLEAR: (wheel, 0, 2),
    })

    w._poll_actions()
    device.press(1)                        # button 2 only
    w._poll_actions()

    assert drain(events) == [joystick.TRIGGER_CLEAR]


def test_an_action_on_a_missing_device_is_silent(joystick, pad):
    """Not an error: the wheel may simply be unplugged."""
    pad(name="Wheel", buttons=20)

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: ("0" * 32, 0, 3)})
    w._poll_actions()

    assert events.empty()


def test_actions_and_the_talk_trigger_share_one_poll(joystick, pad):
    """Both must be serviced each tick, or binding one would disable the other."""
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=1, guid=wheel)
    w.set_actions({joystick.TRIGGER_SEND: (wheel, 0, 5)})

    w._poll_binding()
    w._poll_actions()
    device.press(0)
    device.press(4)
    w._poll_binding()
    w._poll_actions()

    assert sorted(drain(events)) == sorted(
        [joystick.TRIGGER_DOWN, joystick.TRIGGER_SEND])


# -- combining backends ---------------------------------------------------
#
# The only tests here that use a stand-in backend, because one real SDL3
# cannot produce two backends or a broken one.


class StubBackend:
    """A second backend. Implements the same interface SDL3 does."""

    def __init__(self, version, devices, explode=False):
        self.version = version
        self._devices = dict(devices)      # native -> (name, buttons, guid)
        self.failure = None
        self._explode = explode

    def start(self):
        return True

    def stop(self):
        pass

    def list_devices(self):
        if self._explode:
            raise RuntimeError("boom")
        return [(n, name, buttons) for n, (name, buttons, _g) in self._devices.items()]

    def guid(self, native):
        return self._devices[native][2]

    def button_mask(self, native):
        return 0

    def label(self, native, button):
        return f"button {button}"

    def name(self, native):
        return self._devices[native][0]


def test_devices_from_every_backend_are_combined(joystick, pad):
    """A wheel on one backend and a pad on another is an ordinary rig."""
    pad(name="Fanatec CSL Elite", buttons=20)
    joystick._BACKENDS.append(
        StubBackend("XInput", {0: ("XInput controller 1", 14, "xinput:0")}))

    found = {d.name: d.api for d in joystick.devices()}
    assert found == {"Fanatec CSL Elite": "SDL3", "XInput controller 1": "XInput"}


def test_the_same_device_seen_twice_appears_once(joystick, pad):
    """A pad both SDL3 and XInput can see must not be offered twice.

    The earlier backend wins: it is the one that knows the real name and
    button count.
    """
    pad(name="Xbox Wireless Controller", buttons=18)
    shared = guid_of(joystick, "Xbox Wireless Controller")
    joystick._BACKENDS.append(
        StubBackend("XInput", {0: ("XInput controller 1", 14, shared)}))

    found = joystick.devices()
    assert len(found) == 1
    assert found[0].api == "SDL3"
    assert found[0].name == "Xbox Wireless Controller"


def test_a_backend_that_throws_costs_only_itself(joystick, pad):
    """One broken backend must not hide what the others can see."""
    pad(name="Wheel", buttons=20)
    joystick._BACKENDS.insert(0, StubBackend("Broken", {}, explode=True))

    assert [d.name for d in joystick.devices()] == ["Wheel"]


# -- the backend interface -------------------------------------------------


BACKEND_CLASSES = [
    ("pitradio.input.sdl3input", "Sdl3Joysticks"),
    ("pitradio.input.sdlinput", "SdlJoysticks"),
    ("pitradio.input.xinput", "XInputPads"),
    ("pitradio.input.joystick", "LegacyPads"),
]

# What joystick.py calls on whatever it is handed.
REQUIRED = ("version", "start", "stop", "failure",
            "list_devices", "guid", "button_mask", "label", "name")


@pytest.mark.parametrize("module_name,class_name", BACKEND_CLASSES)
def test_every_backend_implements_the_whole_interface(module_name, class_name):
    """`SdlJoysticks` shipped without `version` and nothing noticed.

    The aggregator reads `backend.version` when it enumerates, logs and
    diagnoses, so the SDL2 path raised AttributeError the moment it was used.
    Every test either substituted a stand-in or drove SDL3, so the one backend
    that runs on most users' machines was the one never exercised — it took a
    self-test on a built binary to surface it.
    """
    import importlib

    backend = getattr(importlib.import_module(module_name), class_name)
    missing = [name for name in REQUIRED if not hasattr(backend, name)]
    assert not missing, f"{class_name} is missing {missing}"


@pytest.mark.parametrize("module_name,class_name", BACKEND_CLASSES)
def test_every_backend_names_itself_distinctly(module_name, class_name):
    """The name is what the detector prints per device, so it has to be real."""
    import importlib

    backend = getattr(importlib.import_module(module_name), class_name)
    assert isinstance(backend.version, str) and backend.version.strip()


def test_the_backend_names_are_unique():
    """Two backends sharing a name makes the detector output ambiguous."""
    import importlib

    names = [getattr(importlib.import_module(m), c).version
             for m, c in BACKEND_CLASSES]
    assert len(set(names)) == len(names), names


# -- the live readout ------------------------------------------------------


def test_the_monitor_reports_which_buttons_are_held(joystick, pad):
    """The trigger can only ever say "no"; this says what it can actually see."""
    device = pad(name="Wheel", buttons=20)
    device.press(0)
    device.press(4)

    snapshot = joystick.snapshot()
    assert len(snapshot) == 1
    found, held = snapshot[0]
    assert found.name == "Wheel"
    assert held == [1, 5]


def test_an_unreadable_device_is_not_reported_as_idle(joystick, monkeypatch):
    """A device that enumerates and then cannot be read looks exactly like one
    nobody is touching, and those need entirely different investigation."""
    backend = StubBackend("SDL3", {0: ("FANATEC Wheel", 79, "wheel-guid")})
    backend.button_mask = lambda native: None
    monkeypatch.setattr(joystick, "_BACKENDS", [backend])
    monkeypatch.setattr(joystick, "_STARTED", True)

    (_device, held), = joystick.snapshot()
    assert held is None, "unreadable must not collapse into empty"


def test_the_monitor_says_when_a_binding_resolves_to_nothing(joystick, pad):
    """The failure that is otherwise completely silent.

    Capture does not care about identity; the trigger resolves one on every
    poll. A binding that captured fine and then resolves to nothing looks
    exactly like the app ignoring you.
    """
    pad(name="Wheel", buttons=20)

    w = watcher(joystick, device=0, button=13, guid="0" * 32)
    status = w.binding_status()
    assert status["bound"] is True
    assert status["resolved"] is False


def test_the_monitor_reports_the_bound_button_being_pressed(joystick, pad):
    device = pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    w = watcher(joystick, device=0, button=13, guid=wheel)
    assert w.binding_status()["pressed"] is False

    device.press(12)
    assert w.binding_status()["pressed"] is True


def test_an_unbound_trigger_says_so(joystick):
    assert watcher(joystick).binding_status() == {"bound": False}


# -- recovering a binding whose identity changed ---------------------------


def test_a_binding_falls_back_to_the_device_name(joystick, pad):
    """Some devices report a different GUID between sessions.

    Anything Steam mediates especially. The binding then captures perfectly
    and resolves to nothing on every poll afterwards, and the only symptom is
    that pressing the button does nothing at all.
    """
    pad(name="Steam Controller", buttons=18)

    w = watcher(joystick, device=None, button=5,
                guid="deadbeef" * 4, name="Steam Controller")
    resolved = w._resolve_device()
    assert resolved is not None
    assert resolved.name == "Steam Controller"


def test_the_name_fallback_does_not_grab_a_different_device(joystick, pad):
    """Better inactive than bound to whatever else happens to be plugged in."""
    pad(name="Fanatec CSL Elite", buttons=20)

    w = watcher(joystick, device=None, button=5,
                guid="deadbeef" * 4, name="Steam Controller")
    assert w._resolve_device() is None


def test_the_identity_still_wins_when_it_matches(joystick, pad):
    """Two identical wheels are only distinguishable by identity."""
    pad(name="Wheel", buttons=20)
    wheel = guid_of(joystick, "Wheel")

    w = watcher(joystick, device=None, button=5, guid=wheel, name="Wheel")
    assert w._resolve_device().guid == wheel


# -- one backend, not all of them -----------------------------------------


def test_only_one_backend_is_used(joystick, monkeypatch):
    """SDL2 and SDL3 enumerate the same hardware through the same drivers.

    Running both means two libraries opening the same HID device in one
    process: every controller appears twice and one copy reads a button state
    that never changes. A Fanatec wheel showed up under both [SDL3] and [SDL2]
    and could not be bound at all.
    """
    first = StubBackend("SDL3", {0: ("Wheel", 20, "wheel-guid")})
    second = StubBackend("SDL2", {0: ("Wheel", 20, "other-guid")})
    monkeypatch.setattr(joystick, "_candidates", lambda: [first, second])
    monkeypatch.setattr(joystick, "_STARTED", False)
    monkeypatch.setattr(joystick, "_BACKENDS", [])

    started = joystick.backends()
    assert [b.version for b in started] == ["SDL3"]
    assert [d.name for d in joystick.devices()] == ["Wheel"]


def test_the_next_backend_is_tried_when_one_will_not_start(joystick, monkeypatch):
    class Dead(StubBackend):
        def start(self):
            return False

    dead = Dead("SDL3", {})
    alive = StubBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")})
    monkeypatch.setattr(joystick, "_candidates", lambda: [dead, alive])
    monkeypatch.setattr(joystick, "_STARTED", False)
    monkeypatch.setattr(joystick, "_BACKENDS", [])

    assert [b.version for b in joystick.backends()] == ["SDL2"]


def test_the_detector_names_the_backends_not_in_use(joystick, monkeypatch):
    """Otherwise there is no way to know another one could be tried."""
    monkeypatch.setattr(joystick, "_candidates", lambda: [
        StubBackend("SDL3", {}), StubBackend("SDL2", {})])
    monkeypatch.setattr(joystick, "_STARTED", False)
    monkeypatch.setattr(joystick, "_BACKENDS", [])

    lines = "\n".join(joystick.diagnose())
    assert "backend in use: SDL3" in lines
    assert "not in use: SDL2" in lines
    assert "joystick.backend" in lines


def test_a_backend_can_be_pinned(joystick, monkeypatch):
    monkeypatch.setattr(joystick, "_STARTED", False)
    monkeypatch.setattr(joystick, "_BACKENDS", [])
    monkeypatch.setattr(joystick, "_preferred", "sdl2")

    names = [b.version for b in joystick._candidates()]
    assert names == ["SDL2"]


def test_pinning_something_unknown_falls_back_to_automatic(joystick, monkeypatch):
    """A typo in config.json must not leave the app with no input at all."""
    monkeypatch.setattr(joystick, "_preferred", "sdl9")
    assert len(joystick._candidates()) > 1


def test_a_device_with_no_inputs_is_not_listed(joystick, monkeypatch):
    """Real hardware the legacy interface cannot describe.

    A Fanatec wheel appears through the Windows multimedia API as the generic
    "Microsoft PC-joystick driver" with 0 usable inputs — the same wheel SDL3
    and SDL2 were also both holding open. Nothing can be bound on it, so
    listing it only invites someone to try.
    """
    monkeypatch.setattr(joystick, "_BACKENDS", [
        StubBackend("Windows legacy", {
            0: ("Microsoft PC-joystick driver", 0, "legacy:phantom"),
            1: ("Real Wheel", 20, "wheel-guid"),
        })])
    monkeypatch.setattr(joystick, "_STARTED", True)

    assert [d.name for d in joystick.devices()] == ["Real Wheel"]


def test_presses_are_logged_whatever_has_focus(joystick, pad, caplog):
    """Binding needs the window focused, and for anything Steam mediates that
    changes what the controller is: Steam Input applies its Desktop
    configuration to a non-Steam window and the game's configuration to the
    game. A button captured from the settings window is therefore not
    necessarily the button pressed while racing.

    So the press has to be readable after the fact, from the log, pressed at
    the moment that matters.
    """
    import logging

    device = pad(name="Steam Controller", buttons=18)
    w = watcher(joystick)
    w.set_press_logging(True)
    w._poll_press_log()

    with caplog.at_level(logging.INFO, logger="pitradio.input.joystick"):
        device.press(6)
        w._poll_press_log()

    logged = [r.getMessage() for r in caplog.records
              if "controller press" in r.getMessage()]
    assert logged, "the press was not logged"
    assert "button 7" in logged[0]
    assert "Steam Controller" in logged[0]


def test_a_held_button_is_logged_once(joystick, pad, caplog):
    """Otherwise holding it fills the log at eighty lines a second."""
    import logging

    device = pad(name="Wheel", buttons=20)
    w = watcher(joystick)
    w.set_press_logging(True)
    w._poll_press_log()

    with caplog.at_level(logging.INFO, logger="pitradio.input.joystick"):
        device.press(3)
        for _ in range(5):
            w._poll_press_log()

    logged = [r for r in caplog.records if "controller press" in r.getMessage()]
    assert len(logged) == 1


def test_capture_ignores_a_sensor_that_chatters(joystick, pad):
    """A Steam Controller's touchpads and grip sensors report as buttons.

    On a real one, 20 and 22 come and go on their own. A single-frame baseline
    only excludes what happened to be active at that instant, so the next blip
    read as a fresh press and took the binding — leaving a binding to a phantom
    that never fires when a real button is pressed.
    """
    device = pad(name="Steam Controller", buttons=26)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    # Chattering through the settling window: on, off, on, off...
    for poll in range(joystick.CAPTURE_SETTLE_POLLS):
        device.press(21, down=poll % 2 == 0)
        w._poll_capture()

    # It keeps chattering; none of it may be taken as a press.
    for poll in range(20):
        device.press(21, down=poll % 2 == 0)
        w._poll_capture()
    assert captured == []

    # A real, sustained press still wins.
    device.press(4)
    confirm(joystick, w)
    assert captured == [5]


def test_a_single_frame_blip_cannot_take_the_binding(joystick, pad):
    """One poll of noise is not a press; a deliberate one lasts far longer."""
    device = pad(name="Steam Controller", buttons=26)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))
    settle(joystick, w)

    device.press(9)
    w._poll_capture()          # seen once
    device.press(9, down=False)
    w._poll_capture()          # gone again
    assert captured == []


# -- reading, not owning ---------------------------------------------------


def test_the_sdl_backends_do_not_take_a_controller_by_default(joystick):
    """Reading a button state does not require owning the device.

    SDL's HIDAPI Steam driver opens a Steam Controller directly, which takes it
    from Steam: its desktop keyboard and mouse shortcuts stop working, and the
    raw device reports touchpads and grip sensors as buttons that sit active or
    chatter. Breaking another application to read a button is the wrong trade.
    """
    joystick._take_over_steam = False
    for backend in joystick._candidates():
        if backend.version.startswith("SDL"):
            assert backend.steam_hidapi is False


def test_it_can_be_turned_on_for_a_controller_nothing_else_sees(joystick):
    joystick.prefer("auto", True)
    try:
        sdl = [b for b in joystick._candidates() if b.version.startswith("SDL")]
        assert sdl and all(b.steam_hidapi is True for b in sdl)
    finally:
        joystick.prefer("auto", False)


def test_a_real_press_wins_through_intermittent_noise(joystick, pad):
    """A Steam Controller's gyro flags fire whenever the controller moves —
    and it moves while you press a button.

    Tracking one candidate and resetting it whenever a different input appears
    meant that noise never won the binding but stopped anything else winning
    either: the real press could not accumulate enough consecutive polls.
    """
    device = pad(name="Steam Controller", buttons=26)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))
    settle(joystick, w)

    # A real press held down, while a gyro flag blinks alongside it.
    device.press(3)
    for poll in range(joystick.CAPTURE_CONFIRM_POLLS + 2):
        device.press(21, down=poll % 2 == 0)
        w._poll_capture()

    assert captured == [4], "the deliberate press should have been taken"


def test_noise_alone_still_cannot_bind(joystick, pad):
    """Counting every input must not make chatter easier to capture."""
    device = pad(name="Steam Controller", buttons=26)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))
    settle(joystick, w)

    for poll in range(40):
        device.press(21, down=poll % 2 == 0)
        w._poll_capture()

    assert captured == []
