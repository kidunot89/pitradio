"""Assetto Corsa, Competizione, and Evo.

One plugin for three games because they publish the same shared memory pages
under the same names — `acpmf_static` and `acpmf_graphics`. Which of them is
running is not something this has to know, and deliberately: the profile picks
the plugin, and a game that speaks this format works whether or not anybody
thought of it here.

**What Assetto Corsa gives, and what it does not.** This is the most limited of
the sims supported so far and it is worth being plain about, because the shape
of the integration follows from it:

* Every car's **world position** — so the spotter and proximity both work, the
  same geometry LMU uses.
* The player's **laps and sector times**, from the timing-screen page.
* **Nothing about anybody else's laps**, and **no driver names at all.** The
  pages carry an id per car and a set of coordinates, and that is the whole of
  it.

So the trainers work against **your own best lap**, which is the honest and
still useful version — chasing your own reference is what a practice session
is. What cannot work is anything comparing you to the field: standings, driver
mentions, "somebody has taken the fastest lap". The plugin does not claim
`PROVIDES_FIELD`, so those are skipped with a line in the log rather than
firing with a field of one.

Other cars appear as "Car 12", from the id the page gives them. The spotter
never says a name, so that costs nothing where it is used and keeps them out of
the places names matter.

**Assetto Corsa Evo is early access and its layout was not verifiable.** It is
listed here because it publishes these page names and the fields this reads are
the oldest and least likely to have moved. `plausible()` refuses the pages
outright if the values do not look like Assetto Corsa's, which turns a layout
change into "not connected" rather than into confident nonsense. Run
`--telemetry` with the game on track before trusting it.
"""

from __future__ import annotations

import logging
import sys
import threading

from pitradio.plugins import acpmf, shared_memory
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

#: Lap and sector times come as milliseconds, and "no time" is a huge sentinel
#: rather than zero — 99999999 in practice. Anything past an hour is that.
NO_TIME_MS = 60 * 60 * 1000


def seconds(milliseconds) -> float:
    """A time from the pages, in seconds. Zero means "not set".

    The sentinel matters: left alone it becomes a lap of eleven hours, which
    beats nothing and is never the fastest — but it *is* a valid best lap as
    far as everything downstream is concerned, and the trainers would target
    it.
    """
    if milliseconds is None:
        return 0.0
    try:
        value = float(milliseconds)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0 or value >= NO_TIME_MS:
        return 0.0
    return value / 1000.0


class TrackLength:
    """How long a lap is, learned by watching the car go round.

    Assetto Corsa gives `normalizedCarPosition`, a fraction of the lap, and
    `distanceTraveled`. Neither is a track length on its own, and the static
    page's `trackSPlineLength` sits past the run of fields the three games
    agree on — reading it would mean an offset nobody can check.

    Between two moments, though, the distance covered divided by the fraction
    of a lap covered *is* the length, and that holds whether `distanceTraveled`
    counts the lap or the whole session. So it is measured rather than looked
    up, in the same spirit as finding the corners in the data.
    """

    #: Enough of a lap for the division to mean anything. Too small and the
    #: quantisation on the fraction dominates; too large and it takes half a
    #: lap to learn.
    MIN_FRACTION = 0.02
    #: A lap is somewhere between a kart track and the Nordschleife. Anything
    #: outside this came from a wrap or a teleport.
    MIN_METRES, MAX_METRES = 400.0, 30000.0

    def __init__(self) -> None:
        self.metres = 0.0
        self._previous: tuple[float, float] | None = None

    def reset(self) -> None:
        self.metres = 0.0
        self._previous = None

    def observe(self, fraction: float, travelled: float) -> float:
        previous, self._previous = self._previous, (fraction, travelled)
        if previous is None:
            return self.metres

        was_fraction, was_travelled = previous
        moved = travelled - was_travelled
        turned = fraction - was_fraction
        if turned < self.MIN_FRACTION or moved <= 0:
            # Not far enough, standing still, or across the line — where the
            # fraction wraps to zero and the division would be negative.
            return self.metres

        estimate = moved / turned
        if self.MIN_METRES <= estimate <= self.MAX_METRES:
            # Kept once found. It does not change during a session, and later
            # samples are noisier rather than better.
            self.metres = self.metres or estimate
        return self.metres


def player_name(static: bytes) -> str:
    """What to call the person at the wheel.

    Their nickname if they have one, which is what Assetto Corsa shows other
    people online, and their real name otherwise. Never empty: it is the key
    the lap book files their laps under, and an empty one would file every
    lap under the same blank name as every other car.
    """
    nick = acpmf.text(static, acpmf.STATIC, "playerNick")
    if nick:
        return nick
    first = acpmf.text(static, acpmf.STATIC, "playerName")
    last = acpmf.text(static, acpmf.STATIC, "playerSurname")
    full = " ".join(part for part in (first, last) if part)
    return full or "You"


