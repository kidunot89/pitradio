"""Binding resolution and button capture.

`joystick.py` imports `winapi`, so it cannot be imported directly off Windows —
but the logic that has actually been going wrong is all platform-independent:
which device a binding resolves to, and which button a capture picks. Stubbing
the two backend modules makes that testable, which matters because both
failures are silent. A binding that resolves to the wrong device and a capture
that grabs a latched switch both look exactly like "the trigger does nothing".
"""

import queue
import sys
import types

import pytest


@pytest.fixture
def joystick(monkeypatch):
    """`joystick` with both backends stubbed out."""
    if "winapi" not in sys.modules:
        stub = types.ModuleType("winapi")
        stub.joystick_count = lambda: 0
        stub.joystick_name = lambda index: None
        stub.joystick_buttons = lambda index: 0
        stub.joystick_button_mask = lambda index: None
        monkeypatch.setitem(sys.modules, "winapi", stub)

    monkeypatch.delitem(sys.modules, "joystick", raising=False)
    import joystick as module

    # Never touch real SDL: start() would load a DLL and open every controller
    # attached to the developer's machine.
    monkeypatch.setattr(module, "backend", lambda: None)
    return module


def device(module, index, name, buttons=16, guid=""):
    return module.Device(index=index, name=name, buttons=buttons, guid=guid)


def watcher(module, **kwargs):
    return module.JoystickWatcher(queue.Queue(), lambda: True, **kwargs)


# -- resolving a binding to a device --------------------------------------


def test_a_binding_follows_its_device_when_indices_shift(joystick, monkeypatch):
    """The whole reason identities are stored: indices are positional.

    Plugging in anything that enumerates first pushes the wheel along by one.
    Resolving by index would then bind the trigger to the new device.
    """
    wheel = device(joystick, 2, "Fanatec CSL Elite", guid="wheel-guid")
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Headset"),
        device(joystick, 1, "Pedals", guid="pedals-guid"),
        wheel,
    ])
    monkeypatch.setattr(joystick, "_guid", lambda index: {
        0: "", 1: "pedals-guid", 2: "wheel-guid"}.get(index, ""))

    w = watcher(joystick, device=0, button=13, guid="wheel-guid")
    assert w._resolve_index() == 2


def test_a_missing_device_resolves_to_nothing(joystick, monkeypatch):
    """Better inactive than bound to whichever device took its place."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Pedals", guid="pedals-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "pedals-guid")

    w = watcher(joystick, device=0, button=13, guid="wheel-guid")
    assert w._resolve_index() is None


def test_a_binding_with_no_identity_falls_back_to_its_index(joystick):
    """Configs written before identities were recorded must keep working."""
    w = watcher(joystick, device=1, button=4, guid=None)
    assert w._resolve_index() == 1


# -- capture ---------------------------------------------------------------


def test_capture_ignores_a_switch_that_is_already_held(joystick, monkeypatch):
    """Rims and button boxes carry latched switches and rotary encoders.

    Taking the first button that reads as down captures one nobody touched,
    which is indistinguishable from capture being broken.
    """
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    # Bit 0 held throughout; bit 4 pressed on the third poll.
    masks = iter([0b00001, 0b00001, 0b10001])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.guid, button)))

    w._poll_capture()              # snapshot the latched switch
    assert captured == []
    w._poll_capture()              # still only the latched switch
    assert captured == []
    w._poll_capture()              # a real press

    assert captured == [("wheel-guid", 5)]


def test_capture_takes_a_press_on_any_device(joystick, monkeypatch):
    """A wheel base, its rim and the pedals enumerate separately."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid"),
        device(joystick, 1, "Button box", guid="box-guid"),
    ])
    state = {0: 0, 1: 0}
    monkeypatch.setattr(joystick, "_mask", lambda index: state[index])

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append((dev.name, button)))

    w._poll_capture()
    state[1] = 1 << 12
    w._poll_capture()

    assert captured == [("Button box", 13)]


