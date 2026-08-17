"""iRacing session data.

iRacing publishes a shared memory block with no plugin or app required, which
is what [irsdk.py](irsdk.py) reads. Everything in here is the part that turns
that into the app's own `Car` and `SessionInfo`.

**It is a different shape of sim from LMU, and two of the differences matter.**

*No other car has a world position.* The block gives `CarIdxLapDistPct` — how
far round the lap each car is, as a fraction — and nothing about where that is
in space. Proximity voice and a geometric spotter both need real positions, so
neither can be built the way LMU's are. In exchange iRacing publishes
`CarLeftRight`, its own spotter call, computed from the actual car bodies
rather than from a point and an assumed width — a better answer than the
geometry, not a worse one. That is what `PROVIDES_SPOTTER` exists for.

*No other car has a speed.* Only the player's own `Speed` channel exists. Since
lap distance and the session clock are both published, speed is derived here
from how far a car moved between two reads — which is what a speed trap does,
and is accurate enough for the coaching traces at the rate the block updates.

**Sector times are not implemented yet**, and the plugin says so rather than
guessing: `SplitTimeInfo` gives where the sector boundaries are, but iRacing
does not publish per-car splits, so they would have to be timed here. Until
that exists the plugin does not claim `PROVIDES_SECTORS`, and the sector
behaviours are skipped with a line in the log instead of being switched on and
permanently silent.

**None of this has been run against the real game.** The parsing is tested
against a block built by hand — see `tests/test_iracing.py` — which catches a
wrong offset or a misnamed channel, and cannot catch a wrong assumption about
what iRacing puts in them.
"""

from __future__ import annotations

import logging
import re
import sys
import threading

from pitradio.plugins import irsdk, shared_memory
from pitradio.plugins.base import (
    PROVIDES_LAPS,
    PROVIDES_SPOTTER,
    Car,
    PluginSetting,
    SessionInfo,
    SessionPlugin,
    Standings,
)

log = logging.getLogger(__name__)

#: `irsdk_TrkLoc`: below this the car is not on the circuit at all. Cars in
#: this state are dropped rather than reported at lap distance zero, which is a
#: real place on the track and would put them on the start line.
NOT_IN_WORLD = -1

#: `irsdk_CarLeftRight`, mapped to the sides the spotter speaks in. 0 is "off"
#: and 1 is "clear", both of which mean nobody is there.
LEFT_RIGHT = {
    0: {},
    1: {},
    2: {"left": 1},
    3: {"right": 1},
    4: {"left": 1, "right": 1},
    5: {"left": 2},
    6: {"right": 2},
}

#: Track length arrives as a string with its unit on it: "5.55 km".
_LENGTH = re.compile(r"([\d.]+)\s*(km|mi)?", re.IGNORECASE)

#: Below this a car has not moved enough between reads for the derived speed to
#: be a speed rather than the noise on a lap-distance fraction.
MIN_DELTA_SECONDS = 0.05

#: Metres per second past which the reading is not a car. Formula machinery
#: tops out around 103 m/s and the fastest ovals a little over that, so this
#: leaves generous room while still catching a car that has been moved rather
#: than driven.
MAX_SPEED = 130.0


def track_length(raw) -> float:
    """"5.55 km" -> 5550.0. Zero when it cannot be read.

    Zero rather than a guess: it is used to decide whether a recorded lap
    covered the whole circuit, and a wrong length silently accepts half-laps as
    reference laps.
    """
    if isinstance(raw, (int, float)):
        return float(raw) * 1000.0
    match = _LENGTH.search(str(raw or ""))
    if not match:
        return 0.0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0.0
    unit = (match.group(2) or "km").lower()
    return value * (1609.344 if unit == "mi" else 1000.0)


def sides(value) -> dict[str, int]:
    """`CarLeftRight` as side -> how many cars.

    Anything unrecognised is empty rather than a guess. A spotter inventing a
    car is worse than one that missed it: the driver leaves room for somebody
    who is not there, and stops trusting the next call.
    """
    try:
        return dict(LEFT_RIGHT.get(int(value), {}))
    except (TypeError, ValueError):
        return {}


class Speeds:
    """Per-car speed, derived from how far they moved between two reads.

    iRacing publishes a speed for the player and for nobody else, so this is
    the only way the coaching traces get one for a car being chased. It is a
    speed trap rather than a speedometer: distance over time, which is exactly
    right on average and slightly behind through a corner.
    """

    def __init__(self) -> None:
        #: driver -> (session time, distance round the lap)
        self._seen: dict[str, tuple[float, float]] = {}

    def reset(self) -> None:
        self._seen.clear()

    def of(self, driver: str, elapsed: float, distance: float,
           length: float) -> float:
        previous = self._seen.get(driver)
        self._seen[driver] = (elapsed, distance)
        if previous is None or length <= 0:
            return 0.0

        was_at, was_distance = previous
        span = elapsed - was_at
        if span < MIN_DELTA_SECONDS:
            return 0.0

        moved = distance - was_distance
        if moved < 0:
            # Crossed the line: what was left of the old lap plus what has been
            # done of the new one.
            moved += length

        # Sanity, not arithmetic. A car sent to the pits, a session restart or
        # a tow all jump the lap distance by hundreds of metres between two
        # reads, and every one of them comes out of the subtraction above as a
        # perfectly well-formed enormous speed. Checking the *speed* catches
        # them whichever direction they jumped, which comparing distances did
        # not: a teleport from 4000m to 100m wraps to 1100m and read as 1100
        # metres per second.
        speed = moved / span
        return speed if 0.0 <= speed <= MAX_SPEED else 0.0


