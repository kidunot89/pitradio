"""Le Mans Ultimate session data.

LMU publishes its state in a shared memory block with no plugin required on
Windows, which is the same interface TinyPedal reads. The struct definitions
come from TinyPedal's `pyLMUSharedMemory` (MIT), vendored under `vendor/`
rather than depended on — getting a field offset wrong would silently yield
garbage names instead of failing, so the layout is worth taking from a
maintained source rather than hand-deriving.

The block is *opened*, never created — see _open_existing_mapping for why that
distinction matters. Connection is lazy, since the sim is usually not running
when PitRadio starts. Every failure path returns "no drivers", because a game
update that moves the layout must cost a feature, never a trigger.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path

from plugins.base import PluginSetting, SessionPlugin

log = logging.getLogger(__name__)

# Vendored, not installed: keeps it out of the dependency set and out of
# Nuitka's way, since it is pure Python either way.
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


# Read-only view of an existing mapping.
FILE_MAP_READ = 0x0004


def _open_existing_mapping(name: str, size: int):
    """Open a shared memory block only if something else already published it.

    Deliberately not mmap.mmap(fileno=0, tagname=...): on Windows that calls
    CreateFileMapping, which *creates* the block when it is absent. With LMU
    closed that fabricated a page-file-backed block named LMU_Data full of
    zeros, so the plugin reported itself connected to a session that did not
    exist — and left a phantom mapping under the game's own name.

    OpenFileMappingW only ever opens; it fails when the game is not running,
    which is the answer we actually want.
    """
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenFileMappingW.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_size_t)
    kernel32.MapViewOfFile.restype = ctypes.c_void_p

    handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not handle:
        return None

    view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
    if not view:
        kernel32.CloseHandle(handle)
        return None
    return handle, view


def _close_mapping(handle, view) -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if view:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(view))
        if handle:
            kernel32.CloseHandle(handle)
    except Exception:
        log.debug("releasing the LMU mapping failed", exc_info=True)


class LeMansUltimatePlugin(SessionPlugin):
    id = "lmu"
    name = "Le Mans Ultimate"
    executables = ("le mans ultimate.exe",)
    description = (
        "Reads the driver list from LMU's shared memory, so names are "
        "transcribed correctly and can be turned into mentions."
    )
    settings = (
        PluginSetting(
            key="positions",
            label="Recognise standings positions",
            kind="bool",
            default=True,
            help=('say "P3" and it sends that driver\'s name — useful when you '
                  "cannot pronounce it or did not catch it"),
        ),
    )

    def __init__(self) -> None:
        self._handle = None
        self._view = None
        self._data = None
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._logged_failure = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Deliberately does nothing.

        The sim is usually not running when PitRadio starts, so connecting is
        left until something actually asks for data.
        """

    def stop(self) -> None:
        with self._lock:
            self._release()

    def _release(self) -> None:
        self._data = None
        if self._handle or self._view:
            _close_mapping(self._handle, self._view)
        self._handle = None
        self._view = None

    # -- connection ------------------------------------------------------

    def _connect(self) -> bool:
        """Attach to the shared memory. False when the sim isn't publishing."""
        if self._data is not None:
            return True

        # Named shared memory is a Windows concept; mmap has no `tagname`
        # elsewhere. Said plainly rather than left to raise a confusing
        # TypeError, so the struct definitions stay importable for tests.
        if sys.platform != "win32":
            self._fail("LMU shared memory is only available on Windows")
            return False

        try:
            from pylmusharedmemory import lmu_data
        except ImportError as exc:
            self._fail(f"shared memory definitions unavailable: {exc}")
            return False

        size = ctypes.sizeof(lmu_data.LMUObjectOut)
        name = lmu_data.LMUConstants.LMU_SHARED_MEMORY_FILE

        opened = _open_existing_mapping(name, size)
        if opened is None:
            self._fail("LMU is not running (no shared memory published)")
            return False

        handle, view = opened
        try:
            self._data = ctypes.cast(
                view, ctypes.POINTER(lmu_data.LMUObjectOut)).contents
        except (ValueError, TypeError) as exc:
            _close_mapping(handle, view)
            self._fail(f"unexpected layout: {exc}")
            return False

        self._handle, self._view = handle, view
        self._failure = None
        self._logged_failure = False
        log.info("connected to Le Mans Ultimate shared memory")
        return True

    def _fail(self, reason: str) -> None:
        self._failure = reason
        if not self._logged_failure:
            self._logged_failure = True
            log.info("Le Mans Ultimate data unavailable: %s", reason)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connect()

    # -- data ------------------------------------------------------------

    def drivers(self) -> list[str]:
        with self._lock:
            if not self._connect():
                return []
            try:
                scoring = self._data.scoring
                count = int(scoring.scoringInfo.mNumVehicles)
            except (AttributeError, ValueError) as exc:
                self._fail(f"could not read the scoring block: {exc}")
                self._release()
                return []

            # A stale or unpublished block can hold nonsense; clamp rather than
            # trusting it, so a bad read cannot walk off the end of the array.
            count = max(0, min(count, len(scoring.vehScoringInfo)))

            names = []
            for index in range(count):
                raw = scoring.vehScoringInfo[index].mDriverName
                name = raw.decode("utf-8", "replace").strip("\x00").strip()
                if name:
                    names.append(name)
            return names

    def positions(self) -> dict[int, str]:
        """Place -> driver name, from the same scoring block as the names."""
        with self._lock:
            if not self._connect():
                return {}
            try:
                scoring = self._data.scoring
                count = int(scoring.scoringInfo.mNumVehicles)
            except (AttributeError, ValueError):
                return {}

            count = max(0, min(count, len(scoring.vehScoringInfo)))
            standings: dict[int, str] = {}
            for index in range(count):
                vehicle = scoring.vehScoringInfo[index]
                name = vehicle.mDriverName.decode("utf-8", "replace").strip("\x00").strip()
                place = int(vehicle.mPlace)
                # Place 0 means unclassified, not "leader".
                if name and place > 0:
                    standings[place] = name
            return standings

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or 'LMU is not running'}"
        names = self.drivers()
        if not names:
            return "connected, but no session is running"
        return f"connected — {len(names)} driver(s) in session"