def test_a_released_switch_can_be_bound_by_flicking_it(joystick, monkeypatch):
    """Otherwise a toggle switch could never be bound at all."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    masks = iter([0b1, 0b0, 0b1])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))

    w._poll_capture()   # held
    w._poll_capture()   # released — stops counting as held
    w._poll_capture()   # pressed again

    assert captured == [1]


def test_capture_fires_once(joystick, monkeypatch):
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    masks = iter([0, 1, 1, 1])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    captured = []
    w = watcher(joystick)
    w.start_capture(lambda dev, button: captured.append(button))
    for _ in range(4):
        w._poll_capture()

    assert captured == [1]


# -- the trigger itself ----------------------------------------------------


def test_pressing_and_releasing_posts_one_cycle(joystick, monkeypatch):
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "wheel-guid")
    masks = iter([0, 1 << 12, 1 << 12, 0])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid="wheel-guid")
    for _ in range(4):
        w._poll_binding()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


def test_a_controller_unplugged_mid_press_closes_the_cycle(joystick, monkeypatch):
    """A DOWN with no UP leaves the worker recording until something else fires."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "wheel-guid")
    masks = iter([1 << 12, None])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=13, guid="wheel-guid")
    w._poll_binding()
    w._poll_binding()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_DOWN, joystick.TRIGGER_UP]


# -- send / clear buttons --------------------------------------------------


def test_an_action_button_fires_once_per_press(joystick, monkeypatch):
    """Momentary, not held: the worker never has to pair a press with a release."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "wheel-guid")
    masks = iter([0, 1 << 4, 1 << 4, 1 << 4, 0, 1 << 4])
    monkeypatch.setattr(joystick, "_mask", lambda index: next(masks))

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: ("wheel-guid", 0, 5)})
    for _ in range(6):
        w._poll_actions()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_SEND, joystick.TRIGGER_SEND]


def test_send_and_clear_are_independent(joystick, monkeypatch):
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "wheel-guid")
    # One mask read per action per poll, so a scripted iterator would run out.
    held = {"mask": 0}
    monkeypatch.setattr(joystick, "_mask", lambda index: held["mask"])

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({
        joystick.TRIGGER_SEND: ("wheel-guid", 0, 1),
        joystick.TRIGGER_CLEAR: ("wheel-guid", 0, 2),
    })
    w._poll_actions()
    held["mask"] = 1 << 1              # button 2 only
    w._poll_actions()

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert kinds == [joystick.TRIGGER_CLEAR]


def test_an_action_on_a_missing_device_is_silent(joystick, monkeypatch):
    """Not an error: the wheel may simply be unplugged."""
    monkeypatch.setattr(joystick, "devices", lambda: [])
    monkeypatch.setattr(joystick, "_guid", lambda index: "")

    events = queue.Queue()
    w = joystick.JoystickWatcher(events, lambda: True)
    w.set_actions({joystick.TRIGGER_SEND: ("gone-guid", 0, 3)})
    w._poll_actions()

    assert events.empty()


def test_actions_and_the_talk_trigger_share_one_poll(joystick, monkeypatch):
    """Both must be serviced each tick, or binding one would disable the other."""
    monkeypatch.setattr(joystick, "devices", lambda: [
        device(joystick, 0, "Wheel", guid="wheel-guid")])
    monkeypatch.setattr(joystick, "_guid", lambda index: "wheel-guid")
    # Talk on button 1, send on button 5, both pressed at once.
    monkeypatch.setattr(joystick, "_mask", lambda index: 0b10001)

    events = queue.Queue()
    w = joystick.JoystickWatcher(
        events, lambda: True, device=0, button=1, guid="wheel-guid")
    w.set_actions({joystick.TRIGGER_SEND: ("wheel-guid", 0, 5)})
    w._poll_binding()
    w._poll_actions()

    kinds = sorted(events.get_nowait()[0] for _ in range(events.qsize()))
    assert kinds == sorted([joystick.TRIGGER_DOWN, joystick.TRIGGER_SEND])