class IRacingPlugin(SessionPlugin):
    id = "iracing"
    name = "iRacing"
    executables = ("iracingsim64dx11.exe", "iracingsim.exe")
    description = (
        "Reads the driver list, lap times and iRacing's own left/right spotter "
        "call from its shared memory. No app or plugin needed in the sim."
    )
    #: Not `positions`: iRacing publishes none for other cars. Not `sectors`
    #: either — see the module docstring. Claiming what is not there is how a
    #: behaviour ends up switched on and permanently silent.
    provides = frozenset({PROVIDES_LAPS, PROVIDES_SPOTTER})
    settings = (
        PluginSetting(
            key="positions",
            label="Recognise standings positions",
            kind="bool",
            default=True,
            help=('say "P3" and it sends that driver\'s name — useful when you '
                  "cannot pronounce it or did not catch it"),
        ),
        PluginSetting(
            key="include_pace_car",
            label="Include the pace car",
            kind="bool",
            default=False,
            help=("off, so the pace car is not offered as a driver to mention "
                  "or a lap to chase"),
        ),
    )

    def __init__(self) -> None:
        self._handle = None
        self._view = None
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._logged_failure = False
        self._speeds = Speeds()
        #: The session string is ~200KB of YAML and only changes when the
        #: session does, so it is parsed on its update counter rather than on
        #: every read.
        self._session_update = -1
        self._session: dict = {}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Deliberately does nothing; the sim is usually not running yet."""

    def stop(self) -> None:
        with self._lock:
            self._release()

    def _release(self) -> None:
        if self._handle or self._view:
            shared_memory.close(self._handle, self._view)
        self._handle = self._view = None
        self._speeds.reset()
        self._session_update = -1
        self._session = {}

    def _fail(self, reason: str) -> None:
        self._failure = reason
        if not self._logged_failure:
            self._logged_failure = True
            log.info("iRacing data unavailable: %s", reason)

    def _connect(self) -> bool:
        if self._view is not None:
            return True
        if sys.platform != "win32":
            self._fail("iRacing shared memory is only available on Windows")
            return False

        opened = shared_memory.open_existing(irsdk.MEMORY_NAME, irsdk.MEMORY_SIZE)
        if opened is None:
            self._fail("iRacing is not running (no shared memory published)")
            return False

        self._handle, self._view = opened
        self._failure = None
        self._logged_failure = False
        log.info("connected to iRacing shared memory")
        return True

    def is_connected(self) -> bool:
        with self._lock:
            return self._connect()

    # -- reading ---------------------------------------------------------

    def _read(self) -> tuple[bytes, irsdk.Header, dict, int] | None:
        """One snapshot: (bytes, header, channels, newest buffer offset).

        Everything comes out of a single copy of the block, for the reason
        every other plugin here does the same — two reads are two moments, and
        a car's distance from one with its lap count from the other describes
        a frame that never existed.
        """
        with self._lock:
            if not self._connect():
                return None
            raw = shared_memory.read(self._view, irsdk.MEMORY_SIZE)

        if len(raw) < irsdk.HEADER_SIZE:
            self._fail("the block is too small to be iRacing's")
            return None

        try:
            header = irsdk.Header(raw)
        except Exception as exc:
            self._fail(f"unexpected layout: {exc}")
            return None

        if not header.connected:
            # Published but not in a session: iRacing leaves the mapping in
            # place when you leave the car.
            return None

        offset = header.latest()
        if offset is None:
            return None
        return raw, header, irsdk.channels(raw, header), offset

    def _describe(self, raw: bytes, header: irsdk.Header) -> dict:
        """The session string, parsed only when it has changed."""
        if header.session_update == self._session_update and self._session:
            return self._session
        text = irsdk.session_string(raw, header)
        if not text:
            return self._session
        try:
            self._session = irsdk.parse_session(text)
            self._session_update = header.session_update
        except Exception:
            log.debug("could not parse the iRacing session string", exc_info=True)
        return self._session

    def _drivers(self, described: dict) -> dict[int, dict]:
        """CarIdx -> what the session string says about them."""
        info = described.get("DriverInfo") or {}
        entries = info.get("Drivers") or []
        found: dict[int, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                found[int(entry.get("CarIdx", -1))] = entry
            except (TypeError, ValueError):
                continue
        return found

    def _cars(self, snapshot, settings: dict | None = None) -> list[Car]:
        raw, header, channels, offset = snapshot
        described = self._describe(raw, header)
        drivers = self._drivers(described)
        if not drivers:
            return []

        length = track_length(
            (described.get("WeekendInfo") or {}).get("TrackLength"))
        elapsed = irsdk.value(raw, offset, channels.get("SessionTime")) or 0.0
        own_index = irsdk.value(raw, offset, channels.get("PlayerCarIdx"))
        keep_pace_car = bool((settings or {}).get("include_pace_car"))

        def channel(name):
            return irsdk.values(raw, offset, channels.get(name))

        surface = channel("CarIdxTrackSurface")
        distance_pct = channel("CarIdxLapDistPct")
        laps = channel("CarIdxLapCompleted")
        last = channel("CarIdxLastLapTime")
        best = channel("CarIdxBestLapTime")
        pits = channel("CarIdxOnPitRoad")
        places = channel("CarIdxPosition")

        def at(values, index, fallback=0):
            if index < len(values) and values[index] is not None:
                return values[index]
            return fallback

        cars: list[Car] = []
        for index in sorted(drivers):
            entry = drivers[index]
            name = str(entry.get("UserName") or "").strip()
            if not name:
                continue
            if entry.get("CarIsPaceCar") and not keep_pace_car:
                continue
            if at(surface, index, NOT_IN_WORLD) == NOT_IN_WORLD:
                # In the garage, or not yet out. A car reported at lap distance
                # zero would be sitting on the start line.
                continue

            fraction = at(distance_pct, index, 0.0)
            lap_dist = max(0.0, float(fraction) * length)
            cars.append(Car(
                slot=index,
                driver=name,
                place=max(0, int(at(places, index, 0))),
                vehicle_class=str(entry.get("CarClassShortName") or "").strip(),
                # iRacing has no equivalent of "the AI has your car", so the
                # player's entry is always the one being driven from here.
                control=0 if index == own_index else 2,
                is_player=index == own_index,
                lap_dist=lap_dist,
                speed=self._speeds.of(name, float(elapsed), lap_dist, length),
                laps=max(0, int(at(laps, index, 0))),
                # Negative is how iRacing says "no time", and letting that
                # through would beat every real lap.
                last_lap=max(0.0, float(at(last, index, 0.0))),
                best_lap=max(0.0, float(at(best, index, 0.0))),
                in_pits=bool(at(pits, index, False)),
            ))
        return cars

    # -- what the app asks for -------------------------------------------

    def drivers(self) -> list[str]:
        return [car.driver for car in self._safe_cars()]

    def classes(self) -> list[str]:
        return list(dict.fromkeys(car.vehicle_class for car in self._safe_cars()
                                  if car.vehicle_class))

    def vocabulary(self) -> list[str]:
        return self.classes() + self.drivers()

    def _safe_cars(self, settings: dict | None = None) -> list[Car]:
        snapshot = self._read()
        if snapshot is None:
            return []
        try:
            return self._cars(snapshot, settings)
        except Exception:
            log.exception("reading the iRacing block failed")
            return []

    def standings(self) -> Standings:
        cars = [car for car in self._safe_cars() if car.place > 0]
        if not cars:
            return Standings()

        overall = {car.place: car.driver for car in cars}
        by_class: dict[str, dict[int, str]] = {}
        for name in dict.fromkeys(car.vehicle_class for car in cars
                                  if car.vehicle_class):
            members = sorted((car.place, car.driver) for car in cars
                             if car.vehicle_class == name)
            by_class[name] = {rank: driver
                              for rank, (_place, driver) in enumerate(members, 1)}
        return Standings(overall, by_class)

    def session(self) -> SessionInfo:
        """Who is out, and iRacing's own view of who is beside you.

        There is no room key. iRacing's session id would identify the server,
        but voice chat is not offered here at all: without other cars' world
        positions there is no proximity, and a room with no proximity in a
        forty-car field is not something to switch on by accident.
        """
        snapshot = self._read()
        if snapshot is None:
            return SessionInfo()

        try:
            cars = self._cars(snapshot)
        except Exception:
            log.exception("reading the iRacing block failed")
            return SessionInfo()
        if not cars:
            return SessionInfo()

        raw, header, channels, offset = snapshot
        described = self._describe(raw, header)
        weekend = described.get("WeekendInfo") or {}
        return SessionInfo(
            key="",
            track=str(weekend.get("TrackDisplayName")
                      or weekend.get("TrackName") or "").strip(),
            cars=tuple(cars),
            track_length=track_length(weekend.get("TrackLength")),
            elapsed=float(
                irsdk.value(raw, offset, channels.get("SessionTime")) or 0.0),
            alongside=sides(
                irsdk.value(raw, offset, channels.get("CarLeftRight"))),
        )

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or 'iRacing is not running'}"
        names = self.drivers()
        if not names:
            return "connected, but no session is running"
        return f"connected — {len(names)} driver(s) in session"
