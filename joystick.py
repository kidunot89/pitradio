"""Wheel and gamepad buttons as a trigger.

Uses Windows' legacy multimedia joystick API through ctypes rather than SDL or
pygame. That is a deliberate trade: the legacy API caps at 32 buttons per
device and won't see force-feedback-only or purely HID devices, but it adds no
dependency — and every native dependency this app has taken on has cost a
release to get bundled correctly.

Buttons are polled rather than delivered as events, because the API has no
event interface. The poll interval is a compromise: fast enough that a press
isn't perceptibly late, slow enough to be invisible in a frame time.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import sdlinput
import winapi

log = logging.getLogger(__name__)

# SDL is preferred and the legacy interface is the fallback. SDL's HIDAPI
# drivers see devices the legacy API cannot -- a Steam Controller is invisible
# to it entirely -- but SDL is a bundled DLL that can fail to load, and this app
# must not lose its trigger because of that.
_sdl = sdlinput.SdlJoysticks()
_use_sdl: bool | None = None


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


def _mask(device: int) -> int | None:
    sdl = backend()
    return sdl.button_mask(device) if sdl else winapi.joystick_button_mask(device)


def _name(device: int) -> str | None:
    sdl = backend()
    return sdl.name(device) if sdl else winapi.joystick_name(device)

TRIGGER_DOWN = "down"
TRIGGER_UP = "up"

POLL_SECONDS = 0.012
MAX_DEVICES = 16


def list_devices() -> list[tuple[int, str, int]]:
    """(device id, name, button count) for every attached joystick."""
    sdl = backend()
    if sdl is not None:
        return sdl.list_devices()

    devices = []
    for device in range(min(winapi.joystick_count(), MAX_DEVICES)):
        name = winapi.joystick_name(device)
        if name is None:
            continue
        # A device with a name but no readable state is present in the driver
        # list but not actually connected.
        if winapi.joystick_button_mask(device) is None:
            continue
        devices.append((device, name, winapi.joystick_buttons(device) or 0))
    return devices


def diagnose() -> list[str]:
    """Why a controller might not be showing up.

    This API cannot see everything. Steam Input in particular can capture a
    controller and re-present it in a form the legacy interface never
    enumerates, and a Steam Controller in desktop mode acts as a keyboard and
    mouse rather than a joystick at all. When a device is missing, the useful
    question is whether Windows sees it here at all — so report what was found
    rather than leaving the user to guess.
    """
    lines = [f"backend: {backend_name()}"]
    if backend() is None and _sdl.failure:
        lines.append(f"  SDL2 did not load: {_sdl.failure}")

    devices = list_devices()
    for device, name, buttons in devices:
        lines.append(f"  device {device}: {name!r} — {buttons} buttons")

    if not devices:
        lines.append(
            "  no controllers detected. If one is plugged in and this is the "
            "legacy backend, Steam Input is the usual cause — it hides the "
            "device from that interface."
        )
    return lines


def describe(device: int, button: int) -> str:
    """Human-readable binding, e.g. 'Fanatec CSL Elite - button 13'."""
    return f"{_name(device) or f'joystick {device}'} - button {button}"


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
    ):
        super().__init__(name="joystick", daemon=True)
        self._events = events
        self._is_enabled = is_enabled
        self._device = device
        self._button = button
        self._pressed = False
        self._capture: Callable[[int, int], None] | None = None
        self._stop = threading.Event()
        self._missing_logged = False

    # -- public ----------------------------------------------------------

    # The GUI holds a watcher, not the module, so enumeration has to be
    # reachable from the instance. Shipping these as module functions only is
    # what broke v0.1.4 on startup.
    def list_devices(self) -> list[tuple[int, str, int]]:
        return list_devices()

    def describe(self, device: int, button: int) -> str:
        return describe(device, button)

    def diagnose(self) -> list[str]:
        return diagnose()

    def set_binding(self, device: int | None, button: int | None) -> None:
        self._device, self._button = device, button
        self._pressed = False
        self._missing_logged = False

    def start_capture(self, on_captured: Callable[[int, int], None]) -> None:
        """Report the next button pressed on any device."""
        self._capture = on_captured

    def cancel_capture(self) -> None:
        self._capture = None

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
                elif self._device is not None and self._button is not None:
                    self._poll_binding()
            except Exception:
                # A disconnected wheel must not take the thread down; the
                # trigger would then be silently dead until a restart.
                log.exception("joystick poll failed")
            self._stop.wait(POLL_SECONDS)

    def _poll_binding(self) -> None:
        mask = _mask(self._device)
        if mask is None:
            if not self._missing_logged:
                self._missing_logged = True
                log.warning(
                    "joystick %d is not responding; its button trigger is inactive",
                    self._device,
                )
            return
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
        for device, _label, _count in list_devices():
            mask = _mask(device)
            if not mask:
                continue
            # Lowest set bit wins if several are held, so the result is stable.
            button = (mask & -mask).bit_length()
            callback, self._capture = self._capture, None
            try:
                callback(device, button)
            except Exception:
                log.exception("joystick capture callback failed")
            return

    def _post(self, kind: str) -> None:
        try:
            self._events.put_nowait((kind, time.monotonic()))
        except queue.Full:
            log.warning("trigger queue full; dropped joystick %s", kind)
