"""SDL3 joystick backend.

The preferred backend. SDL3 added native drivers for hardware that SDL2 never
covered — the Steam Controller among them — and reads it directly over HIDAPI
rather than depending on the Steam client to present a virtual pad. SDL2
remains as the fallback, and the legacy Windows interface behind that.

Deliberately mirrors `sdlinput.SdlJoysticks` method for method, so
`joystick.py` can hold either one and never ask which. The differences between
the two APIs are real and all of them live in here:

* Devices are addressed by **instance ID**, not by position. SDL2's indices
  shifted whenever anything was plugged in; SDL3 hands out a stable ID per
  connection. `joystick.py` still calls the number an "index" because that is
  all it is to a caller — an opaque handle it passes back.
* `SDL_GetJoysticks` returns a **malloc'd array** that the caller must free.
* `SDL_Init` and `SDL_GetJoystickButton` return **bool**, where the SDL2
  equivalents returned 0-on-success and Uint8. Getting either backwards means
  a backend that reports success and finds nothing.

Every entry point is guarded so a missing or unloadable SDL3 degrades to
"unavailable" and the caller drops to SDL2, rather than taking the app down.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMEPAD = 0x00002000

# Without this SDL discards joystick input whenever its process is not focused.
# PitRadio is unfocused by definition while you are driving.
HINT_BACKGROUND_EVENTS = b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"
HINT_HIDAPI = b"SDL_JOYSTICK_HIDAPI"

# Reading a Steam Controller directly over HIDAPI, instead of through whatever
# Steam presents. Off by default, because it takes the device away from Steam:
# its desktop keyboard/mouse shortcuts stop working while PitRadio is running,
# and the raw device exposes touchpads and grip sensors as buttons that sit
# active or chatter — 20 and 22 on a real one — which is noise nobody asked for.
#
# Seizing a user's controller to read a button is the wrong trade. With this
# off, SDL sees the virtual pad Steam already publishes, which is what every
# other application sees.
HINT_HIDAPI_STEAM = b"SDL_JOYSTICK_HIDAPI_STEAM"
# Not optional, and the least obvious of the four. SDL only re-scans for
# devices when its device-change window receives WM_DEVICECHANGE, and with this
# hint off SDL creates that window on whichever thread called SDL_Init — ours,
# which runs a polling loop and never pumps messages. The result is that
# anything connected after startup is never noticed, for the life of the
# process. With the hint on, SDL runs its own thread and pumps it itself.
HINT_JOYSTICK_THREAD = b"SDL_JOYSTICK_THREAD"

MAX_BUTTONS = 128

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


class SDL_GUID(ctypes.Structure):
    """SDL3's stable per-device identity. Same 16 bytes as SDL2's."""

    _fields_ = [("data", ctypes.c_uint8 * 16)]


def _candidate_paths() -> list[Path]:
    """Where SDL3 might be, most specific first."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        # Beside the executable, where packaging/build.py places it in a
        # frozen build. Checked first because a compiled module's __file__
        # does not reliably point at a real directory.
        candidates.append(Path(sys.executable).parent / "SDL3.dll")
        # Running from source: src/pitradio/input/ -> repository root.
        root = Path(__file__).resolve().parent.parent.parent.parent
        candidates.append(root / "packaging" / "runtime" / "SDL3.dll")
    elif sys.platform == "darwin":
        # Only so the binding can be exercised off Windows.
        candidates.append(Path("/opt/homebrew/lib/libSDL3.dylib"))
        candidates.append(Path("/usr/local/lib/libSDL3.dylib"))
    else:
        candidates.append(Path("/usr/lib/libSDL3.so.0"))
    return candidates


def _load() -> ctypes.CDLL | None:
    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            return ctypes.CDLL(str(path))
        except OSError as exc:
            log.debug("could not load SDL3 from %s: %s", path, exc)

    name = {"win32": "SDL3.dll", "darwin": "libSDL3.dylib"}.get(
        sys.platform, "libSDL3.so.0")
    try:
        return ctypes.CDLL(name)
    except OSError as exc:
        log.debug("SDL3 not found on the library path: %s", exc)
        return None


