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

#: How far apart two cars may be along the track and still count as alongside
#: **once they already are**. A prototype is about 5m long, so this is
#: overlapping bodywork plus a car either way.
DEFAULT_ALONGSIDE_METRES = 9.0

#: And how much closer they must be before the call is *made* in the first
#: place. Deliberately tighter than the range that keeps it.
#:
#: The two are different questions and answering them with one number gets both
#: wrong. Announcing at the full range calls a car that is still most of a
#: length back — which the driver cannot see, does not believe, and learns to
#: ignore. Dropping the call at the same range would then have it flicker on
#: and off as the two cars breathe. Enter close, leave at arm's length.
DEFAULT_OVERLAP_METRES = 4.0

#: How far to the side. Beyond this they are on a different part of the
#: circuit: an adjacent straight, or the other side of a hairpin, which is
#: exactly where a naive distance check starts shouting about nobody.
DEFAULT_WIDTH_METRES = 12.0

#: Below this the heading derived from two positions is noise rather than a
#: direction, so nothing is called at all. A stationary car in the pits would
#: otherwise have every car on the circuit swinging around it.
MIN_HEADING_METRES = 0.5


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


def _flat(point) -> tuple[float, float]:
    """A world position as (x, z), dropping height.

    Elevation is what makes a bridge, a banking or the Le Mans esses look like
    somebody sitting on your door. Two cars separated only by altitude are not
    racing each other.
    """
    x, _y, z = point
    return float(x), float(z)


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
    holding: frozenset[str] | None = None,
) -> list[Alongside]:
    """Every car beside this one, nearest first.

    `others` is driver name -> world position, which is exactly what
    `SessionInfo.positions()` already returns for proximity voice.

    **Two ranges, not one.** A car has to come within `overlap` before it
    counts as alongside at all; once a side is `holding` a call, cars stay
    counted out to `metres`. Announcing at the outer range calls somebody the
    driver cannot yet see beside them, and dropping at the inner one makes the
    call flicker as two cars breathe. `holding` is the set of sides currently
    being called, which is what the notification already tracks.
    """
    if facing is None:
        return []

    held = holding or frozenset()
    found: list[Alongside] = []
    for driver, position in (others or {}).items():
        if not driver:
            continue
        along, across = offsets(mine, facing, position)
        if abs(across) > width:
            continue
        side = RIGHT if (across > 0) != swap else LEFT
        reach = metres if side in held else min(overlap, metres)
        if abs(along) > reach:
            continue
        if abs(across) < 0.5:
            # Directly in front or behind at overlapping distance means the
            # positions came from different moments, not that somebody is
            # inside the car. Nothing useful can be said about it.
            continue
        found.append(Alongside(driver, side, abs(across), along))

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

#: And how much slower than you a moving car has to be before it is a hazard
#: rather than simply someone you are catching. A car forty metres a second
#: slower is a closing speed no driver expects.
SLOWER_BY = 20.0


@dataclass(frozen=True)
class Hazard:
    """Something stationary or much slower on the road ahead."""

    driver: str
    metres: float
    stopped: bool


def ahead(own, cars, *, track_length: float = 0.0,
          metres: float = HAZARD_METRES) -> Hazard | None:
    """The nearest stopped or much slower car in front, or None.

    Measured **along the track**, not through the air. Two cars either side of
    a hairpin are metres apart in space and half a lap apart on the road, and a
    spotter that cannot tell those apart cries wolf at every corner.

    Nearest first, because only one call can be made and the near one is the
    one about to matter.
    """
    if own is None:
        return None
    mine = float(getattr(own, "lap_dist", 0.0) or 0.0)
    my_speed = float(getattr(own, "speed", 0.0) or 0.0)

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
        if speed <= STOPPED_SPEED:
            found.append(Hazard(name, gap, True))
        elif my_speed - speed >= SLOWER_BY:
            found.append(Hazard(name, gap, False))

    if not found:
        return None
    return min(found, key=lambda hazard: hazard.metres)


def hazard_call(hazard: Hazard | None) -> str | None:
    """What to say about what is up the road."""
    if hazard is None:
        return None
    return "car stopped ahead" if hazard.stopped else "slower car ahead"


def counts(neighbours: list[Alongside]) -> dict[str, int]:
    """side -> how many cars are on it.

    The same shape a sim that does its own spotting reports in, so both routes
    hand the notification one thing and it never has to know which sim it is
    talking to.
    """
    tally: dict[str, int] = {}
    for car in neighbours:
        tally[car.side] = tally.get(car.side, 0) + 1
    return tally


def warning(side: str, count: int) -> str:
    """The standing call for a side that still has somebody on it.

    What the repeat timer re-says. Counts them, because two cars stacked down
    one side is a different problem from one.
    """
    return f"two cars {side}" if count > 1 else f"car {side}"
