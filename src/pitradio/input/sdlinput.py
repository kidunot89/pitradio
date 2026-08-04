"""SDL2 joystick backend.

Windows' legacy joystick interface cannot see everything. A Steam Controller is
invisible to it entirely — in desktop mode it presents as a keyboard and mouse,
and Steam Input re-presents it in a form the legacy API never enumerates. SDL2
has explicit HIDAPI drivers for those devices, so it sees what the legacy path
misses.

This is a hand-written ctypes binding over the handful of joystick calls needed,
rather than PySDL2. The same reasoning as `winapi.py`: a small explicit surface,
no import machinery to bundle, and only a DLL to carry. Every entry point is
guarded so that a missing or unloadable SDL2 degrades to "unavailable" and the
caller falls back to the legacy interface, rather than taking the app down —
which is the failure mode this project has paid for repeatedly.

Deliberately does not use SDL's event queue. SDL_PumpEvents must run on the
main thread, which belongs to tkinter; SDL_JoystickUpdate can be called from
our polling thread and is all that state polling needs.

That choice is also what makes the trigger work globally. Polling device state
asks the driver directly and does not care which window has focus — the thing
that *would* care is SDL's event queue, and we never touch it. SDL is
initialised without a video subsystem for the same reason: with no window,
there is no focus for SDL to gate input on.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000

# Without this, SDL discards joystick input whenever its process isn't focused.
# PitRadio is unfocused by definition while you're driving, so this is not
# optional — it is the difference between working and never firing.
HINT_BACKGROUND_EVENTS = b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"
# The HIDAPI drivers are what see a Steam Controller at all.
HINT_HIDAPI = b"SDL_JOYSTICK_HIDAPI"
HINT_HIDAPI_STEAM = b"SDL_JOYSTICK_HIDAPI_STEAM"

MAX_BUTTONS = 128  # button boxes and wheel rims routinely pass 32

# POV hat directions. A hat is not a button as far as SDL is concerned, but it
# is one as far as a driver is concerned: the D-pad on a wheel rim is a hat, and
# a binding UI that cannot see it looks broken to the person pressing it.
SDL_HAT_UP = 0x01
SDL_HAT_RIGHT = 0x02
SDL_HAT_DOWN = 0x04
SDL_HAT_LEFT = 0x08
HAT_DIRECTIONS = (
    (SDL_HAT_UP, "up"),
    (SDL_HAT_RIGHT, "right"),
    (SDL_HAT_DOWN, "down"),
    (SDL_HAT_LEFT, "left"),
)


class SDL_JoystickGUID(ctypes.Structure):
    """SDL's stable per-device identity — vendor, product and version.

    This is what a binding must be stored against. SDL device indices are
    positional: unplug a pedal set, or start Steam, and every index after it
    shifts. A binding recorded against index 2 then silently points at whatever
    is now second, which is indistinguishable from the trigger being broken.
    """

    _fields_ = [("data", ctypes.c_uint8 * 16)]


def _candidate_paths() -> list[Path]:
    """Where SDL2 might be, most specific first."""
    candidates: list[Path] = []

    # Beside the executable: where packaging/build.py places it. Checked first
    # because a compiled sdl2dll's __file__ does not reliably point at a real
    # directory, which is how a build shipped with SDL2 unloadable.
    if sys.platform == "win32":
        candidates.append(Path(sys.executable).parent / "SDL2.dll")

    try:
        import sdl2dll

        root = Path(sdl2dll.get_dllpath())
        if sys.platform == "win32":
            candidates.append(root / "SDL2.dll")
        elif sys.platform == "darwin":
            # Only so this binding can be exercised off Windows.
            candidates.append(root / "SDL2.framework" / "Versions" / "A" / "SDL2")
        else:
            candidates.append(root / "libSDL2-2.0.so.0")
    except Exception as exc:
        log.debug("sdl2dll unavailable: %s", exc)

    return candidates


def _load() -> ctypes.CDLL | None:
    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            return ctypes.CDLL(str(path))
        except OSError as exc:
            log.debug("could not load SDL2 from %s: %s", path, exc)

    # Last resort: let the OS loader resolve it from the search path.
    name = {"win32": "SDL2.dll", "darwin": "libSDL2-2.0.0.dylib"}.get(
        sys.platform, "libSDL2-2.0.so.0")
    try:
        return ctypes.CDLL(name)
    except OSError as exc:
        log.debug("SDL2 not found on the library path: %s", exc)
        return None


class SdlJoysticks:
    """Opens every attached joystick and reports button state.

    All SDL access is serialised: the GUI enumerates from the Tk thread while
    the watcher polls from its own, and SDL's guarantees here are not worth
    relying on.
    """

    version = "SDL2"

    #: Whether to take a Steam Controller away from Steam.
    steam_hidapi = False

    def __init__(self) -> None:
        self._lib: ctypes.CDLL | None = None
        self._lock = threading.Lock()
        self._handles: dict[int, ctypes.c_void_p] = {}
        self._failure: str | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Load and initialise SDL. False means fall back to the legacy API."""
        with self._lock:
            if self._started:
                return self._lib is not None

            self._started = True
            lib = _load()
            if lib is None:
                self._failure = "SDL2 library not found"
                return False

            try:
                self._declare(lib)
                for hint in (HINT_BACKGROUND_EVENTS, HINT_HIDAPI):
                    lib.SDL_SetHint(hint, b"1")
                # See sdl3input.HINT_HIDAPI_STEAM: taking the controller away
                # from Steam breaks its desktop shortcuts and surfaces sensor
                # "buttons" nobody wants.
                lib.SDL_SetHint(HINT_HIDAPI_STEAM,
                                b"1" if self.steam_hidapi else b"0")
                if lib.SDL_Init(SDL_INIT_JOYSTICK) != 0:
                    self._failure = self._error(lib)
                    return False
            except Exception as exc:
                self._failure = f"{type(exc).__name__}: {exc}"
                return False

            self._lib = lib
            log.info("SDL2 joystick backend ready")
            return True

    @staticmethod
    def _declare(lib: ctypes.CDLL) -> None:
        lib.SDL_SetHint.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
        lib.SDL_SetHint.restype = ctypes.c_int
        lib.SDL_Init.argtypes = (ctypes.c_uint32,)
        lib.SDL_Init.restype = ctypes.c_int
        lib.SDL_Quit.argtypes = ()
        lib.SDL_GetError.argtypes = ()
        lib.SDL_GetError.restype = ctypes.c_char_p
        lib.SDL_JoystickUpdate.argtypes = ()
        lib.SDL_NumJoysticks.argtypes = ()
        lib.SDL_NumJoysticks.restype = ctypes.c_int
        lib.SDL_JoystickOpen.argtypes = (ctypes.c_int,)
        lib.SDL_JoystickOpen.restype = ctypes.c_void_p
        lib.SDL_JoystickClose.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickName.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickName.restype = ctypes.c_char_p
        lib.SDL_JoystickNumButtons.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickNumButtons.restype = ctypes.c_int
        lib.SDL_JoystickGetButton.argtypes = (ctypes.c_void_p, ctypes.c_int)
        lib.SDL_JoystickGetButton.restype = ctypes.c_uint8
        lib.SDL_JoystickGetAttached.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickGetAttached.restype = ctypes.c_int
        lib.SDL_JoystickNumHats.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickNumHats.restype = ctypes.c_int
        lib.SDL_JoystickGetHat.argtypes = (ctypes.c_void_p, ctypes.c_int)
        lib.SDL_JoystickGetHat.restype = ctypes.c_uint8
        lib.SDL_JoystickGetGUID.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickGetGUID.restype = SDL_JoystickGUID
        lib.SDL_JoystickGetGUIDString.argtypes = (
            SDL_JoystickGUID, ctypes.c_char_p, ctypes.c_int)

    @staticmethod
    def _error(lib: ctypes.CDLL) -> str:
        try:
            return (lib.SDL_GetError() or b"").decode("utf-8", "replace")
        except Exception:
            return "unknown SDL error"

    @property
    def available(self) -> bool:
        return self._lib is not None

    @property
    def failure(self) -> str | None:
        return self._failure

    def stop(self) -> None:
        with self._lock:
            if self._lib is None:
                return
            for handle in self._handles.values():
                with_suppressed(lambda h=handle: self._lib.SDL_JoystickClose(h))
            self._handles.clear()
            with_suppressed(self._lib.SDL_Quit)
            self._lib = None

    # -- queries ---------------------------------------------------------

    def _handle(self, index: int):
        handle = self._handles.get(index)
        if handle is not None and self._lib.SDL_JoystickGetAttached(handle):
            return handle
        handle = self._lib.SDL_JoystickOpen(index)
        if not handle:
            return None
        self._handles[index] = handle
        return handle

    def _pump(self) -> None:
        """Refresh device state before reading it.

        `SDL_JoystickUpdate` alone is documented as sufficient and mostly is. But
        SDL's Windows joystick backends do some of their work off the event
        queue, and this project has already been bitten once by a documented
        "this is enough" call that was not — device *detection* silently
        stopped without a message pump. Pumping costs nothing here: SDL is
        initialised with no video subsystem, so there is no window whose
        thread we would have to be on.
        """
        self._lib.SDL_PumpEvents()
        self._lib.SDL_JoystickUpdate()

    def list_devices(self) -> list[tuple[int, str, int]]:
        with self._lock:
            if self._lib is None:
                return []
            try:
                self._pump()
                devices = []
                for index in range(self._lib.SDL_NumJoysticks()):
                    handle = self._handle(index)
                    if handle is None:
                        continue
                    raw = self._lib.SDL_JoystickName(handle) or b""
                    name = raw.decode("utf-8", "replace") or f"joystick {index}"
                    # Hats are bindable too, so count them as inputs — the
                    # number here is what the binding UI offers.
                    count = self._buttons(handle) + self._hats(handle) * len(HAT_DIRECTIONS)
                    devices.append((index, name, count))
                return devices
            except Exception:
                log.exception("SDL joystick enumeration failed")
                return []

    def button_mask(self, index: int) -> int | None:
        """Bitmask of held inputs, or None when the device isn't usable.

        Physical buttons occupy the low bits, then four bits per POV hat. Hats
        are folded in rather than exposed separately so that everything above
        this layer — binding, capture, config, the GUI — deals in one flat
        button number, which is also how a wheel labels them.
        """
        with self._lock:
            if self._lib is None:
                return None
            try:
                self._pump()
                handle = self._handle(index)
                if handle is None:
                    return None

                mask = 0
                bit = 0
                for button in range(self._buttons(handle)):
                    if self._lib.SDL_JoystickGetButton(handle, button):
                        mask |= 1 << bit
                    bit += 1

                for hat in range(self._hats(handle)):
                    value = self._lib.SDL_JoystickGetHat(handle, hat)
                    for direction, _label in HAT_DIRECTIONS:
                        if value & direction:
                            mask |= 1 << bit
                        bit += 1
                return mask
            except Exception:
                log.exception("SDL button read failed")
                return None

    def _buttons(self, handle) -> int:
        return max(0, min(self._lib.SDL_JoystickNumButtons(handle), MAX_BUTTONS))

    def _hats(self, handle) -> int:
        return max(0, self._lib.SDL_JoystickNumHats(handle))

    def guid(self, index: int) -> str | None:
        """Stable identity for the device at `index`, as a 32-character hex string."""
        with self._lock:
            if self._lib is None:
                return None
            try:
                handle = self._handle(index)
                if handle is None:
                    return None
                buffer = ctypes.create_string_buffer(33)
                self._lib.SDL_JoystickGetGUIDString(
                    self._lib.SDL_JoystickGetGUID(handle), buffer, len(buffer))
                text = buffer.value.decode("ascii", "replace")
                # All zeroes means SDL had no identity for it, which is no more
                # useful for matching than nothing at all.
                return text if text.strip("0") else None
            except Exception:
                log.exception("SDL GUID read failed")
                return None

    def label(self, index: int, button: int) -> str:
        """'button 13' or 'POV up' for a 1-based flat button number."""
        with self._lock:
            if self._lib is None:
                return f"button {button}"
            try:
                handle = self._handle(index)
                if handle is None:
                    return f"button {button}"
                buttons = self._buttons(handle)
                if button <= buttons:
                    return f"button {button}"
                offset = button - buttons - 1
                hat, direction = divmod(offset, len(HAT_DIRECTIONS))
                if hat >= self._hats(handle):
                    return f"button {button}"
                name = HAT_DIRECTIONS[direction][1]
                return f"POV {name}" if hat == 0 else f"POV {hat + 1} {name}"
            except Exception:
                log.exception("SDL button label failed")
                return f"button {button}"

    def name(self, index: int) -> str | None:
        for device, label, _count in self.list_devices():
            if device == index:
                return label
        return None


def with_suppressed(fn) -> None:
    try:
        fn()
    except Exception:
        log.debug("SDL teardown call failed", exc_info=True)
