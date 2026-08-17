"""Who is alongside, and which side of you they are on.

Every car's world position is already read for proximity voice, so a spotter
costs nothing extra to compute — the geometry is the whole of it.

**Heading comes from where the car has been, not from the sim's orientation
matrix.** The matrix is right there in the scoring block and would be the
obvious thing to use, but its rows are a handedness convention this project
cannot verify: reading it wrong produces a spotter that is confidently mirrored,
which is worse than no spotter at all. Two consecutive positions give a heading
that is true whatever the convention, and the app is already sampling positions
several times a second for the coaching traces.

That leaves one thing genuinely undecidable from here: whether the cross
product's sign means left or right in this sim's world axes. It cannot be
settled without a car on a track, so it is a setting — `spotter_swap_sides` on
the plugin — rather than a guess baked into the code. Anyone who hears "car
left" for a car on their right flips it once and never thinks about it again.

Pure, and no sim: everything below is arithmetic on tuples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LEFT = "left"
RIGHT = "right"

#: The car, in metres. **Everything else is derived from these.**
#:
#: Taken from how Crew Chief models it, which is the right way round: two cars
#: are alongside when their *bodywork* overlaps, and that is a fact about car
#: length, not an arbitrary radius. Its defaults are 4.5m by 1.8m, with
#: per-car overrides — a kart is 2.0 by 1.5, an LMP 4.34 by 1.75, a truck 6.5
#: by 2.5 — which is why these are plugin settings rather than constants.
DEFAULT_CAR_LENGTH = 4.5
DEFAULT_CAR_WIDTH = 1.8

#: Bodywork overlaps when the centres are within a car length. Slightly inside
#: that, so the call means "properly beside you" rather than "just caught the
#: back of them".
ENTER_LENGTHS = 0.9

#: And the call is dropped just past a full length, when they are genuinely
#: clear. This was two car lengths, which is why the all-clear kept arriving
#: long after the car had gone.
LEAVE_LENGTHS = 1.15

#: Two cars side by side have their centres about a car width apart. Nearer
#: than this and they are on the same line — one following the other.
MIN_LATERAL_WIDTHS = 0.9

#: And beyond a few widths they are on another part of the circuit.
MAX_LATERAL_WIDTHS = 4.0

#: Below this the heading taken from two positions is noise rather than a
#: direction.
#:
#: Half a metre was far too little, and this is the fault underneath several
#: others. Through slow traffic consecutive reads are centimetres apart and the
#: direction between them is dominated by the sim's own rounding — so "forward"
#: swings about, and a car directly ahead resolves as a car directly beside.
#: Everything downstream depends on these axes being right.
MIN_HEADING_METRES = 3.0

DEFAULT_ALONGSIDE_METRES = DEFAULT_CAR_LENGTH * LEAVE_LENGTHS
DEFAULT_OVERLAP_METRES = DEFAULT_CAR_LENGTH * ENTER_LENGTHS
DEFAULT_WIDTH_METRES = DEFAULT_CAR_WIDTH * MAX_LATERAL_WIDTHS
MIN_LATERAL_METRES = DEFAULT_CAR_WIDTH * MIN_LATERAL_WIDTHS


def ranges(car_length: float = DEFAULT_CAR_LENGTH,
           car_width: float = DEFAULT_CAR_WIDTH) -> dict[str, float]:
    """Every spotter distance, from the size of the cars.

    One place, so the four of them cannot drift apart — and so a sim whose
    cars are karts or trucks gets all four right by changing two numbers.
    """
    return {
        "overlap": car_length * ENTER_LENGTHS,
        "metres": car_length * LEAVE_LENGTHS,
        "min_lateral": car_width * MIN_LATERAL_WIDTHS,
        "width": car_width * MAX_LATERAL_WIDTHS,
    }


#: How many recent positions the heading is averaged over. Enough to ride out a
#: bad sample without lagging round a corner.
HEADING_SAMPLES = 5


@dataclass(frozen=True)
class Alongside:
    """One car beside you, and how far beside."""

    driver: str
    side: str
    #: Metres to the side, always positive. Kept so a caller can prefer the
    #: nearer of two cars on the same side rather than picking arbitrarily.
    lateral: float
    #: Metres ahead (positive) or behind (negative) along the heading.
    longitudinal: float
    #: Whether the bodywork actually overlaps, rather than this being a car
    #: kept in view by the wider holding range. **Only overlapping cars are
    #: counted**: a car tucked in behind the one beside you is one car on that
    #: side, not two, and calling "two cars left" for it is worse than saying
    #: nothing because the driver looks for something that is not there.
    overlapping: bool = True


def _flat(point) -> tuple[float, float]:
    """A world position as (x, z), dropping height.

    Elevation is what makes a bridge, a banking or the Le Mans esses look like
    somebody sitting on your door. Two cars separated only by altitude are not
    racing each other.
    """
    x, _y, z = point
    return float(x), float(z)


class Heading:
    """Which way the car is pointing, from where it has recently been.

    A running window rather than the last two positions. Two consecutive reads
    are only centimetres apart in slow traffic, and a direction taken from them
    is mostly the sim's rounding — which rotates the whole frame of reference
    and turns the car in front into the car beside. Averaging over a window and
    refusing to answer until the car has actually covered some ground makes the
    axes trustworthy, and everything downstream depends on them being so.
    """

    def __init__(self, samples: int = HEADING_SAMPLES,
                 minimum: float = MIN_HEADING_METRES) -> None:
        self._positions: list[tuple[float, float]] = []
        self._samples = max(2, samples)
        self._minimum = minimum
        self._last: tuple[float, float] | None = None

    def reset(self) -> None:
        self._positions.clear()
        self._last = None

    def update(self, position) -> tuple[float, float] | None:
        """Record where the car is now and return the heading, or None.

        None means "cannot say yet", which callers read as "make no calls" —
        the honest answer while stationary or just after a reset.
        """
        if position is None:
            return self._last
        self._positions.append(_flat(position))
        if len(self._positions) > self._samples:
            self._positions.pop(0)

        oldest, newest = self._positions[0], self._positions[-1]
        dx, dz = newest[0] - oldest[0], newest[1] - oldest[1]
        if math.hypot(dx, dz) < self._minimum:
            # Not enough baseline for the direction to mean anything. The last
            # good heading is kept rather than dropped: a car slowing to a stop
            # is still pointing the way it was a moment ago, and forgetting
            # that would silence the spotter exactly when traffic is closest.
            return self._last

        length = math.hypot(dx, dz)
        self._last = (dx / length, dz / length)
        return self._last


def heading(previous, current) -> tuple[float, float] | None:
    """A unit vector for where a car is pointing, from where it has been.

    None when it has not moved far enough to say — which is a real answer, not
    a failure, and every caller treats it as "no calls this tick".
    """
    if previous is None or current is None:
        return None
    (px, pz), (cx, cz) = _flat(previous), _flat(current)
    dx, dz = cx - px, cz - pz
    length = math.hypot(dx, dz)
    if length < MIN_HEADING_METRES:
        return None
    return dx / length, dz / length


def offsets(mine, facing, theirs) -> tuple[float, float]:
    """(along the heading, across it) for another car, in metres.

    Across is signed. Which sign is which side is the one thing this module
    cannot know — see the module docstring — so `alongside` takes a `swap`.
    """
    (mx, mz), (tx, tz) = _flat(mine), _flat(theirs)
    dx, dz = tx - mx, tz - mz
    forward_x, forward_z = facing
    along = dx * forward_x + dz * forward_z
    across = dx * forward_z - dz * forward_x
    return along, across


def alongside(
    mine,
    facing,
    others: dict[str, tuple[float, float, float]],
    *,
    metres: float = DEFAULT_ALONGSIDE_METRES,
    width: float = DEFAULT_WIDTH_METRES,
    swap: bool = False,
    overlap: float = DEFAULT_OVERLAP_METRES,
    min_lateral: float = MIN_LATERAL_METRES,
    holding: frozenset[str] | None = None,
) -> list[Alongside]:
    """Every car beside this one, nearest first.

    `others` is driver name -> world position, which is exactly what
    `SessionInfo.positions()` already returns for proximity voice.

    **A car is alongside when it is beside you across the track**, not merely
    near you. Three tests, and the middle one was missing until traffic proved
    it: far enough to the side to be a different line (`MIN_LATERAL_METRES`),
    not so far as to be elsewhere on the circuit (`width`), and overlapping
    along the road (`overlap`). All four distances come from `ranges()`, so
    they move together with the size of the cars in the sim.

    Cars out to `metres` are still returned while their side is `holding` a
    call, marked `overlapping=False`. They keep the call alive so it does not
    flicker as two cars breathe — but they are **not counted**, because a car
    tucked in behind the one beside you is one car on that side, not two.
    """
    if facing is None:
        return []

    held = holding or frozenset()
    found: list[Alongside] = []
    for driver, position in (others or {}).items():
        if not driver:
            continue
        along, across = offsets(mine, facing, position)
        sideways = abs(across)

        # Beside you, not in front of you and not on another part of the track.
        if sideways < min_lateral or sideways > width:
            continue

        side = RIGHT if (across > 0) != swap else LEFT
        overlapping = abs(along) <= overlap
        if not overlapping and not (side in held and abs(along) <= metres):
            continue

        found.append(Alongside(driver, side, sideways, along, overlapping))

    found.sort(key=lambda car: abs(car.lateral))
    return found


def call(neighbours: list[Alongside]) -> str | None:
    """What the spotter says about them, or nothing.

    Deliberately not a driver name. At the moment a car is beside you the only
    thing worth hearing is which way not to turn, and a name is a syllable
    count nobody has time for.
    """
    if not neighbours:
        return None
    beside = [car for car in neighbours if car.overlapping] or neighbours
    neighbours = beside
    sides = {car.side for car in neighbours}
    if len(sides) > 1:
        # Cars on both sides is the one call that is about *you* rather than
        # about them: it means there is nowhere to go, which is a different
        # instruction from "somebody is on your left".
        return "three wide" if len(neighbours) <= 2 else "four wide"
    side = neighbours[0].side
    if len(neighbours) > 1:
        return f"two cars {side}"
    return f"car {side}"


def occupied(neighbours: list[Alongside]) -> frozenset[str]:
    """Which sides currently have somebody on them."""
    return frozenset(car.side for car in neighbours)


#: What the spotter says about a car that is *still* there, rather than saying
#: "car left" over and over.
#:
#: Crew Chief's own vocabulary, and it is right for a reason worth writing
#: down: the first call tells you which side, and repeating it makes the driver
#: re-check a side they already know about. "Still there" carries the one piece
#: of information a repeat actually has — that nothing has changed — in two
#: syllables, and it cannot be mistaken for a second car arriving.
STILL_THERE = "still there"

#: And when both sides go clear on the same tick. One call, because two
#: all-clears on top of each other is the moment a driver most needs a short
#: answer.
CLEAR_ALL_ROUND = "clear all round"


def calls(
    now: dict[str, int] | frozenset[str], before: frozenset[str]
) -> list[tuple[str, str, bool]]:
    """(side, what to say, whether it is urgent), for what changed.

    `now` is side -> how many cars, so an arrival says how many rather than
    always "car left": two cars stacked down one side is a different problem
    from one, and the moment they arrive is when that matters most. A bare set
    is accepted as "one car each", which is what a caller that cannot count
    means.

    **Both directions.** A spotter that only says "car left" leaves the driver
    holding a line they no longer need to hold, waiting for a call that never
    comes — which is worse than not being told in the first place, because they
    are now deliberately not using a piece of track. So a side going clear is a
    call in its own right.

    The clear is not urgent and the warning is: one of them means do not move,
    and the other means you may. Only the first can arrive too late to matter.

    A side that has not changed produces nothing here. Repeating a warning
    while a car is still there is a *timer*, not a state change, and belongs to
    the notification that owns the repeat interval.
    """
    tally = now if isinstance(now, dict) else {side: 1 for side in now}
    if (LEFT in before and RIGHT in before
            and LEFT not in tally and RIGHT not in tally):
        # Both at once. Two all-clears back to back is the one place the
        # spotter is talking over itself at exactly the wrong moment.
        return [(LEFT, CLEAR_ALL_ROUND, True)]

    changed: list[tuple[str, str, bool]] = []
    for side in (LEFT, RIGHT):
        was, is_now = side in before, side in tally
        if is_now and not was:
            changed.append((side, warning(side, tally.get(side, 1)), True))
        elif was and not is_now:
            # **Urgent too.** This was ranked below the warning on the reasoning
            # that only a warning can arrive too late to matter. That is wrong
            # from the seat: a driver holding a line for a car that left two
            # corners ago is giving up track they could be using, and they hold
            # it until they are told otherwise. The all-clear is what ends that,
            # so it cannot queue behind a lap time either.
            changed.append((side, f"clear {side}", True))
    return changed


#: How far up the road a hazard is worth warning about. Beyond this there is
#: time to see it; much closer and the call arrives after the impact.
HAZARD_METRES = 250.0

#: Below this a car is stopped rather than slow — on the racing line, facing
#: the wrong way, or in the wall.
STOPPED_SPEED = 4.0

#: And how long it has to stay that way before it is called.
#:
#: **This is the whole of the fix**, and the reason is worth stating plainly.
#: There was a second rule here: a car much slower than you was a hazard too.
#: It fired in every braking zone, because a braking zone is precisely where
#: the car in front is much slower than you — that is what a braking zone is.
#: The rule was wrong in kind, not in threshold, and no number would have saved
#: it.
#:
#: Crew Chief does not have that rule either. Its "slow car ahead" and "stopped
#: car ahead" are in `flags/`, not in `spotter/`, alongside `slow_car_in_turn_3`
#: and `local_yellow_ahead` — they come from the sim saying there is an
#: incident, and they name the corner it is in. Its spotter proper says only
#: which side somebody is on.
#:
#: So what is left is the one case a braking zone cannot produce: a car that is
#: barely moving, and has been for long enough that it is not slowing for
#: anything. The rest belongs with the flags.
STOPPED_FOR_SECONDS = 1.0


@dataclass(frozen=True)
class Hazard:
    """Something stationary on the road ahead."""

    driver: str
    metres: float
    stopped: bool = True


class Stopped:
    """How long each car has been standing still.

    A memory, because one sample cannot tell a stopped car from a slow one and
    the difference is the entire point. Kept here rather than in the
    notification so the rule and its timing are testable together, with a clock
    passed in rather than read.
    """

    def __init__(self, hold: float = STOPPED_FOR_SECONDS) -> None:
        self._hold = hold
        self._since: dict[str, float] = {}

    def reset(self) -> None:
        self._since.clear()

    def update(self, driver: str, speed: float, now: float) -> bool:
        """Record a car's speed. True once it has been stopped long enough."""
        if speed > STOPPED_SPEED:
            self._since.pop(driver, None)
            return False
        since = self._since.setdefault(driver, now)
        return now - since >= self._hold


