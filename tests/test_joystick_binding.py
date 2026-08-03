"""Backend aggregation, binding resolution and button capture.

`joystick.py` imports `winapi`, so it cannot be imported directly off Windows —
but the logic that has actually been going wrong is all platform-independent:
which backend a device came from, which device a binding resolves to, and which
button a capture picks. Stubbing `winapi` and substituting fake backends makes
all of it testable, which matters because every one of those failures is
silent. A binding that resolves to the wrong device and a capture that grabs a
latched switch both look exactly like "the trigger does nothing".

The fakes implement the backend interface rather than patching module helpers,
so these exercise the real aggregation path.
"""

import queue
import sys
import types

import pytest


class FakeBackend:
    """A backend with a fixed device list and settable button state."""

    def __init__(self, version, devices, fails=None):
        self.version = version
        # native id -> (name, buttons, guid)
        self._devices = dict(devices)
        self.masks = {}
        self.failure = fails
        self.stopped = False

    def start(self):
        return self.failure is None

    def stop(self):
        self.stopped = True

    def list_devices(self):
        return [(native, name, buttons)
                for native, (name, buttons, _guid) in self._devices.items()]

    def guid(self, native):
        entry = self._devices.get(native)
        return entry[2] if entry else None

    def button_mask(self, native):
        if native not in self._devices:
            return None
        return self.masks.get(native)

    def label(self, native, button):
        return f"button {button}"

    def name(self, native):
        entry = self._devices.get(native)
        return entry[0] if entry else None

    def detach(self, native):
        self._devices.pop(native, None)
        self.masks.pop(native, None)


@pytest.fixture
def joystick(monkeypatch):
    """`joystick` with winapi stubbed and no real backends started."""
    if "winapi" not in sys.modules:
        stub = types.ModuleType("winapi")
        stub.joystick_count = lambda: 0
        stub.joystick_name = lambda index: None
        stub.joystick_buttons = lambda index: 0
        stub.joystick_button_mask = lambda index: None
        monkeypatch.setitem(sys.modules, "winapi", stub)

    monkeypatch.delitem(sys.modules, "joystick", raising=False)
    import joystick as module

    # Never start the real backends: they would load SDL and open every
    # controller attached to the developer's machine.
    monkeypatch.setattr(module, "_STARTED", True)
    monkeypatch.setattr(module, "_BACKENDS", [])
    return module


def use(module, *backends):
    module._BACKENDS[:] = list(backends)
    return backends


def watcher(module, **kwargs):
    return module.JoystickWatcher(queue.Queue(), lambda: True, **kwargs)


# -- aggregating the backends ---------------------------------------------


def test_devices_from_every_backend_are_combined(joystick):
    """A wheel on one backend and a pad on another is an ordinary rig."""
    use(joystick,
        FakeBackend("SDL2", {0: ("Fanatec CSL Elite", 20, "wheel-guid")}),
        FakeBackend("XInput", {0: ("XInput controller 1", 14, "xinput:0")}))

    found = joystick.devices()
    assert [d.name for d in found] == ["Fanatec CSL Elite", "XInput controller 1"]
    assert [d.api for d in found] == ["SDL2", "XInput"]


def test_each_device_reports_which_api_found_it(joystick):
    """The first thing worth knowing when a device does not work."""
    use(joystick, FakeBackend("SDL3", {7: ("Steam Controller", 18, "steam-guid")}))

    lines = joystick.diagnose()
    assert any("[SDL3]" in line and "Steam Controller" in line for line in lines)


def test_the_same_device_seen_twice_appears_once(joystick):
    """A pad both SDL3 and XInput can see must not be offered twice.

    The earlier backend wins, because it is the one that knows the device's
    real name and button count.
    """
    use(joystick,
        FakeBackend("SDL3", {0: ("Xbox Wireless Controller", 18, "shared-guid")}),
        FakeBackend("XInput", {0: ("XInput controller 1", 14, "shared-guid")}))

    found = joystick.devices()
    assert len(found) == 1
    assert found[0].api == "SDL3"
    assert found[0].name == "Xbox Wireless Controller"


def test_a_backend_that_throws_costs_only_itself(joystick):
    """One broken backend must not hide the devices the others can see."""

    class Exploding(FakeBackend):
        def list_devices(self):
            raise RuntimeError("boom")

    use(joystick,
        Exploding("SDL3", {}),
        FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))

    assert [d.name for d in joystick.devices()] == ["Wheel"]


def test_no_devices_anywhere_says_so(joystick):
    use(joystick, FakeBackend("SDL3", {}), FakeBackend("SDL2", {}))
    assert any("no controllers detected" in line for line in joystick.diagnose())


# -- resolving a binding to a device --------------------------------------


def test_a_binding_follows_its_device_across_backends(joystick):
    """Identity is the key, not position, and not which backend found it."""
    use(joystick,
        FakeBackend("SDL2", {0: ("Headset", 2, "headset-guid"),
                             1: ("Fanatec CSL Elite", 20, "wheel-guid")}))

    w = watcher(joystick, device=0, button=13, guid="wheel-guid")
    device = w._resolve_device()
    assert device is not None
    assert device.name == "Fanatec CSL Elite"


