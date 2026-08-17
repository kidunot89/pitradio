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

#: How far apart two cars may be along the track and still count as alongside.
#: A prototype is about 5m long, so this is roughly overlapping bodywork plus
#: the length of a car either way — the range where somebody is actually there.
DEFAULT_ALONGSIDE_METRES = 9.0

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
) -> list[Alongside]:
    """Every car beside this one, nearest first.

    `others` is driver name -> world position, which is exactly what
    `SessionInfo.positions()` already returns for proximity voice.
    """
    if facing is None:
        return []

    found: list[Alongside] = []
    for driver, position in (others or {}).items():
        if not driver:
            continue
        along, across = offsets(mine, facing, position)
        if abs(along) > metres or abs(across) > width:
            continue
        if abs(across) < 0.5:
            # Directly in front or behind at overlapping distance means the
            # positions came from different moments, not that somebody is
            # inside the car. Nothing useful can be said about it.
            continue
        side = RIGHT if (across > 0) != swap else LEFT
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
        return "cars both sides"
    side = neighbours[0].side
    if len(neighbours) > 1:
        return f"two cars {side}"
    return f"car {side}"


def occupied(neighbours: list[Alongside]) -> frozenset[str]:
    """Which sides currently have somebody on them."""
    return frozenset(car.side for car in neighbours)


def calls(
    now: frozenset[str], before: frozenset[str]
) -> list[tuple[str, str, bool]]:
    """(side, what to say, whether it is urgent), for what changed.

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
    changed: list[tuple[str, str, bool]] = []
    for side in (LEFT, RIGHT):
        was, is_now = side in before, side in now
        if is_now and not was:
            changed.append((side, f"car {side}", True))
        elif was and not is_now:
            changed.append((side, f"clear {side}", False))
    return changed


def warning(side: str, neighbours: list[Alongside]) -> str:
    """The standing call for a side that still has somebody on it.

    What the repeat timer re-says. Counts them, because two cars stacked down
    one side is a different problem from one.
    """
    count = sum(1 for car in neighbours if car.side == side)
    return f"two cars {side}" if count > 1 else f"car {side}"
