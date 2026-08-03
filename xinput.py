"""XInput backend.

The narrowest of the backends and the most reliable within its range: exactly
four controller slots, a fixed 14-button layout, and no enumeration to get
wrong. Windows keeps XInput working for anything that presents as an Xbox pad,
which includes most wireless controllers and anything a wrapper such as Steam
Input or DS4Windows re-presents as one.

It is last in the preference order rather than first because of what it cannot
do: no wheels, no button boxes, no device names, no stable identity beyond the
slot number, and a hard limit of four devices. A rim with 20 buttons appears
here as nothing at all. It exists to catch pads the SDL backends miss.

Mirrors the SDL backends method for method so `joystick.py` can hold any of
them without asking which.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger(__name__)

ERROR_SUCCESS = 0
XUSER_MAX_COUNT = 4

# Newest first. 1_4 ships with Windows 8 and later, 9_1_0 is the redistributable
# floor, and 1_3 comes from the old DirectX runtime that many games install.
LIBRARIES = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")

# Bit -> label, in the order they are packed into wButtons. Reported as 1-based
# button numbers so they read the same way as every other backend's.
BUTTONS = (
    (0x0001, "D-pad up"),
    (0x0002, "D-pad down"),
    (0x0004, "D-pad left"),
    (0x0008, "D-pad right"),
    (0x0010, "Start"),
    (0x0020, "Back"),
    (0x0040, "Left stick"),
    (0x0080, "Right stick"),
    (0x0100, "Left bumper"),
    (0x0200, "Right bumper"),
    (0x1000, "A"),
    (0x2000, "B"),
    (0x4000, "X"),
    (0x8000, "Y"),
)


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


def _load() -> ctypes.WinDLL | None:
    for name in LIBRARIES:
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    return None


class XInputPads:
    """The four XInput slots, presented as devices."""

    version = "XInput"

    def __init__(self) -> None:
        self._lib = None
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return self._lib is not None

            self._started = True
            if not hasattr(ctypes, "WinDLL"):
                self._failure = "not Windows"
                return False

            lib = _load()
            if lib is None:
                self._failure = f"none of {', '.join(LIBRARIES)} could be loaded"
                return False

            try:
                lib.XInputGetState.argtypes = (
                    wintypes.DWORD, ctypes.POINTER(XINPUT_STATE))
                lib.XInputGetState.restype = wintypes.DWORD
            except Exception as exc:
                self._failure = f"{type(exc).__name__}: {exc}"
                return False

            self._lib = lib
            log.info("XInput backend ready")
            return True

    @property
    def available(self) -> bool:
        return self._lib is not None

    @property
    def failure(self) -> str | None:
        return self._failure

    def stop(self) -> None:
        with self._lock:
            self._lib = None

    # -- queries ---------------------------------------------------------

    def _state(self, slot: int) -> XINPUT_STATE | None:
        state = XINPUT_STATE()
        if self._lib.XInputGetState(slot, ctypes.byref(state)) != ERROR_SUCCESS:
            return None
        return state

    def list_devices(self) -> list[tuple[int, str, int]]:
        with self._lock:
            if self._lib is None:
                return []
            devices = []
            for slot in range(XUSER_MAX_COUNT):
                try:
                    if self._state(slot) is None:
                        continue
                except Exception:
                    log.exception("XInput enumeration failed")
                    return devices
                devices.append((slot, f"XInput controller {slot + 1}", len(BUTTONS)))
            return devices

    def button_mask(self, slot: int) -> int | None:
        with self._lock:
            if self._lib is None:
                return None
            try:
                state = self._state(slot)
            except Exception:
                log.exception("XInput read failed")
                return None
            if state is None:
                return None

            # Repacked into contiguous bits: wButtons has gaps at 0x0400 and
            # 0x0800, and a binding stored against a raw bit index would leave
            # button numbers that skip.
            mask = 0
            for index, (bit, _label) in enumerate(BUTTONS):
                if state.Gamepad.wButtons & bit:
                    mask |= 1 << index
            return mask

    def guid(self, slot: int) -> str | None:
        """XInput has no device identity, so the slot has to stand in.

        Weaker than a real GUID: unplug two pads and plug them back in the
        other order and the bindings swap. There is nothing better available —
        XInput deliberately exposes no device information at all.
        """
        return f"xinput:{slot}"

    def label(self, slot: int, button: int) -> str:
        if 1 <= button <= len(BUTTONS):
            return BUTTONS[button - 1][1]
        return f"button {button}"

    def name(self, slot: int) -> str | None:
        for device, label, _count in self.list_devices():
            if device == slot:
                return label
        return None
