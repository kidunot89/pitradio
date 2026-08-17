"""Sector times, and who holds the best of each.

A sector is the unit a driver actually thinks in. It is short enough to
remember what you did in it, it updates three times a lap instead of once, and
"you lost two tenths in sector two" is something you can act on before the lap
is over.

**The sim does not hand them over as sector times.** It publishes cumulative
splits — `cur_sector2` is sector one *plus* sector two — and it never publishes
sector three at all, because sector three is only knowable once the lap time
exists. So all three come out of watching the sector index change:

    1 -> 2   sector one is done, and its time is cur_sector1
    2 -> 0   sector two is done: cur_sector2 - cur_sector1
    0 -> 1   the line: sector three is last_lap - last_sector2

The sim's numbering is its own and is not the obvious one — **0 is sector
three**. That is untangled here, once, and nothing else in the app has to know
it.

Pure: fed a car at a time, so it can be driven from a list of made-up ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The sim's sector index -> the sector a human would call it.
_HUMAN = {1: 1, 2: 2, 0: 3}

#: Below this a sector time is the sim reporting nothing rather than somebody
#: being quick. Sectors on a real circuit are tens of seconds.
MIN_SECTOR_SECONDS = 1.0


@dataclass(frozen=True)
class SectorTime:
    """One sector, just completed."""

    driver: str
    #: 1, 2 or 3, as a person would say it.
    sector: int
    seconds: float
    #: Whether it beat that driver's own best for this sector.
    personal_best: bool = False
    #: Whether it beat everybody's.
    session_best: bool = False
    #: What it beat, or what it fell short of. Zero when there was no previous.
    previous: float = 0.0
    #: Which class the driver is racing in, empty when the sim has none.
    vehicle_class: str = ""
    #: Whether it beat everybody *in that class*. On a single-class grid this
    #: is the same as `session_best`; on an endurance one it is the one that
    #: matters to the driver hearing it.
    class_best: bool = False

    @property
    def delta(self) -> float:
        """Against the driver's previous best. Negative is quicker."""
        return self.seconds - self.previous if self.previous else 0.0


@dataclass
class SectorBook:
    """Everybody's sector times, and the best of each.

    Two kinds of best, because they answer different questions: a driver's own
    is what tells them whether they are improving, and the session's is what
    tells them whether they are quick.
    """

    #: driver -> sector -> their best time in it.
    best: dict[str, dict[int, float]] = field(default_factory=dict)
    #: sector -> (driver, time) for the best anybody has done.
    session_best: dict[int, tuple[str, float]] = field(default_factory=dict)
    #: sector -> the lap distance at which it begins, learned by watching cars
    #: cross the line between them. The sim does not publish where its sector
    #: boundaries are, and every car on the circuit crosses the same ones — so
    #: they are observed rather than configured, exactly as corners are.
    boundaries: dict[int, float] = field(default_factory=dict)
    #: (class, sector) -> (driver, time), for a grid where the overall best is
    #: somebody in a faster category and therefore not your business.
    class_best: dict[tuple[str, int], tuple[str, float]] = field(default_factory=dict)
    #: driver -> the class they are racing in.
    classes: dict[str, str] = field(default_factory=dict)
    #: driver -> the sector index they were last seen in.
    _seen: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        """A new track means none of these numbers mean anything any more."""
        self.best.clear()
        self.session_best.clear()
        self.class_best.clear()
        self.classes.clear()
        self.boundaries.clear()
        self._seen.clear()

    def sector_at(self, distance: float) -> int | None:
        """Which sector a point on the track is in, or None if not yet known.

        None until cars have been seen crossing the boundaries. A routine that
        works on one sector waits for that rather than guessing at thirds of a
        lap, which is wrong on every circuit that is not symmetrical.
        """
        if len(self.boundaries) < 3:
            return None
        ordered = sorted(self.boundaries.items(), key=lambda item: item[1])
        found = ordered[-1][0]
        for sector, start in ordered:
            if distance >= start:
                found = sector
        return found

    def observe(self, car) -> SectorTime | None:
        """Record where a car is. Returns a sector it just finished, if it did.

        Only the fields that exist off Windows are touched, so a plain object
        with the right attributes drives this in a test.
        """
        driver = getattr(car, "driver", "") or ""
        if not driver:
            return None

        vehicle_class = str(getattr(car, "vehicle_class", "") or "")
        if vehicle_class:
            self.classes[driver] = vehicle_class

        index = int(getattr(car, "sector", 0) or 0)
        previous = self._seen.get(driver)
        self._seen[driver] = index
        if previous is None or previous == index:
            return None

        # The car is standing on the boundary it has just crossed, so this is
        # the one moment its position says where that boundary is.
        entering = _HUMAN.get(index)
        if entering is not None:
            distance = float(getattr(car, "lap_dist", 0.0) or 0.0)
            if distance > 0 or entering == 1:
                self.boundaries[entering] = distance

        seconds = self._elapsed(car, previous)
        if seconds < MIN_SECTOR_SECONDS:
            # A car being teleported to the pits, a session restart, or the
            # block simply not having filled the split in yet. None of them is
            # a sector anybody drove.
            return None

        # A car in the pit lane goes through the sectors like anybody else and
        # its times mean nothing.
        if getattr(car, "in_pits", False):
            return None

        return self._record(driver, _HUMAN.get(previous, previous), seconds)

    def _elapsed(self, car, finished: int) -> float:
        """The time for the sector that just ended, from the cumulative splits."""
        if finished == 1:
            return float(getattr(car, "cur_sector1", 0.0) or 0.0)
        if finished == 2:
            first = float(getattr(car, "cur_sector1", 0.0) or 0.0)
            second = float(getattr(car, "cur_sector2", 0.0) or 0.0)
            return second - first if second > first else 0.0
        # Sector three, which the block never publishes: what is left of the
        # lap once the second split is taken off it. Both come from the *last*
        # lap, because by now the car is across the line and into a new one.
        lap = float(getattr(car, "last_lap", 0.0) or 0.0)
        second = float(getattr(car, "last_sector2", 0.0) or 0.0)
        return lap - second if lap > second else 0.0

    def _record(self, driver: str, sector: int, seconds: float) -> SectorTime:
        mine = self.best.setdefault(driver, {})
        previous = mine.get(sector, 0.0)
        personal = previous <= 0 or seconds < previous
        if personal:
            mine[sector] = seconds

        held = self.session_best.get(sector)
        session = held is None or seconds < held[1]
        if session:
            self.session_best[sector] = (driver, seconds)

        vehicle_class = self.classes.get(driver, "")
        in_class = session
        if vehicle_class:
            key = (vehicle_class, sector)
            standing = self.class_best.get(key)
            in_class = standing is None or seconds < standing[1]
            if in_class:
                self.class_best[key] = (driver, seconds)

        return SectorTime(driver, sector, seconds, personal, session, previous,
                          vehicle_class, in_class)

    def best_for(self, driver: str, sector: int) -> float:
        return self.best.get(driver, {}).get(sector, 0.0)

    def fastest(self, sector: int, vehicle_class: str = "") -> tuple[str, float] | None:
        """The best time in a sector, overall or within one class."""
        if vehicle_class:
            return self.class_best.get((vehicle_class, sector))
        return self.session_best.get(sector)