def ahead(own, cars, *, track_length: float = 0.0,
          metres: float = HAZARD_METRES, stopped: Stopped | None = None,
          now: float = 0.0) -> Hazard | None:
    """The nearest stopped car in front, or None.

    Measured **along the track**, not through the air. Two cars either side of
    a hairpin are metres apart in space and half a lap apart on the road, and a
    spotter that cannot tell those apart cries wolf at every corner.

    `stopped` carries how long each car has been stationary. Without one this
    answers from the single sample, which is what a test that does not care
    about the timing wants; the notification always passes one.

    Nearest first, because only one call can be made and the near one is the
    one about to matter.
    """
    if own is None:
        return None
    mine = float(getattr(own, "lap_dist", 0.0) or 0.0)

    found: list[Hazard] = []
    for car in cars or ():
        name = getattr(car, "driver", "") or ""
        if not name or name == getattr(own, "driver", ""):
            continue
        if getattr(car, "in_pits", False):
            # A car in its pit box is not on the road, however close the
            # numbers say it is.
            continue

        gap = float(getattr(car, "lap_dist", 0.0) or 0.0) - mine
        if gap < 0 and track_length > 0:
            # They are round the lap from here, not behind: on a circuit the
            # car "behind" you is also the car a lap ahead.
            gap += track_length
        if not 0.0 < gap <= metres:
            continue

        speed = float(getattr(car, "speed", 0.0) or 0.0)
        if stopped is not None:
            if stopped.update(name, speed, now):
                found.append(Hazard(name, gap))
        elif speed <= STOPPED_SPEED:
            found.append(Hazard(name, gap))

    if not found:
        return None
    return min(found, key=lambda hazard: hazard.metres)


