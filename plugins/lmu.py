"""Le Mans Ultimate session data.

LMU publishes its state in a shared memory block with no plugin required on
Windows, which is the same interface TinyPedal reads. The struct definitions
come from TinyPedal's `pyLMUSharedMemory` (MIT), vendored under `vendor/`
rather than depended on — getting a field offset wrong would silently yield
garbage names instead of failing, so the layout is worth taking from a
maintained source rather than hand-deriving.

Reading is cheap and lazy: the map is opened on first use and stays open. Every
failure path returns "no drivers", because a game update that moves the layout
must cost a feature, never a trigger.
"""

from __future__ import annotations

import ctypes
import logging
import mmap
import sys
import threading
from pathlib import Path

from plugins.base import SessionPlugin

log = logging.getLogger(__name__)

# Vendored, not installed: keeps it out of the dependency set and out of
# Nuitka's way, since it is pure Python either way.
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


class LeMansUltimatePlugin(SessionPlugin):
    id = "lmu"
    name = "Le Mans Ultimate"
    executables = ("le mans ultimate.exe",)
    description = (
        "Reads the driver list from LMU's shared memory, so names are "
        "transcribed correctly and can be turned into mentions."
    )

    def __init__(self) -> None:
        self._mmap: mmap.mmap | None = None
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
        if self._mmap is not None:
            try:
                self._mmap.close()
            except (BufferError, OSError) as exc:
                log.debug("closing the LMU shared memory failed: %s", exc)
            self._mmap = None

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

        try:
            block = mmap.mmap(
                fileno=0,
                length=ctypes.sizeof(lmu_data.LMUObjectOut),
                tagname=lmu_data.LMUConstants.LMU_SHARED_MEMORY_FILE,
            )
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            # Normal when LMU isn't running, so this is not an error.
            self._fail(f"not published ({exc})")
            return False

        try:
            self._data = lmu_data.LMUObjectOut.from_buffer(block)
        except (ValueError, TypeError) as exc:
            block.close()
            self._fail(f"unexpected layout: {exc}")
            return False

        self._mmap = block
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

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or 'LMU is not running'}"
        names = self.drivers()
        if not names:
            return "connected, but no session is running"
        return f"connected — {len(names)} driver(s) in session"
