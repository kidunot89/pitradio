"""Project CARS 2, and everything else that speaks its shared memory.

The `$pcars2$` block is published by Project CARS 2 and 3 and by Automobilista
2, which is why one reader covers three games. Automobilista 2 gets its own
plugin on top of this one — see [ams2.py](ams2.py) — so a profile can name the
game it is actually running.

**Lap times are derived, not read.** The participant array gives lap *counts*
and lap distance in metres, and the timing arrays sit past it in a part of the
structure this deliberately does not map. So a lap time is measured the way a
stopwatch measures one: the clock when the lap counter went up, minus the clock
when it last did.

That has one real consequence and it is worth stating. The clock is this
machine's, not the sim's, so a lap that spans a pause comes out longer than it
was. It fails safe — an inflated lap is never a personal best, so it is never
the reference the trainers chase — but it is why lap times here are a little
less trustworthy than LMU's, which come from the sim.

**Sector calls are not offered.** `mCurrentSector` is an enum whose values
could not be pinned down from outside the games, and a sector index that is
wrong by one puts every split in the wrong sector while looking entirely
reasonable. The plugin does not claim `PROVIDES_SECTORS`, so those behaviours
are skipped with a line in the log.

**Not run against any of the three games.** The reader is tested against a
block built by hand, which catches a wrong width or the padding in
`ParticipantInfo` and cannot catch a wrong assumption about what the games put
in a field.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from pitradio.plugins import pcars2, shared_memory
from pitradio.plugins.base import (
    PROVIDES_FIELD,
    PROVIDES_LAPS,
    PROVIDES_POSITIONS,
    Car,
    PluginSetting,
    SessionInfo,
    SessionPlugin,
    Standings,
)
from pitradio.plugins.derive import Speeds

log = logging.getLogger(__name__)

#: A lap has to be at least this long to be one. Below it the counter moved for
#: some other reason — a session restart, a car being placed on track — and the
#: "lap" is the gap between two unrelated moments.
MIN_LAP_SECONDS = 10.0

#: And no longer than this, or it spans a pause, a tow, or somebody leaving the
#: car in the pits and coming back.
MAX_LAP_SECONDS = 20 * 60.0


class LapTimes:
    """Lap times measured from the lap counter changing.

    A stopwatch, because the participant block carries counts and no times. The
    clock is injected so the whole thing is testable without waiting for laps
    to happen in real time.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        #: driver -> (their lap count, when it last changed)
        self._seen: dict[str, tuple[int, float]] = {}
        self._last: dict[str, float] = {}

    def reset(self) -> None:
        self._seen.clear()
        self._last.clear()

    def observe(self, driver: str, laps: int) -> float:
        """That driver's last lap time, in seconds. Zero until one is measured."""
        now = self._clock()
        previous = self._seen.get(driver)
        self._seen[driver] = (laps, now)

        if previous is None:
            return self._last.get(driver, 0.0)

        was_laps, was_at = previous
        if laps == was_laps:
            # Nothing happened; keep the count's original timestamp so the lap
            # is measured from when it started rather than from the last read.
            self._seen[driver] = (laps, was_at)
            return self._last.get(driver, 0.0)

        if laps < was_laps:
            # The session restarted. Whatever was being timed is not a lap.
            self._last.pop(driver, None)
            return 0.0

        taken = now - was_at
        if MIN_LAP_SECONDS <= taken <= MAX_LAP_SECONDS:
            self._last[driver] = taken
        else:
            self._last.pop(driver, None)
        return self._last.get(driver, 0.0)

    def best(self, driver: str) -> float:
        return self._last.get(driver, 0.0)


