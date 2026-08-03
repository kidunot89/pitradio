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

    w._poll_capture()                      # snapshot it
    assert captured == []
    w._poll_capture()
    assert captured == []

    device.press(4)                        # a real press
    w._poll_capture()
    assert captured == [5]


def test_capture_takes_a_press_on_any_device(joystick, pad):
    """A wheel base, its rim and the pedals enumerate separately."""
    pad(name="Wheel", buttons=20)
    box = pad(name="Button Box", buttons=32)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.name, button)))

    w._poll_capture()
    box.press(12)
    w._poll_capture()

    assert captured == [("Button Box", 13)]


def test_a_hat_can_be_captured(joystick, pad):
    """A rim's D-pad is a hat, and was invisible to capture before."""
    from pitradio.input import sdl3input

    device = pad(name="Rim", buttons=10, hats=1)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    w._poll_capture()
    device.hat(0, sdl3input.SDL_HAT_UP)
    w._poll_capture()

    assert captured == [11]                # first input after the 10 buttons


def test_a_released_switch_can_be_bound_by_flicking_it(joystick, pad):
    """Otherwise a toggle switch could never be bound at all."""
    device = pad(name="Wheel", buttons=20)
    device.press(0)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    w._poll_capture()                      # held
    device.press(0, down=False)
    w._poll_capture()                      # released — stops counting as held
    device.press(0)
    w._poll_capture()                      # pressed again

    assert captured == [1]


def test_capture_fires_once(joystick, pad):
    device = pad(name="Wheel", buttons=20)

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    w._poll_capture()
    device.press(0)
    for _ in range(3):
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
