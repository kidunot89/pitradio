"""Yellows, blues, and where on the circuit the trouble is.

Crew Chief's layout is the guide here, and it draws a line this module keeps:
`car_left`, `still_there`, `clear_all_round` are in `spotter/`, while
`slow_car_in_turn_3`, `stopped_car_ahead`, `local_yellow_ahead` and the whole
full-course-caution vocabulary are in `flags/`. The spotter answers "who is
beside me", which is geometry. Flags answer "what has happened to the track",
which is not — and trying to derive the second from the first is what produced
a warning in every braking zone.

**Three sources, and they are not equally trustworthy.**

*Full-course yellow* comes from the sim and is reliable: `mGamePhase` and
`mYellowFlagState` both read sanely against a live session.

*Blue* comes from the sim, per car, and is reliable for the same reason.

*Local yellows* are **derived**, because LMU's `mSectorFlag` is not usable —
it reads `[11, 11, 1]` under a green flag, which as booleans would mean a
permanent yellow on the whole circuit. See the note in `plugins/lmu.py`. So a
local yellow here means what a marshal means by one: a car has stopped on the
road, and it has been there long enough that it is not simply slow. That is a
derivation from data the sim does publish honestly, in the same spirit as
finding the corners in the speed trace rather than shipping a track map.

The cost of deriving it is that the call cannot precede the incident — a real
yellow flag is out the moment the marshals see it, and this one waits a second
to be sure. The benefit is that it is never wrong about a green track, which
is the failure that makes people turn a feature off.

Pure: arithmetic and bookkeeping on plain objects, no sim.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How long a car has to be stationary on the road before it is an incident
#: rather than somebody having a slow moment. The same reasoning as
#: `spotter.STOPPED_FOR_SECONDS`, and deliberately longer: a spotter call is
#: about the next two seconds and a yellow is about the next lap, so this one
#: can afford to be sure.
STOPPED_FOR_SECONDS = 2.0

#: And how far round the lap a hazard has to be before it stops being ahead of
#: you and starts being behind you. Anything nearer than a lap counts, but the
#: call names how far away it is only inside this.
AHEAD_METRES = 600.0


@dataclass(frozen=True)
class Incident:
    """A car that has stopped on the road, and where."""

    driver: str
    #: Metres round the lap, in the sim's own frame.
    lap_dist: float
    #: How far up the road it is from the driver being spoken to.
    ahead: float
    #: The sector it is in, 1-3, or 0 when the sim does not number them.
    sector: int = 0
    #: The corner it is in, numbered from the reference lap, or 0 when no lap
    #: has been driven yet and there is nothing to number them from.
    corner: int = 0


def sector_of(car) -> int:
    """A car's sector as a person counts it, from the sim's own numbering.

    The sim's is 0=third, 1=first, 2=second, which is documented in
    `sectors.py` and untangled in exactly these two places.
    """
    raw = int(getattr(car, "sector", 0) or 0)
    return {0: 3, 1: 1, 2: 2}.get(raw, 0)


class Incidents:
    """Which cars have stopped on the road, and for how long.

    Separate from `spotter.Stopped` even though both watch the same thing,
    because they answer over different timescales and merging them would force
    one of the two thresholds to be wrong. The spotter needs to know within a
    second so the driver can lift; a yellow is a fact about the circuit that
    stays true for a lap.
    """

    def __init__(self, hold: float = STOPPED_FOR_SECONDS,
                 stopped_speed: float = 4.0) -> None:
        self._hold = hold
        self._speed = stopped_speed
        self._since: dict[str, float] = {}

    def reset(self) -> None:
        self._since.clear()

    def update(self, cars, now: float) -> list[str]:
        """Record everybody's speed. Returns who has been stopped long enough.

        Cars that have got going again are forgotten, so a driver who spins,
        rejoins and spins again starts the clock afresh rather than being
        called instantly the second time.
        """
        seen: set[str] = set()
        stopped: list[str] = []
        for car in cars or ():
            name = getattr(car, "driver", "") or ""
            if not name:
                continue
            seen.add(name)
            if getattr(car, "in_pits", False):
                # A car sitting in its box is stationary and is not an
                # incident. Without this every pit stop is a yellow.
                self._since.pop(name, None)
                continue
            if float(getattr(car, "speed", 0.0) or 0.0) > self._speed:
                self._since.pop(name, None)
                continue
            since = self._since.setdefault(name, now)
            if now - since >= self._hold:
                stopped.append(name)

        # A car that has left the session entirely is not stopped on the road.
        for name in [name for name in self._since if name not in seen]:
            self._since.pop(name, None)
        return stopped


def incidents(own, cars, stopped: list[str], *, track_length: float = 0.0,
              corner_at=None) -> list[Incident]:
    """The stopped cars, as hazards on the road ahead, nearest first.

    `corner_at` turns a lap distance into a corner number — the lap book's, so
    the numbering is the same one the coaching routines use and a driver hears
    one set of corner numbers rather than two. None when no reference lap has
    been driven, and the call then names the sector instead.
    """
    if own is None:
        return []
    mine = float(getattr(own, "lap_dist", 0.0) or 0.0)
    my_name = getattr(own, "driver", "")

    found: list[Incident] = []
    for car in cars or ():
        name = getattr(car, "driver", "") or ""
        if not name or name not in stopped or name == my_name:
            continue
        where = float(getattr(car, "lap_dist", 0.0) or 0.0)
        gap = where - mine
        if gap < 0 and track_length > 0:
            gap += track_length
        corner = 0
        if corner_at is not None:
            corner = int(corner_at(where) or 0)
        found.append(Incident(name, where, gap, sector_of(car), corner))

    found.sort(key=lambda incident: incident.ahead)
    return found


def nearest_ahead(found: list[Incident],
                  metres: float = AHEAD_METRES) -> Incident | None:
    """The one worth calling, or None.

    One, because a driver arriving at an incident can only be told about the
    one they are arriving at. The others are still there next lap.
    """
    for incident in found:
        if 0.0 < incident.ahead <= metres:
            return incident
    return None


# -- what changes are worth saying ----------------------------------------


GREEN, YELLOW, BLUE = "green", "yellow", "blue"


class Watch:
    """What was true last tick, so only changes are spoken.

    A flag is a state, not an event, and a notification that fired on the
    state would say "full course yellow" ten times a second. What a driver
    needs is the two edges: it started, and it is over.
    """

    def __init__(self) -> None:
        self.caution = False
        self.blue = False
        #: Which stopped cars have already been called, so an incident is
        #: announced when it happens and not again every lap you pass it.
        self.called: set[str] = set()

    def reset(self) -> None:
        self.caution = False
        self.blue = False
        self.called.clear()

    def caution_changed(self, now: bool) -> str | None:
        """"full course yellow", "green flag", or nothing."""
        was, self.caution = self.caution, now
        if now and not was:
            return "full course yellow"
        if was and not now:
            return "green flag"
        return None

    def blue_changed(self, now: bool) -> str | None:
        """Called on the rising edge only.

        A blue flag going away is not news — it means the car went past, which
        the driver watched happen.
        """
        was, self.blue = self.blue, now
        return "blue flag" if now and not was else None

    def incident_call(self, incident: Incident | None) -> str | None:
        """What to say about a car stopped up the road, once each.

        Crew Chief's wording, which names the place rather than the driver:
        at the speed this matters, "turn six" is something a driver can act on
        and a name is a syllable count they cannot.
        """
        if incident is None:
            return None
        if incident.driver in self.called:
            return None
        self.called.add(incident.driver)
        if incident.corner:
            return f"car stopped in turn {incident.corner}"
        if incident.sector:
            return f"car stopped in sector {incident.sector}"
        return "car stopped ahead"

    def forget(self, still_stopped: list[str]) -> None:
        """Drop cars that have got going again, so a second spin is called.

        Without this the set only grows, and a driver who goes off twice in a
        session is told about it once.
        """
        self.called &= set(still_stopped)