class Sdl3Joysticks:
    """Opens every attached joystick and reports button state.

    All SDL access is serialised: the GUI enumerates from the Tk thread while
    the watcher polls from its own.
    """

    version = "SDL3"

    #: Whether to take a Steam Controller away from Steam. See
    #: HINT_HIDAPI_STEAM; set from config before start().
    steam_hidapi = False

    def __init__(self) -> None:
        self._lib: ctypes.CDLL | None = None
        self._lock = threading.Lock()
        self._handles: dict[int, ctypes.c_void_p] = {}
        self._failure: str | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Load and initialise SDL3. False means try the next backend."""
        with self._lock:
            if self._started:
                return self._lib is not None

            self._started = True
            lib = _load()
            if lib is None:
                self._failure = "SDL3 library not found"
                return False

            try:
                self._declare(lib)
                for hint in (HINT_BACKGROUND_EVENTS, HINT_HIDAPI,
                             HINT_JOYSTICK_THREAD):
                    lib.SDL_SetHint(hint, b"1")
                lib.SDL_SetHint(HINT_HIDAPI_STEAM,
                                b"1" if self.steam_hidapi else b"0")
                # SDL3 returns true on success, where SDL2 returned 0.
                if not lib.SDL_Init(SDL_INIT_JOYSTICK):
                    self._failure = self._error(lib)
                    return False
            except Exception as exc:
                self._failure = f"{type(exc).__name__}: {exc}"
                return False

            self._lib = lib
            log.info("SDL3 joystick backend ready")
            return True

    @staticmethod
    def _declare(lib: ctypes.CDLL) -> None:
        lib.SDL_SetHint.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
        lib.SDL_SetHint.restype = ctypes.c_bool
        lib.SDL_Init.argtypes = (ctypes.c_uint32,)
        lib.SDL_Init.restype = ctypes.c_bool
        lib.SDL_Quit.argtypes = ()
        lib.SDL_PumpEvents.argtypes = ()
        lib.SDL_GetError.argtypes = ()
        lib.SDL_GetError.restype = ctypes.c_char_p
        lib.SDL_free.argtypes = (ctypes.c_void_p,)
        lib.SDL_UpdateJoysticks.argtypes = ()
        lib.SDL_GetJoysticks.argtypes = (ctypes.POINTER(ctypes.c_int),)
        lib.SDL_GetJoysticks.restype = ctypes.POINTER(ctypes.c_uint32)
        lib.SDL_OpenJoystick.argtypes = (ctypes.c_uint32,)
        lib.SDL_OpenJoystick.restype = ctypes.c_void_p
        lib.SDL_CloseJoystick.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickConnected.argtypes = (ctypes.c_void_p,)
        lib.SDL_JoystickConnected.restype = ctypes.c_bool
        lib.SDL_GetJoystickName.argtypes = (ctypes.c_void_p,)
        lib.SDL_GetJoystickName.restype = ctypes.c_char_p
        lib.SDL_GetJoystickNameForID.argtypes = (ctypes.c_uint32,)
        lib.SDL_GetJoystickNameForID.restype = ctypes.c_char_p
        lib.SDL_GetNumJoystickButtons.argtypes = (ctypes.c_void_p,)
        lib.SDL_GetNumJoystickButtons.restype = ctypes.c_int
        lib.SDL_GetNumJoystickHats.argtypes = (ctypes.c_void_p,)
        lib.SDL_GetNumJoystickHats.restype = ctypes.c_int
        lib.SDL_GetJoystickButton.argtypes = (ctypes.c_void_p, ctypes.c_int)
        lib.SDL_GetJoystickButton.restype = ctypes.c_bool
        lib.SDL_GetJoystickHat.argtypes = (ctypes.c_void_p, ctypes.c_int)
        lib.SDL_GetJoystickHat.restype = ctypes.c_uint8
        lib.SDL_GetJoystickGUID.argtypes = (ctypes.c_void_p,)
        lib.SDL_GetJoystickGUID.restype = SDL_GUID
        lib.SDL_GUIDToString.argtypes = (SDL_GUID, ctypes.c_char_p, ctypes.c_int)

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
                _suppress(lambda h=handle: self._lib.SDL_CloseJoystick(h))
            self._handles.clear()
            _suppress(self._lib.SDL_Quit)
            self._lib = None

    # -- queries ---------------------------------------------------------

    def _instance_ids(self) -> list[int]:
        """Attached devices, as SDL3 instance IDs. Frees SDL's array."""
        count = ctypes.c_int(0)
        array = self._lib.SDL_GetJoysticks(ctypes.byref(count))
        if not array:
            return []
        try:
            return [int(array[i]) for i in range(max(0, count.value))]
        finally:
            self._lib.SDL_free(ctypes.cast(array, ctypes.c_void_p))

    def _handle(self, instance_id: int):
        handle = self._handles.get(instance_id)
        if handle is not None and self._lib.SDL_JoystickConnected(handle):
            return handle
        handle = self._lib.SDL_OpenJoystick(instance_id)
        if not handle:
            return None
        self._handles[instance_id] = handle
        return handle

    def _pump(self) -> None:
        """Refresh device state before reading it.

        `SDL_UpdateJoysticks` alone is documented as sufficient and mostly is. But
        SDL's Windows joystick backends do some of their work off the event
        queue, and this project has already been bitten once by a documented
        "this is enough" call that was not — device *detection* silently
        stopped without a message pump. Pumping costs nothing here: SDL is
        initialised with no video subsystem, so there is no window whose
        thread we would have to be on.
        """
        self._lib.SDL_PumpEvents()
        self._lib.SDL_UpdateJoysticks()

    def list_devices(self) -> list[tuple[int, str, int]]:
        with self._lock:
            if self._lib is None:
                return []
            try:
                self._pump()
                devices = []
                for instance_id in self._instance_ids():
                    handle = self._handle(instance_id)
                    if handle is None:
                        continue
                    raw = self._lib.SDL_GetJoystickName(handle) or b""
                    name = (raw.decode("utf-8", "replace")
                            or f"joystick {instance_id}")
                    count = (self._buttons(handle)
                             + self._hats(handle) * len(HAT_DIRECTIONS))
                    devices.append((instance_id, name, count))
                return devices
            except Exception:
                log.exception("SDL3 joystick enumeration failed")
                return []

    def button_mask(self, instance_id: int) -> int | None:
        """Bitmask of held inputs, buttons first then four bits per POV hat."""
        with self._lock:
            if self._lib is None:
                return None
            try:
                self._pump()
                handle = self._handle(instance_id)
                if handle is None:
                    return None

                mask = 0
                bit = 0
                for button in range(self._buttons(handle)):
                    if self._lib.SDL_GetJoystickButton(handle, button):
                        mask |= 1 << bit
                    bit += 1
                for hat in range(self._hats(handle)):
                    value = self._lib.SDL_GetJoystickHat(handle, hat)
                    for direction, _label in HAT_DIRECTIONS:
                        if value & direction:
                            mask |= 1 << bit
                        bit += 1
                return mask
            except Exception:
                log.exception("SDL3 button read failed")
                return None

    def _buttons(self, handle) -> int:
        return max(0, min(self._lib.SDL_GetNumJoystickButtons(handle), MAX_BUTTONS))

    def _hats(self, handle) -> int:
        return max(0, self._lib.SDL_GetNumJoystickHats(handle))

    def guid(self, instance_id: int) -> str | None:
        with self._lock:
            if self._lib is None:
                return None
            try:
                handle = self._handle(instance_id)
                if handle is None:
                    return None
                buffer = ctypes.create_string_buffer(33)
                self._lib.SDL_GUIDToString(
                    self._lib.SDL_GetJoystickGUID(handle), buffer, len(buffer))
                text = buffer.value.decode("ascii", "replace")
                # All zeroes means SDL had no identity for it, which is no more
                # use for matching than nothing at all.
                return text if text.strip("0") else None
            except Exception:
                log.exception("SDL3 GUID read failed")
                return None

    def label(self, instance_id: int, button: int) -> str:
        """'button 13' or 'POV up' for a 1-based flat button number."""
        with self._lock:
            if self._lib is None:
                return f"button {button}"
            try:
                handle = self._handle(instance_id)
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
                log.exception("SDL3 button label failed")
                return f"button {button}"

    def name(self, instance_id: int) -> str | None:
        for device, label, _count in self.list_devices():
            if device == instance_id:
                return label
        return None


def _suppress(fn) -> None:
    try:
        fn()
    except Exception:
        log.debug("SDL3 teardown call failed", exc_info=True)
