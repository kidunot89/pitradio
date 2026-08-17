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
import json
import logging
import sys
import threading
import time
import urllib.request
from pathlib import Path

from pitradio import voice
from pitradio.plugins import shared_memory
from pitradio.plugins.base import (
    PROVIDES_LAPS,
    PROVIDES_POSITIONS,
    PROVIDES_SECTORS,
    Car,
    PluginSetting,
    SessionInfo,
    SessionPlugin,
    Standings,
)

log = logging.getLogger(__name__)

# Vendored, not installed: keeps it out of the dependency set and out of
# Nuitka's way, since it is pure Python either way.
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


# Read-only view of an existing mapping.
FILE_MAP_READ = 0x0004

#: Roughly the car behind and the one you are catching. Named here rather than
#: written into the setting so the number has somewhere to be explained.
DEFAULT_PROXIMITY_METRES = 200

#: The spotter's window, per sim, because cars are not the same length
#: everywhere. A Hypercar is about 5m, so this is overlapping bodywork plus a
#: little either way.
DEFAULT_ALONGSIDE_METRES = 9
#: How far to the side still counts as beside you rather than on another part
#: of the circuit.
DEFAULT_WIDTH_METRES = 12

# -- which car is on screen ----------------------------------------------
#
# The shared memory block does not say, and the obvious candidates do not
# either — all checked against a live spectated session rather than assumed:
#
#   * `telemetry.playerVehicleIdx` is the *player's* vehicle and stays pointed
#     at their parked car while they watch somebody else.
#   * `appInfo.mOptionsLocation` reads 0 throughout.
#   * `$rFactor2SMMP_Graphics$` is published but never populated — the whole
#     buffer is zeros apart from its version counter, because LMU does not call
#     the graphics callback the rF2 plugin fills it from.
#
# LMU's own overlays read this instead: a local HTTP API, part of the game
# rather than any plugin, whose standings carry `hasFocus` on the car being
# watched. `slotID` there is the same number as `mID` in shared memory.

LMU_STANDINGS_URL = "http://127.0.0.1:6397/rest/watch/standings"

#: Short, because this runs on the trigger cycle. The game is on the same
#: machine; if it has not answered in this long it is busy loading and the
#: answer would be stale anyway.
FOCUS_TIMEOUT_SECONDS = 0.4

#: The camera does not move between cars every frame, and the response is ~16KB.
FOCUS_CACHE_SECONDS = 1.0


def _fetch_standings(timeout: float):
    """The game's own standings, as it hands them to its overlays.

    A seam, so the focus logic can be tested without a running game.
    """
    with urllib.request.urlopen(LMU_STANDINGS_URL, timeout=timeout) as response:
        return json.load(response)


def focus_slot(standings) -> int | None:
    """The slot of the car being watched, or None.

    Pure, because it is the part worth testing. `hasFocus` is what LMU's own
    overlays key on; `focus` sits beside it and has always agreed, so either
    will do and neither is worth preferring on a guess.
    """
    if not isinstance(standings, list):
        return None
    for entry in standings:
        if not isinstance(entry, dict):
            continue
        if entry.get("hasFocus") or entry.get("focus"):
            slot = entry.get("slotID")
            # Slot 0 is a real slot; None is not.
            return int(slot) if isinstance(slot, int) else None
    return None


def _open_existing_mapping(name: str, size: int):
    """Open a shared memory block only if something else already published it.

    The implementation moved to `shared_memory` the moment a second sim needed
    it, along with the explanation of why it must never be `mmap.mmap` — a rule
    that is silent when broken, and so is exactly the wrong thing to have two
    copies of.
    """
    return shared_memory.open_existing(name, size)


def _text(raw: bytes) -> str:
    """A fixed-width field from the block as a string.

    `replace` rather than `strict`: a mangled byte in somebody's name must cost
    that character, not the whole driver list.
    """
    return raw.decode("utf-8", "replace").strip("\x00").strip()


def _speed(local_velocity) -> float:
    """How fast a car is going, in metres per second.

    The block publishes velocity in the car's own axes rather than a scalar, so
    speed is the length of that vector. Taking the longitudinal component alone
    would read low through a slide, which is exactly where somebody is losing
    the time a coaching routine is meant to find.
    """
    try:
        return float((local_velocity.x ** 2 + local_velocity.y ** 2
                      + local_velocity.z ** 2) ** 0.5)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _close_mapping(handle, view) -> None:
    shared_memory.close(handle, view)