class ProjectCars2Plugin(SessionPlugin):
    """The generic one. Automobilista 2 subclasses it with its own identity."""

    id = "pcars2"
    name = "Project CARS 2 / 3"
    executables = ("pcars2.exe", "pcars2avx.exe", "pcars3.exe")
    description = (
        "Reads the driver list, positions and lap distance from the Project "
        "CARS 2 shared memory, which Project CARS 3 and Automobilista 2 also "
        "publish."
    )
    #: No sectors: `mCurrentSector` is an enum that could not be pinned down
    #: from outside the games, and being wrong by one puts every split in the
    #: wrong sector while looking reasonable.
    provides = frozenset({PROVIDES_POSITIONS, PROVIDES_LAPS, PROVIDES_FIELD})
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
            key="proximity_only",
            label="Proximity voice only",
            kind="bool",
            default=False,
            help=("only hear racers near you on track. Off, you hear the whole "
                  "session"),
        ),
        PluginSetting(
            key="proximity_metres",
            label="Proximity range (metres)",
            kind="int",
            default=200,
            help="how near counts, in metres of real track",
        ),
        PluginSetting(
            key="spotter_swap_sides",
            label="Swap spotter sides",
            kind="bool",
            default=False,
            help=("turn this on if the spotter says \"left\" for a car on your "
                  "right. Which side is which depends on the sim's own axes"),
        ),
        PluginSetting(
            key="spotter_metres",
            label="Spotter overlap (metres)",
            kind="int",
            default=9,
            help=("how far apart along the track two cars can be and still "
                  "count as alongside"),
        ),
        PluginSetting(
            key="spotter_width_metres",
            label="Spotter width (metres)",
            kind="int",
            default=12,
            help="how far to the side still counts",
        ),
    )

    def __init__(self, clock=time.monotonic) -> None:
        self._handle = None
        self._view = None
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._logged_failure = False
        self._laps = LapTimes(clock)
        self._speeds = Speeds()
        self._clock = clock
        self._logged_version = False

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
        self._laps.reset()
        self._speeds.reset()
        self._logged_version = False

    def _fail(self, reason: str) -> None:
        self._failure = reason
        if not self._logged_failure:
            self._logged_failure = True
            log.info("%s data unavailable: %s", self.name, reason)

    def _connect(self) -> bool:
        if self._view is not None:
            return True
        if sys.platform != "win32":
            self._fail("the shared memory is only available on Windows")
            return False

        opened = shared_memory.open_existing(pcars2.MEMORY_NAME, pcars2.MEMORY_SIZE)
        if opened is None:
            self._fail(f"{self.name} is not running (no shared memory published)")
            return False

        self._handle, self._view = opened
        self._failure = None
        self._logged_failure = False
        log.info("connected to the %s shared memory", self.name)
        return True

    def is_connected(self) -> bool:
        with self._lock:
            return self._connect()

    # -- reading ---------------------------------------------------------

    def _read(self):
        """(bytes, header) for one snapshot, or None."""
        with self._lock:
            if not self._connect():
                return None
            raw = shared_memory.read(self._view, pcars2.MEMORY_SIZE)

        if len(raw) < pcars2.PARTICIPANTS_AT:
            self._fail("the block is too small to be this game's")
            return None

        try:
            header = pcars2.Header(raw)
        except Exception as exc:
            self._fail(f"unexpected layout: {exc}")
            return None

        if not self._logged_version:
            self._logged_version = True
            # Said once, because it is the first thing worth knowing when the
            # layout turns out to have moved.
            log.info("%s shared memory version %d (build %d)",
                     self.name, header.version, header.build)

        if not pcars2.plausible(raw, header):
            self._fail("the shared memory does not look like this game's; "
                       "the layout may have changed")
            return None
        if not header.playing:
            # In the menus. The block keeps its last contents there.
            return None
        return raw, header

    def _cars(self, snapshot) -> list[Car]:
        raw, header = snapshot
        entries = pcars2.participants(raw, header)
        if not entries:
            return []

        now = self._clock()
        cars = []
        for index, entry in entries:
            last = self._laps.observe(entry.name, entry.laps)
            cars.append(Car(
                slot=index,
                driver=entry.name,
                place=max(0, entry.place),
                # The block has no class names, so multi-class grids resolve
                # overall only. Better an empty class than an invented one.
                vehicle_class="",
                control=0 if index == header.viewed else 2,
                is_player=index == header.viewed,
                position=entry.position,
                lap_dist=max(0.0, entry.lap_distance),
                # No speed in the head of the block. Left at zero the lap book
                # would record nothing at all and the trainers would never see
                # a lap, so it is derived — see `derive.Speeds`.
                speed=self._speeds.of(entry.name, now, max(0.0, entry.lap_distance)),
                laps=max(0, entry.laps),
                last_lap=last,
                best_lap=self._laps.best(entry.name),
            ))
        return cars

    # -- what the app asks for -------------------------------------------

    def _safe_cars(self) -> list[Car]:
        snapshot = self._read()
        if snapshot is None:
            return []
        try:
            return self._cars(snapshot)
        except Exception:
            log.exception("reading the %s block failed", self.name)
            return []

    def drivers(self) -> list[str]:
        return [car.driver for car in self._safe_cars()]

    def vocabulary(self) -> list[str]:
        return self.drivers()

    def standings(self) -> Standings:
        cars = [car for car in self._safe_cars() if car.place > 0]
        if not cars:
            return Standings()
        return Standings({car.place: car.driver for car in cars})

    def session(self) -> SessionInfo:
        """Who is out, and which car is being watched.

        No room key: the block says nothing about a server, so voice chat has
        nothing to agree on with anybody else's copy.
        """
        snapshot = self._read()
        if snapshot is None:
            return SessionInfo()
        try:
            cars = self._cars(snapshot)
        except Exception:
            log.exception("reading the %s block failed", self.name)
            return SessionInfo()
        if not cars:
            return SessionInfo()

        _raw, header = snapshot
        return SessionInfo(
            key="",
            track="",
            cars=tuple(cars),
            # `mViewedParticipantIndex` is what the camera is on, which is the
            # question `listener` asks — and unlike LMU this block answers it
            # directly rather than needing the game's HTTP API.
            focus_slot=header.viewed if header.viewed >= 0 else None,
            elapsed=self._clock(),
        )

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or f'{self.name} is not running'}"
        names = self.drivers()
        if not names:
            return "connected, but no session is running"
        return f"connected — {len(names)} driver(s) in session"
