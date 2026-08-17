"""Whether it is safe to rejoin, and when it will be.

Spun, stopped, facing the wrong way: the question is not "is anything beside me
now" — nothing is, you are off the track — it is **"will anything reach me
before I am up to speed"**. Those are different questions and answering the
first is how a driver gets collected pulling out.

The difference is acceleration. Stationary on the exit of a corner with a car
two hundred metres back closing at sixty metres a second, the naive answer is
three seconds of clear track, which sounds like plenty. But a GT car takes the
better part of five seconds to reach a speed where it is not a moving
chicane — so for most of that window you are slow, on the racing line, and the
car behind is arriving at a closing speed nobody planned for. The gap needed is
not the gap to the car; it is the gap to the car *minus how long you need*.

**Everything here is pure and deliberately conservative.** A rejoin call that
is wrong in the optimistic direction puts somebody in the wall, so every
uncertainty resolves towards waiting: an unknown speed is treated as fast, an
unknown gap as small, and a car whose closing speed cannot be worked out is
assumed to be arriving.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Metres per second squared, from a standing start on track. Deliberately
#: pessimistic: a GT3 car does rather better than this, and being wrong in that
#: direction means waiting a moment longer than necessary rather than pulling
#: out in front of somebody.
ACCELERATION = 6.0

#: The speed at which a rejoining car stops being a hazard — roughly the pace
#: of a slow corner. Reaching *racing* speed is not the bar; being fast enough
#: that a closing car can deal with you is.
SAFE_SPEED = 30.0

#: Added to every calculation, for reaction time and for the fact that the
#: numbers underneath are a sim's five-times-a-second view of the world.
MARGIN_SECONDS = 1.5

#: Cars further back than this are not part of the decision. Beyond it the
#: answer would change several times before it mattered.
LOOK_BACK_METRES = 500.0


def time_to_safe(speed: float, *, acceleration: float = ACCELERATION,
                 target: float = SAFE_SPEED) -> float:
    """Seconds from the current speed to one that is no longer a hazard."""
    speed = max(0.0, float(speed))
    if speed >= target:
        return 0.0
    return (target - speed) / max(0.1, acceleration)


@dataclass(frozen=True)
class Approach:
    """A car coming up behind, and how long until it arrives."""

    driver: str
    metres: float
    closing: float
    seconds: float


def approaching(own, cars, *, track_length: float = 0.0,
                look_back: float = LOOK_BACK_METRES) -> list[Approach]:
    """Everything behind that is closing, soonest first.

    Measured along the track rather than through the air, for the same reason
    the hazard check is: two cars either side of a hairpin are metres apart in
    space and nowhere near each other on the road.

    A car going slower than you is not approaching and is left out — it is not
    going to arrive, and including it would make every rejoin wait for traffic
    that is falling behind.
    """
    if own is None:
        return []
    mine = float(getattr(own, "lap_dist", 0.0) or 0.0)
    my_speed = max(0.0, float(getattr(own, "speed", 0.0) or 0.0))

    found: list[Approach] = []
    for car in cars or ():
        name = getattr(car, "driver", "") or ""
        if not name or name == getattr(own, "driver", ""):
            continue
        if getattr(car, "in_pits", False):
            continue

        gap = mine - float(getattr(car, "lap_dist", 0.0) or 0.0)
        if gap < 0 and track_length > 0:
            # They are behind you round the lap, not ahead.
            gap += track_length
        if not 0.0 < gap <= look_back:
            continue

        closing = max(0.0, float(getattr(car, "speed", 0.0) or 0.0)) - my_speed
        if closing <= 0.1:
            # Not gaining. They will not arrive, and waiting for them would
            # mean waiting for the whole field every time.
            continue
        found.append(Approach(name, gap, closing, gap / closing))

    found.sort(key=lambda entry: entry.seconds)
    return found


@dataclass(frozen=True)
class Verdict:
    """Whether to go, and why not if not."""

    clear: bool
    #: Seconds until the nearest car arrives, or None when nothing is coming.
    seconds: float | None = None
    #: How long the car needs before it is no longer a hazard.
    needed: float = 0.0
    driver: str = ""

    @property
    def waiting_for(self) -> float:
        """Seconds still to wait, as far as anyone can tell."""
        if self.clear or self.seconds is None:
            return 0.0
        return max(0.0, self.needed - self.seconds)


def safe_to_rejoin(own, cars, *, track_length: float = 0.0,
                   margin: float = MARGIN_SECONDS,
                   acceleration: float = ACCELERATION) -> Verdict:
    """Whether there is room to pull out and get going.

    The comparison is **time to be safe** against **time until the next car
    arrives**, not distance against distance. A stationary car needs the whole
    of its acceleration back before the first arrival, plus a margin.
    """
    needed = time_to_safe(getattr(own, "speed", 0.0),
                          acceleration=acceleration) + margin
    coming = approaching(own, cars, track_length=track_length)
    if not coming:
        return Verdict(True, None, needed)

    nearest = coming[0]
    return Verdict(nearest.seconds >= needed, nearest.seconds, needed,
                   nearest.driver)