class LeMansUltimatePlugin(SessionPlugin):
    id = "lmu"
    name = "Le Mans Ultimate"
    executables = ("le mans ultimate.exe",)
    description = (
        "Reads the driver list, lap and sector times and every car's position "
        "from LMU's shared memory — names for mentions, and everything the "
        "engineer needs."
    )
    #: All of it. The block carries every car's world position, lap and sector
    #: data, which is why this is the sim the engineer was built against.
    provides = frozenset({
        PROVIDES_POSITIONS, PROVIDES_LAPS, PROVIDES_SECTORS,
    })
    settings = (
        PluginSetting(
            key="positions",
            label="Recognise standings positions",
            kind="bool",
            default=True,
            help=('say "P3" and it sends that driver\'s name, or "GT3 P3" for '
                  "the one in that class — useful when you cannot pronounce "
                  "it or did not catch it"),
        ),
        PluginSetting(
            key="proximity_only",
            label="Proximity voice only",
            kind="bool",
            default=False,
            help=("only hear racers near you on track. Off, you hear the whole "
                  "session — which is what you want in practice and on a "
                  "formation lap"),
        ),
        PluginSetting(
            key="proximity_metres",
            label="Proximity range (metres)",
            kind="int",
            default=DEFAULT_PROXIMITY_METRES,
            help=("how near counts. 200m is roughly the car behind you and the "
                  "one you are catching; a lap of Daytona is 5,700m"),
        ),
        PluginSetting(
            key="spotter_swap_sides",
            label="Swap spotter sides",
            kind="bool",
            default=False,
            help=("turn this on if the engineer's spotter says \"left\" for a "
                  "car on your right. Which side is which depends on the sim's "
                  "own axes, and that could not be checked without driving"),
        ),
        PluginSetting(
            key="spotter_metres",
            label="Spotter overlap (metres)",
            kind="int",
            default=DEFAULT_ALONGSIDE_METRES,
            help=("how far apart along the track two cars can be and still "
                  "count as alongside. A Hypercar is about 5m long, so 9m is "
                  "overlapping bodywork plus a little"),
        ),
        PluginSetting(
            key="spotter_width_metres",
            label="Spotter width (metres)",
            kind="int",
            default=DEFAULT_WIDTH_METRES,
            help=("how far to the side still counts. Beyond this they are on "
                  "another part of the circuit — an adjacent straight, or the "
                  "far side of a hairpin"),
        ),
    )

    def __init__(self) -> None:
        self._handle = None
        self._view = None
        self._data = None
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._logged_failure = False
        #: (when it was read, the slot) — see _focus_slot.
        self._focus: tuple[float, int | None] = (float("-inf"), None)

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
        return [car.driver for car in self._cars()]

    def classes(self) -> list[str]:
        """The car classes on this grid, in the order the block lists them.

        Worth having on its own: they go into Whisper's vocabulary, and a class
        Whisper has never heard of comes back as something else entirely — at
        which point "GT3 P3" cannot resolve no matter how good the matching is.
        """
        return list(dict.fromkeys(car.vehicle_class for car in self._cars()
                                  if car.vehicle_class))

    def vocabulary(self) -> list[str]:
        """Driver names, with the class names in front of them.

        In front because the hint is capped: a 60-car entry list would push the
        three words that make "GT3 P3" work off the end of it.
        """
        return self.classes() + self.drivers()

    def standings(self) -> Standings:
        """The overall order and the order within each class.

        **In-class places are derived, not read.** The scoring block carries an
        overall `mPlace` and a class name per car and nothing that combines
        them, so class order is the class's members sorted on their overall
        place. That is the same thing the timing screen shows, and it holds
        whatever LMU decides to call its classes.
        """
        cars = [car for car in self._cars() if car.place > 0]
        if not cars:
            return Standings()

        overall = {car.place: car.driver for car in cars}

        by_class: dict[str, dict[int, str]] = {}
        for name in dict.fromkeys(car.vehicle_class for car in cars if car.vehicle_class):
            members = sorted(
                (car.place, car.driver) for car in cars if car.vehicle_class == name)
            by_class[name] = {
                rank: driver for rank, (_place, driver) in enumerate(members, 1)}
        return Standings(overall, by_class)

    def session(self) -> SessionInfo:
        """The room this session belongs to, and where everybody is.

        The key comes from the *game server*, not the track: an event that moves
        to the next circuit is still the same set of people, and a room that
        dissolved underneath them would be a worse room. No server — offline, or
        single player — means no key and no room.
        """
        cars = self._cars()
        with self._lock:
            if self._data is None:
                return SessionInfo()
            try:
                info = self._data.scoring.scoringInfo
                address = int(info.mServerPublicIP)
                port = int(info.mServerPort)
                track = _text(info.mTrackName)
                # The lap length and the session clock, read in the same lock
                # as the cars above. The engineer compares one car's position
                # against another's, and two reads would be two moments.
                track_length = max(0.0, float(info.mLapDist))
                elapsed = float(info.mCurrentET)
            except (AttributeError, ValueError) as exc:
                self._fail(f"could not read the session block: {exc}")
                return SessionInfo()

        return SessionInfo(
            key=voice.session_key(address, port), track=track, cars=tuple(cars),
            focus_slot=self._focus_slot(), track_length=track_length,
            elapsed=elapsed)

    def _focus_slot(self) -> int | None:
        """Which car the camera is on, from the game's own HTTP API.

        Cached and short-timeout, because it is called from the trigger cycle
        and a dictation app must never wait on a game that is mid-load. Every
        failure is None, which `listener` reads as "cannot tell" and therefore
        as audible — the sim being closed, the API having moved, a request
        timing out: all normal, none of them worth a silent radio.
        """
        now = time.monotonic()
        cached_at, slot = self._focus
        if now - cached_at < FOCUS_CACHE_SECONDS:
            return slot

        try:
            found = focus_slot(_fetch_standings(FOCUS_TIMEOUT_SECONDS))
        except Exception:
            # Cached as a miss too, so a game that is not answering is asked
            # once a second rather than on every single trigger.
            log.debug("could not read the focused car", exc_info=True)
            found = None

        self._focus = (now, found)
        return found

    def _cars(self) -> list[Car]:
        """Every scored car, from one read of the block.

        Everything that depends on where people are — the overall order, the
        class orders, proximity — comes through here, so they can never disagree
        about which frame they came from. The block updates many times a second
        and reading it twice is reading two different races.
        """
        with self._lock:
            if not self._connect():
                return []
            try:
                scoring = self._data.scoring
                count = int(scoring.scoringInfo.mNumVehicles)
            except (AttributeError, ValueError):
                return []

            count = max(0, min(count, len(scoring.vehScoringInfo)))
            cars: list[Car] = []
            for index in range(count):
                vehicle = scoring.vehScoringInfo[index]
                name = _text(vehicle.mDriverName)
                if not name:
                    continue
                cars.append(Car(
                    slot=int(vehicle.mID),
                    driver=name,
                    # Place 0 means unclassified, not "leader"; kept as 0 so
                    # the car still exists for proximity, which does not care.
                    place=max(0, int(vehicle.mPlace)),
                    vehicle_class=_text(vehicle.mVehicleClass),
                    control=int(vehicle.mControl),
                    position=(float(vehicle.mPos.x), float(vehicle.mPos.y),
                              float(vehicle.mPos.z)),
                    is_player=bool(vehicle.mIsPlayer),
                    lap_dist=float(vehicle.mLapDist),
                    speed=_speed(vehicle.mLocalVel),
                    laps=max(0, int(vehicle.mTotalLaps)),
                    # Negative is how the block says "no time", and letting
                    # that through would make every car's best lap beat every
                    # real one.
                    last_lap=max(0.0, float(vehicle.mLastLapTime)),
                    best_lap=max(0.0, float(vehicle.mBestLapTime)),
                    in_pits=bool(vehicle.mInPits),
                    # mSector numbers its sectors 0=third, 1=first, 2=second.
                    # Passed through as the block reports it rather than
                    # normalised here, so the one place that has to know the
                    # convention is the module that documents it.
                    sector=int(vehicle.mSector),
                    cur_sector1=max(0.0, float(vehicle.mCurSector1)),
                    cur_sector2=max(0.0, float(vehicle.mCurSector2)),
                    last_sector1=max(0.0, float(vehicle.mLastSector1)),
                    last_sector2=max(0.0, float(vehicle.mLastSector2)),
                ))
            return cars

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or 'LMU is not running'}"
        names = self.drivers()
        if not names:
            return "connected, but no session is running"
        return f"connected — {len(names)} driver(s) in session"