def test_a_missing_device_resolves_to_nothing(joystick):
    """Better inactive than bound to whichever device took its place."""
    use(joystick, FakeBackend("SDL2", {0: ("Pedals", 4, "pedals-guid")}))

    w = watcher(joystick, device=0, button=13, guid="wheel-guid")
    assert w._resolve_device() is None


def test_a_binding_with_no_identity_falls_back_to_its_index(joystick):
    """Configs written before identities were recorded must keep working."""
    use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, ""), 1: ("Pad", 14, "")}))

    w = watcher(joystick, device=1, button=4, guid=None)
    device = w._resolve_device()
    assert device is not None and device.name == "Pad"


# -- capture ---------------------------------------------------------------


def test_capture_ignores_a_switch_that_is_already_held(joystick):
    """Rims and button boxes carry latched switches and rotary encoders.

    Taking the first button that reads as down captures one nobody touched,
    which is indistinguishable from capture being broken.
    """
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0b00001          # a latched switch

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.guid, button)))

    w._poll_capture()                   # snapshot it
    assert captured == []
    w._poll_capture()                   # still only the switch
    assert captured == []
    backend.masks[0] = 0b10001          # a real press
    w._poll_capture()

    assert captured == [("wheel-guid", 5)]


def test_capture_takes_a_press_on_any_device(joystick):
    """A wheel base, its rim and the pedals enumerate separately."""
    backend, = use(joystick, FakeBackend("SDL2", {
        0: ("Wheel", 20, "wheel-guid"),
        1: ("Button box", 32, "box-guid"),
    }))
    backend.masks[0] = 0
    backend.masks[1] = 0

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.name, button)))

    w._poll_capture()
    backend.masks[1] = 1 << 12
    w._poll_capture()

    assert captured == [("Button box", 13)]


def test_capture_works_across_backends(joystick):
    """The pad you want to bind may not be on the same backend as the wheel."""
    sdl, xin = use(joystick,
                   FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}),
                   FakeBackend("XInput", {0: ("XInput controller 1", 14, "xinput:0")}))
    sdl.masks[0] = 0
    xin.masks[0] = 0

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.api, button)))

    w._poll_capture()
    xin.masks[0] = 1 << 2
    w._poll_capture()

    assert captured == [("XInput", 3)]


def test_a_released_switch_can_be_bound_by_flicking_it(joystick):
    """Otherwise a toggle switch could never be bound at all."""
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0b1

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    w._poll_capture()          # held
    backend.masks[0] = 0
    w._poll_capture()          # released — stops counting as held
    backend.masks[0] = 0b1
    w._poll_capture()          # pressed again

    assert captured == [1]


def test_capture_fires_once(joystick):
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))
    w._poll_capture()
    backend.masks[0] = 1
    for _ in range(3):
        w._poll_capture()

    assert captured == [1]


# -- the trigger itself ----------------------------------------------------


def test_pressing_and_releasing_posts_one_cycle(joystick):
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid="wheel-guid")
    w._poll_binding()
    backend.masks[0] = 1 << 12
    w._poll_binding()
    w._poll_binding()
    backend.masks[0] = 0
    w._poll_binding()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


def test_a_controller_unplugged_mid_press_closes_the_cycle(joystick):
    """A DOWN with no UP leaves the worker recording until something else fires."""
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 1 << 12

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid="wheel-guid")
    w._poll_binding()
    backend.detach(0)
    w._poll_binding()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


# -- send / clear buttons --------------------------------------------------


def test_an_action_button_fires_once_per_press(joystick):
    """Momentary, not held: the worker never has to pair press with release."""
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: ("wheel-guid", 0, 5)})

    w._poll_actions()
    backend.masks[0] = 1 << 4
    w._poll_actions()
    w._poll_actions()          # still held — no second event
    backend.masks[0] = 0
    w._poll_actions()
    backend.masks[0] = 1 << 4
    w._poll_actions()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_SEND, joystick.TRIGGER_SEND]


def test_send_and_clear_are_independent(joystick):
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({
        joystick.TRIGGER_SEND: ("wheel-guid", 0, 1),
        joystick.TRIGGER_CLEAR: ("wheel-guid", 0, 2),
    })
    w._poll_actions()
    backend.masks[0] = 1 << 1          # button 2 only
    w._poll_actions()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_CLEAR]


def test_an_action_on_a_missing_device_is_silent(joystick):
    """Not an error: the wheel may simply be unplugged."""
    use(joystick, FakeBackend("SDL2", {}))

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: ("gone-guid", 0, 3)})
    w._poll_actions()

    assert events.empty()


def test_actions_and_the_talk_trigger_share_one_poll(joystick):
    """Both must be serviced each tick, or binding one would disable the other."""
    backend, = use(joystick, FakeBackend("SDL2", {0: ("Wheel", 20, "wheel-guid")}))
    backend.masks[0] = 0b10001         # talk on button 1, send on button 5

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=1, guid="wheel-guid")
    w.set_actions({joystick.TRIGGER_SEND: ("wheel-guid", 0, 5)})
    w._poll_binding()
    w._poll_actions()

    kinds = sorted(events.get_nowait()[0] for _ in range(events.qsize()))
    assert kinds == sorted([joystick.TRIGGER_DOWN, joystick.TRIGGER_SEND])
