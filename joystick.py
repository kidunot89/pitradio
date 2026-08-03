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

import winapi

log = logging.getLogger(__name__)

TRIGGER_DOWN = "down"
TRIGGER_UP = "up"

POLL_SECONDS = 0.012
MAX_DEVICES = 16


def list_devices() -> list[tuple[int, str, int]]:
    """(device id, name, button count) for every attached joystick."""
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
    lines = [f"joyGetNumDevs reports {winapi.joystick_count()} supported device slots"]

    found = 0
    for device in range(min(winapi.joystick_count(), MAX_DEVICES)):
        name = winapi.joystick_name(device)
        if name is None:
            continue
        mask = winapi.joystick_button_mask(device)
        buttons = winapi.joystick_buttons(device)
        if mask is None:
            lines.append(f"  slot {device}: {name!r} — known to the driver but not connected")
        else:
            found += 1
            lines.append(f"  slot {device}: {name!r} — {buttons} buttons, state readable")

    if not found:
        lines.append(
            "  no usable devices. If a controller is plugged in, it is most likely "
            "held by Steam Input, which hides it from this interface — try "
            "disabling Steam Input for the device, or use a keyboard trigger."
        )
    return lines


def describe(device: int, button: int) -> str:
    """Human-readable binding, e.g. 'Fanatec CSL Elite - button 13'."""
    name = winapi.joystick_name(device) or f"joystick {device}"
    return f"{name} - button {button}"


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
        mask = winapi.joystick_button_mask(self._device)
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
        for device, _name, _count in list_devices():
            mask = winapi.joystick_button_mask(device)
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
