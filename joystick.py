"""Wheel and gamepad buttons as a trigger.

Two things here are copied from how sims handle controller bindings, because
both are the difference between a binding that keeps working and one that
appears to break for no reason.

**Bindings are stored against the device, not its position.** SDL device
indices are positional: unplug a pedal set, launch Steam, or plug in a headset
that enumerates as a controller, and every index after it shifts. A binding
recorded as "device 2, button 13" then quietly points at a different device.
Sims store a device identity — SDL calls it a GUID — and resolve it to whatever
index that device currently occupies. So does this. The index is still written
to the config as a fallback for bindings made before identities were recorded.

**Capture waits for a button to be pressed, not to be held.** Wheel rims and
button boxes are covered in toggle switches and rotary encoders that sit in a
permanently-on position, so "the first button that reads as down" captures a
switch nobody touched. Capture snapshots what is already held and waits for a
change, which is what makes it possible to bind a button on a rim that has a
latched switch on it.

Buttons are polled rather than delivered as events. That is also what makes the
trigger global: reading device state asks the driver and does not care which
window has focus, so a button on the wheel fires while the sim is in front.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import sdlinput
import winapi

log = logging.getLogger(__name__)

# SDL is preferred and the legacy interface is the fallback. SDL's HIDAPI
# drivers see devices the legacy API cannot -- a Steam Controller is invisible
# to it entirely -- but SDL is a bundled DLL that can fail to load, and this app
# must not lose its trigger because of that.
_sdl = sdlinput.SdlJoysticks()
_use_sdl: bool | None = None

TRIGGER_DOWN = "down"
TRIGGER_UP = "up"

POLL_SECONDS = 0.012
MAX_DEVICES = 16


def backend():
    """The SDL backend, or None when the legacy interface is in use."""
    global _use_sdl
    if _use_sdl is None:
        _use_sdl = _sdl.start()
        if not _use_sdl:
            log.info(
                "SDL2 unavailable (%s); falling back to the legacy joystick "
                "interface, which cannot see Steam Input devices",
                _sdl.failure,
            )
    return _sdl if _use_sdl else None


def backend_name() -> str:
    return "SDL2" if backend() is not None else "Windows legacy joystick API"


@dataclass(frozen=True)
class Device:
    """One attached controller, as both the config and the GUI need it."""

    index: int
    name: str
    buttons: int
    guid: str

    @property
    def key(self) -> str:
        """What a binding is matched on."""
        return self.guid or f"index:{self.index}"


def _mask(device: int) -> int | None:
    sdl = backend()
    return sdl.button_mask(device) if sdl else winapi.joystick_button_mask(device)


def _name(device: int) -> str | None:
    sdl = backend()
    return sdl.name(device) if sdl else winapi.joystick_name(device)


def _guid(device: int, name: str | None = None) -> str:
    """Stable identity for a device.

    The legacy interface has no notion of one, so its name stands in. That is
    weaker than a GUID — two identical wheels are indistinguishable — but it
    still survives the reordering that breaks a bare index, which is the failure
    this exists to prevent.
    """
    sdl = backend()
    if sdl is not None:
        return sdl.guid(device) or ""
    label = name if name is not None else winapi.joystick_name(device)
    return f"legacy:{label}" if label else ""


def _label(device: int, button: int) -> str:
    sdl = backend()
    return sdl.label(device, button) if sdl else f"button {button}"


def devices() -> list[Device]:
    """Every attached controller, with the identity a binding is stored against."""
    sdl = backend()
    if sdl is not None:
        return [Device(index, name, buttons, sdl.guid(index) or "")
                for index, name, buttons in sdl.list_devices()]

    found = []
    for index in range(min(winapi.joystick_count(), MAX_DEVICES)):
        name = winapi.joystick_name(index)
        if name is None:
            continue
        # A device with a name but no readable state is present in the driver
        # list but not actually connected.
        if winapi.joystick_button_mask(index) is None:
            continue
        found.append(Device(index, name, winapi.joystick_buttons(index) or 0,
                            _guid(index, name)))
    return found


def list_devices() -> list[tuple[int, str, int]]:
    """(device id, name, button count) for every attached joystick."""
    return [(d.index, d.name, d.buttons) for d in devices()]


def diagnose() -> list[str]:
    """Why a controller might not be showing up.

    This reports what was found rather than leaving the user to guess. Steam
    Input in particular can capture a controller and re-present it in a form the
    legacy interface never enumerates, and a Steam Controller in desktop mode
    acts as a keyboard and mouse rather than a joystick at all.
    """
    lines = [f"backend: {backend_name()}"]
    if backend() is None and _sdl.failure:
        lines.append(f"  SDL2 did not load: {_sdl.failure}")

    found = devices()
    for device in found:
        identity = device.guid or "no identity"
        lines.append(
            f"  device {device.index}: {device.name!r} — "
            f"{device.buttons} inputs [{identity}]"
        )

    if not found:
        lines.append(
            "  no controllers detected. If one is plugged in and this is the "
            "legacy backend, Steam Input is the usual cause — it hides the "
            "device from that interface."
        )
    return lines


def describe(device: int, button: int) -> str:
    """Human-readable binding, e.g. 'Fanatec CSL Elite - button 13'."""
    return f"{_name(device) or f'joystick {device}'} - {_label(device, button)}"


class JoystickWatcher(threading.Thread):
    """Polls one button and posts down/up onto the shared trigger queue.

    Publishes the same event kinds as the keyboard hook, so the worker cannot
    tell — and does not need to tell — which one fired.
    """

    def __init__(
        self,
        events: queue.Queue[tuple[str, float]],
        is_enabled: Callable[[], bool],
        device: int | None = None,
        button: int | None = None,
        guid: str | None = None,
    ):
        super().__init__(name="joystick", daemon=True)
        self._events = events
        self._is_enabled = is_enabled
        self._device = device
        self._button = button
        self._guid = guid or ""
        self._resolved: int | None = None
        self._pressed = False
        self._capture: Callable[[Device, int], None] | None = None
        self._baseline: dict[str, int] | None = None
        self._stop = threading.Event()
        self._missing_logged = False

    # -- public ----------------------------------------------------------

    # The GUI holds a watcher, not the module, so enumeration has to be
    # reachable from the instance. Shipping these as module functions only is
    # what broke v0.1.4 on startup.
    def list_devices(self) -> list[tuple[int, str, int]]:
        return list_devices()

    def devices(self) -> list[Device]:
        return devices()

    def describe(self, device: int, button: int) -> str:
        return describe(device, button)

    def diagnose(self) -> list[str]:
        return diagnose()

    def set_binding(
        self, device: int | None, button: int | None, guid: str | None = None
    ) -> None:
        self._device, self._button = device, button
        self._guid = guid or ""
        self._resolved = None
        self._pressed = False
        self._missing_logged = False

    def start_capture(self, on_captured: Callable[[Device, int], None]) -> None:
        """Report the next button *pressed* on any device.

        Buttons already held when capture starts are ignored until they are
        released and pressed again — see the module docstring.
        """
        self._baseline = None
        self._capture = on_captured

    def cancel_capture(self) -> None:
        self._capture = None
        self._baseline = None

    def stop(self) -> None:
        self._stop.set()
        if backend() is not None:
            _sdl.stop()

    # -- thread body -----------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._capture is not None:
                    self._poll_capture()
                elif self._button is not None:
                    self._poll_binding()
            except Exception:
                # A disconnected wheel must not take the thread down; the
                # trigger would then be silently dead until a restart.
                log.exception("joystick poll failed")
            self._stop.wait(POLL_SECONDS)

    def _resolve_index(self) -> int | None:
        """Where the bound device currently sits, or None if it is not attached."""
        if not self._guid:
            return self._device

        # Re-enumerating every poll would be wasteful, so trust the last answer
        # for as long as that index still holds the same device.
        if self._resolved is not None and _guid(self._resolved) == self._guid:
            return self._resolved

        for device in devices():
            if device.guid == self._guid:
                self._resolved = device.index
                return device.index

        self._resolved = None
        return None

    def _poll_binding(self) -> None:
        index = self._resolve_index()
        mask = None if index is None else _mask(index)
        if mask is None:
            if not self._missing_logged:
                self._missing_logged = True
                log.warning(
                    "the controller bound to the trigger is not responding; "
                    "its button trigger is inactive until it reconnects"
                )
            # A device that vanishes mid-press must not leave the cycle open.
            if self._pressed:
                self._pressed = False
                if self._is_enabled():
                    self._post(TRIGGER_UP)
            return

        if self._missing_logged:
            log.info("the controller bound to the trigger is back")
        self._missing_logged = False

        down = bool(mask & (1 << (self._button - 1)))
        if down and not self._pressed:
            self._pressed = True
            if self._is_enabled():
                self._post(TRIGGER_DOWN)
        elif not down and self._pressed:
            self._pressed = False
            if self._is_enabled():
                self._post(TRIGGER_UP)

    def _poll_capture(self) -> None:
        snapshot: dict[str, tuple[Device, int]] = {}
        for device in devices():
            mask = _mask(device.index)
            if mask is None:
                continue
            snapshot[device.key] = (device, mask)

        # First pass records what is already held, so a latched switch on the
        # rim does not win the race against the button being pressed.
        if self._baseline is None:
            self._baseline = {key: mask for key, (_, mask) in snapshot.items()}
            return

        for key, (device, mask) in snapshot.items():
            fresh = mask & ~self._baseline.get(key, 0)
            if not fresh:
                continue
            # Lowest set bit wins if several arrive in the same poll, so the
            # result is stable rather than dependent on iteration order.
            button = (fresh & -fresh).bit_length()
            callback, self._capture = self._capture, None
            self._baseline = None
            try:
                callback(device, button)
            except Exception:
                log.exception("joystick capture callback failed")
            return

        # Released buttons stop counting as held, so a switch can be bound by
        # flicking it off and on again.
        for key, (_, mask) in snapshot.items():
            self._baseline[key] = self._baseline.get(key, 0) & mask

    def _post(self, kind: str) -> None:
        try:
            self._events.put_nowait((kind, time.monotonic()))
        except queue.Full:
            log.warning("trigger queue full; dropped joystick %s", kind)
