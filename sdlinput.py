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

MAX_BUTTONS = 32  # matches the config's range; wheels rarely expose more


def _candidate_paths() -> list[Path]:
    """Where SDL2 might be, most specific first."""
    candidates: list[Path] = []
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

    # Beside the executable, and then whatever the loader can find.
    if sys.platform == "win32":
        candidates.append(Path(sys.executable).parent / "SDL2.dll")
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
                for hint in (HINT_BACKGROUND_EVENTS, HINT_HIDAPI, HINT_HIDAPI_STEAM):
                    lib.SDL_SetHint(hint, b"1")
                if lib.SDL_Init(SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER) != 0:
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

    def list_devices(self) -> list[tuple[int, str, int]]:
        with self._lock:
            if self._lib is None:
                return []
            try:
                self._lib.SDL_JoystickUpdate()
                devices = []
                for index in range(self._lib.SDL_NumJoysticks()):
                    handle = self._handle(index)
                    if handle is None:
                        continue
                    raw = self._lib.SDL_JoystickName(handle) or b""
                    name = raw.decode("utf-8", "replace") or f"joystick {index}"
                    buttons = min(self._lib.SDL_JoystickNumButtons(handle), MAX_BUTTONS)
                    devices.append((index, name, buttons))
                return devices
            except Exception:
                log.exception("SDL joystick enumeration failed")
                return []

    def button_mask(self, index: int) -> int | None:
        """Bitmask of held buttons, or None when the device isn't usable."""
        with self._lock:
            if self._lib is None:
                return None
            try:
                self._lib.SDL_JoystickUpdate()
                handle = self._handle(index)
                if handle is None:
                    return None
                count = min(self._lib.SDL_JoystickNumButtons(handle), MAX_BUTTONS)
                mask = 0
                for button in range(count):
                    if self._lib.SDL_JoystickGetButton(handle, button):
                        mask |= 1 << button
                return mask
            except Exception:
                log.exception("SDL button read failed")
                return None

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