def hazard_call(hazard: Hazard | None) -> str | None:
    """What to say about what is up the road."""
    if hazard is None:
        return None
    return "car stopped ahead"


def counts(neighbours: list[Alongside]) -> dict[str, int]:
    """side -> how many cars are on it.

    The same shape a sim that does its own spotting reports in, so both routes
    hand the notification one thing and it never has to know which sim it is
    talking to.
    """
    tally: dict[str, int] = {}
    for car in neighbours:
        if not car.overlapping:
            # Held in view by the wider range, so the side stays called — but
            # it is not a second car beside you and must not be counted as one.
            tally.setdefault(car.side, 0)
            continue
        tally[car.side] = tally.get(car.side, 0) + 1
    return tally


def warning(side: str, count: int) -> str:
    """The call for a side somebody has just arrived on.

    Counts them, because two cars stacked down one side is a different problem
    from one — but only cars actually beside you count, so a queue forming
    behind the one on your door stays "car left".
    """
    return f"two cars {side}" if count > 1 else f"car {side}"


def standing(side: str, count: int, *, first: bool) -> tuple[str, str]:
    """(key, what to say) for a side that has somebody on it.

    The key is the **count**, not the words, and that is the whole design. It
    has to stay the same for as long as the situation does, or the repeat
    interval never governs anything: a key that changed when the wording did
    would make the follow-up a brand new call and it would go out on the very
    next tick, a heartbeat after the arrival.

    Keying on the count instead means one car becoming two *is* a new call, and
    goes out immediately — which is right, because it is news — while a car
    simply sitting there repeats on the timer and says `STILL_THERE`.
    """
    return f"{side}:{count}", warning(side, count) if first else STILL_THERE