class AssettoCorsaPlugin(SessionPlugin):
    id = "assetto"
    name = "Assetto Corsa / Competizione / Evo"
    executables = ("acs.exe", "acs_x64.exe", "ac2-win64-shipping.exe",
                   "assettocorsaevo.exe")
    description = (
        "Reads your laps and sectors, and every car's position for the "
        "spotter, from the Assetto Corsa shared memory pages. Other drivers' "
        "names and lap times are not published by the game."
    )
    #: No `field`: the pages carry no lap data or names for anybody but the
    #: player, so anything comparing you to the grid would be comparing you to
    #: yourself and calling it the session.
    provides = frozenset({PROVIDES_POSITIONS, PROVIDES_LAPS, PROVIDES_SECTORS})
    settings = (
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

    def __init__(self) -> None:
        self._pages: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._failure: str | None = None
        self._logged_failure = False
        #: The player's own sector timing, rebuilt from the page's sector index
        #: and last sector time, which is all Assetto Corsa gives.
        self._sector = 0
        self._splits: dict[int, float] = {}
        self._last_splits: dict[int, float] = {}
        self._laps = -1
        self._length = TrackLength()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Deliberately does nothing; the sim is usually not running yet."""

    def stop(self) -> None:
        with self._lock:
            self._release()

    def _release(self) -> None:
        for handle, view in self._pages.values():
            shared_memory.close(handle, view)
        self._pages = {}
        self._sector = 0
        self._splits = {}
        self._last_splits = {}
        self._laps = -1
        self._length.reset()

    def _fail(self, reason: str) -> None:
        self._failure = reason
        if not self._logged_failure:
            self._logged_failure = True
            log.info("Assetto Corsa data unavailable: %s", reason)

    def _connect(self) -> bool:
        if self._pages:
            return True
        if sys.platform != "win32":
            self._fail("the Assetto Corsa pages are only available on Windows")
            return False

        opened = {}
        for name, page, size in (
            ("static", acpmf.STATIC_PAGE, acpmf.STATIC_SIZE),
            ("graphics", acpmf.GRAPHICS_PAGE, acpmf.GRAPHICS_SIZE),
            ("physics", acpmf.PHYSICS_PAGE, acpmf.PHYSICS_SIZE),
        ):
            mapping = shared_memory.open_existing(page, size)
            if mapping is None:
                for handle, view in opened.values():
                    shared_memory.close(handle, view)
                self._fail("Assetto Corsa is not running (no shared memory)")
                return False
            opened[name] = mapping

        self._pages = opened
        self._failure = None
        self._logged_failure = False
        log.info("connected to the Assetto Corsa shared memory")
        return True

    def is_connected(self) -> bool:
        with self._lock:
            return self._connect()

    # -- reading ---------------------------------------------------------

    def _read(self) -> tuple[bytes, bytes, bytes] | None:
        """All three pages, copied in one go under the lock.

        Together, because they describe one moment: a lap fraction from the
        graphics page with a speed from a physics page written a frame later is
        a car that was never quite anywhere.
        """
        with self._lock:
            if not self._connect():
                return None
            static = shared_memory.read(
                self._pages["static"][1], acpmf.STATIC_SIZE)
            graphics = shared_memory.read(
                self._pages["graphics"][1], acpmf.GRAPHICS_SIZE)
            physics = shared_memory.read(
                self._pages["physics"][1], acpmf.PHYSICS_SIZE)

        if not acpmf.plausible(static, graphics):
            # Refused rather than reported. Wrong offsets do not raise; they
            # produce a nameless track and a grid of hundreds, and the engineer
            # would talk about them.
            self._fail("the shared memory does not look like Assetto Corsa's; "
                       "the layout may have changed")
            return None

        status = acpmf.number(graphics, acpmf.GRAPHICS, "status")
        if status is None or status < acpmf.STATUS_LIVE:
            # The pages keep their last contents when you leave the car.
            return None
        return static, graphics, physics

    def _sectors_for(self, graphics: bytes) -> tuple[int, float, float]:
        """(sector in the app's numbering, cumulative s1, cumulative s2).

        Assetto Corsa gives a sector *index* and the time of the sector just
        finished, and nothing cumulative — so the running totals are kept here.
        The app's `Car.sector` numbers sector three as 0, which is rFactor's
        convention and the one `engineer/sectors.py` untangles, so the index is
        translated rather than passed through.
        """
        index = acpmf.number(graphics, acpmf.GRAPHICS, "currentSectorIndex") or 0
        last = seconds(acpmf.number(graphics, acpmf.GRAPHICS, "lastSectorTime"))
        laps = acpmf.number(graphics, acpmf.GRAPHICS, "completedLaps") or 0

        if laps != self._laps:
            self._laps = laps
            self._last_splits = dict(self._splits)
            self._splits = {}

        if index != self._sector:
            # The sector that just ended is the one before this one, and its
            # time is what the page is now reporting.
            finished = self._sector
            self._sector = index
            if last > 0:
                running = self._splits.get(finished - 1, 0.0) if finished else 0.0
                self._splits[finished] = running + last

        # 0 -> sector one, 1 -> sector two, 2 -> sector three, which the app
        # calls 0 for the same reason rFactor does.
        return ({0: 1, 1: 2, 2: 0}.get(int(index), 1),
                self._splits.get(0, 0.0), self._splits.get(1, 0.0))

    def _cars(self, pages) -> list[Car]:
        static, graphics, physics = pages
        name = player_name(static)
        travelled = float(
            acpmf.number(graphics, acpmf.GRAPHICS, "distanceTraveled") or 0.0)
        fraction = float(
            acpmf.number(graphics, acpmf.GRAPHICS, "normalizedCarPosition") or 0.0)
        track_length = self._length.observe(fraction, travelled)

        sector, split1, split2 = self._sectors_for(graphics)
        own_slot = 0
        position = acpmf.coordinates(graphics, own_slot) or (0.0, 0.0, 0.0)

        cars = [Car(
            slot=own_slot,
            driver=name,
            place=max(0, int(
                acpmf.number(graphics, acpmf.GRAPHICS, "position") or 0)),
            control=0,
            is_player=True,
            position=position,
            lap_dist=max(0.0, fraction * track_length),
            # From the physics page, not derived: without a speed the lap book
            # records no samples at all and the trainers never see a lap.
            speed=max(0.0, float(
                acpmf.number(physics, acpmf.PHYSICS, "speedKmh") or 0.0) / 3.6),
            laps=max(0, int(
                acpmf.number(graphics, acpmf.GRAPHICS, "completedLaps") or 0)),
            last_lap=seconds(acpmf.number(graphics, acpmf.GRAPHICS, "iLastTime")),
            best_lap=seconds(acpmf.number(graphics, acpmf.GRAPHICS, "iBestTime")),
            in_pits=bool(acpmf.number(graphics, acpmf.GRAPHICS, "isInPit")),
            sector=sector,
            cur_sector1=split1,
            cur_sector2=split2,
            last_sector1=self._last_splits.get(0, 0.0),
            last_sector2=self._last_splits.get(1, 0.0),
        )]

        # Everybody else: a position and an id, which is all the pages have.
        # Named from the id so the spotter has something to key on; it never
        # says a name, and nothing that does will match one of these.
        active = acpmf.number(graphics, acpmf.GRAPHICS, "activeCars")
        total = int(active) if active else int(
            acpmf.number(static, acpmf.STATIC, "numCars") or 0)
        for slot in range(1, min(max(0, total), acpmf.MAX_CARS)):
            where = acpmf.coordinates(graphics, slot)
            if where is None or where == (0.0, 0.0, 0.0):
                continue
            identifier = acpmf.number(graphics, acpmf.GRAPHICS, "carID", slot)
            cars.append(Car(
                slot=slot,
                driver=f"Car {int(identifier) if identifier is not None else slot}",
                control=2,
                is_player=False,
                position=where,
            ))
        return cars

    # -- what the app asks for -------------------------------------------

    def _safe_cars(self) -> list[Car]:
        pages = self._read()
        if pages is None:
            return []
        try:
            return self._cars(pages)
        except Exception:
            log.exception("reading the Assetto Corsa pages failed")
            return []

    def drivers(self) -> list[str]:
        """Only the player. The pages carry no names for anybody else.

        Returning "Car 12" here would put them in Whisper's vocabulary and
        offer them as mentions, which is worse than saying there is nobody:
        a mention nobody recognises is a message that reads as a mistake.
        """
        return [car.driver for car in self._safe_cars() if car.is_player]

    def vocabulary(self) -> list[str]:
        return self.drivers()

    def standings(self) -> Standings:
        """Empty, and it has to be.

        The page gives the player's own position and nobody's name, so "P3"
        could only ever resolve to the player or to nothing. Answering it with
        the one name available would put your own name in a message about
        somebody else.
        """
        return Standings()

    def session(self) -> SessionInfo:
        pages = self._read()
        if pages is None:
            return SessionInfo()
        try:
            cars = self._cars(pages)
        except Exception:
            log.exception("reading the Assetto Corsa pages failed")
            return SessionInfo()
        if not cars:
            return SessionInfo()

        static, graphics, _physics = pages
        own = cars[0]
        return SessionInfo(
            key="",
            track=acpmf.text(static, acpmf.STATIC, "track"),
            cars=tuple(cars),
            track_length=self._length.metres,
            # **The current lap time, in seconds.** There is no session clock in
            # these pages — only the time *left*, which counts down and is zero
            # in a lap-limited race. A per-lap clock is not a compromise here
            # either: a trace is one lap, and every time it is asked for is a
            # subtraction between two points on the same lap.
            elapsed=seconds(
                acpmf.number(graphics, acpmf.GRAPHICS, "iCurrentTime")),
            focus_slot=own.slot,
        )

    def status(self) -> str:
        if not self.is_connected():
            return f"not connected — {self._failure or 'Assetto Corsa is not running'}"
        cars = self._safe_cars()
        if not cars:
            return "connected, but no session is running"
        return f"connected — driving as {cars[0].driver}"
