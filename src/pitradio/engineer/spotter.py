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
#: Crew Chief's model, and its numbers: `spotter_car_length` ships at 4.5 for
#: Le Mans Ultimate, 5 for Project CARS 2 and ACC, 4.4 for Automobilista 2 —
#: which is why these are plugin settings rather than constants. Two cars are
#: alongside when their *bodywork* overlaps, and that is a fact about how long
#: they are, not an arbitrary radius.
DEFAULT_CAR_LENGTH = 4.5
DEFAULT_CAR_WIDTH = 1.8

#: How much daylight there has to be before a side is clear, in metres past
#: the bodywork. Crew Chief's `spotter_gap_for_clear`, and its default.
#:
#: A gap rather than a second multiple of the car length. The distinction
#: matters at the extremes: for a kart, "a further car length" is two metres of
#: hysteresis and the call hangs on far too long; half a metre of daylight is
#: half a metre of daylight whatever you are driving.
GAP_FOR_CLEAR = 0.5

#: Two cars side by side have their centres about a car width apart. Nearer
#: than this and they are on the same line — one following the other.
MIN_LATERAL_WIDTHS = 0.9

#: And beyond a few widths they are on another part of the circuit.
MAX_LATERAL_WIDTHS = 4.0

#: Below this, in metres per second, the spotter says nothing at all. Crew
#: Chief's `min_speed_for_spotter`, and its default of 10.
#:
#: Which is 36km/h, and higher than it first looks — deliberately. Below it you
#: are in the pit lane, on the grid, or crawling out of a spin, and in all
#: three the cars around you are stationary or passing at walking pace. A
#: spotter that calls those is a spotter nobody leaves switched on.
MIN_SPEED = 10.0

#: And a car closing faster than this is not alongside, it is going past.
#: Crew Chief's `max_closing_speed_for_spotter`, default 12.
#:
#: **This is the one that catches the lapping car.** Something arriving 12 m/s
#: quicker crosses the whole overlap window in under a second: by the time the
#: call is spoken they are gone, and the driver holds a line for a car that is
#: no longer there. It is also every car you go past in the pit exit.
MAX_CLOSING_SPEED = 12.0

#: Below this the heading taken from two positions is noise rather than a
#: direction.
#:
#: Half a metre was far too little, and this is the fault underneath several
#: others. Through slow traffic consecutive reads are centimetres apart and the
#: direction between them is dominated by the sim's own rounding — so "forward"
#: swings about, and a car directly ahead resolves as a car directly beside.
#: Everything downstream depends on these axes being right.
MIN_HEADING_METRES = 3.0

#: How long an overlap has to last before it is called, and how long a side has
#: to be clear before the all-clear is. Crew Chief's `spotter_overlap_delay`
#: and `spotter_clear_delay`, in seconds rather than its milliseconds.
#:
#: **Both are needed and they are deliberately different.** Two cars at the
#: same corner cross in and out of overlap as they breathe, and without the
#: delays the spotter chatters. The overlap delay is the shorter of the two
#: because a warning that is late is worthless, while the all-clear can afford
#: to be sure — a driver who holds their line a tenth longer than necessary has
#: lost nothing.
OVERLAP_DELAY = 0.05
CLEAR_DELAY = 0.15

DEFAULT_OVERLAP_METRES = DEFAULT_CAR_LENGTH
DEFAULT_ALONGSIDE_METRES = DEFAULT_CAR_LENGTH + GAP_FOR_CLEAR
DEFAULT_WIDTH_METRES = DEFAULT_CAR_WIDTH * MAX_LATERAL_WIDTHS
MIN_LATERAL_METRES = DEFAULT_CAR_WIDTH * MIN_LATERAL_WIDTHS


def ranges(car_length: float = DEFAULT_CAR_LENGTH,
           car_width: float = DEFAULT_CAR_WIDTH,
           gap: float = GAP_FOR_CLEAR) -> dict[str, float]:
    """Every spotter distance, from the size of the cars.

    One place, so the four of them cannot drift apart — and so a sim whose
    cars are karts or trucks gets all four right by changing two numbers.
    """
    return {
        "overlap": car_length,
        "metres": car_length + gap,
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
    #: side, not two, and calling it three wide is worse than saying
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
    speeds: dict[str, float] | None = None,
    my_speed: float = 0.0,
    max_closing: float = MAX_CLOSING_SPEED,
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

    **A car closing faster than `max_closing` is not alongside, it is going
    past.** Crew Chief's `max_closing_speed_for_spotter`, and the rule that
    catches the lapping car: something arriving 12 m/s quicker crosses the
    whole overlap window in well under a second, so by the time the call has
    been spoken they have gone — and the driver holds a line for a car that is
    no longer there. `speeds` is optional; without it every car is treated as
    running alongside, which is what a caller with no speed data means.
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

        if speeds is not None:
            closing = abs(float(speeds.get(driver, my_speed)) - my_speed)
            if closing > max_closing:
                continue

        side = RIGHT if (across > 0) != swap else LEFT
        overlapping = abs(along) <= overlap
        if not overlapping and not (side in held and abs(along) <= metres):
            continue

        found.append(Alongside(driver, side, sideways, along, overlapping))

    found.sort(key=lambda car: abs(car.lateral))
    return found


#: Crew Chief's spotter vocabulary, taken from the sound folders in a local
#: installation rather than guessed at. Every one of these is a clip a voice
#: pack built for Crew Chief already contains, so a pack dropped in here speaks
#: them without a mapping.
#:
#: What is deliberately absent is the oval set — `car_inside`, `clear_outside`,
#: `three_wide_on_inside`. Which side is inside is a fact about the banking,
#: which none of the sims here publish and which Crew Chief keeps per-track. A
#: guess at it is a call that is confidently the wrong way round.
CAR_LEFT, CAR_RIGHT = "car left", "car right"
#: **Which side *you* are on**, not how many cars are beside you.
#:
#: This was "two cars left", which is the same fact stated the harder way
#: round: a driver hearing it has to work out where that leaves them, while
#: they are busy. Crew Chief says "three wide, you're on the right" when both
#: other cars are to your left, and that is directly actionable — it says which
#: way there is no room.
THREE_WIDE_LEFT = "three wide you're on the left"
THREE_WIDE_RIGHT = "three wide you're on the right"
#: One car each side, so you are the filling.
IN_THE_MIDDLE = "in the middle"
#: The standing call while somebody is overlapping on **both** sides, which is
#: a different instruction from "still there" on one: do not move at all.
HOLD_YOUR_LINE = "hold your line"


def call(neighbours: list[Alongside]) -> str | None:
    """What the spotter says about them, or nothing.

    Deliberately not a driver name. At the moment a car is beside you the only
    thing worth hearing is which way not to turn, and a name is a syllable
    count nobody has time for.
    """
    beside = [car for car in neighbours if car.overlapping] or neighbours
    return arrival(counts(beside) if beside else {})


def arrival(tally: dict[str, int]) -> str | None:
    """The call for a situation, from how many cars are on each side.

    One function, because every one of these is a statement about the *same*
    thing — where you are and where the room is — and splitting it across the
    caller is how "two cars left" and "three wide" ended up disagreeing about
    what to say when there were two on one side and one on the other.
    """
    left, right = tally.get(LEFT, 0), tally.get(RIGHT, 0)
    if not left and not right:
        return None
    if left and right:
        # Somebody either side. Which is the one call that is about *you*
        # rather than about them: there is nowhere to go.
        return IN_THE_MIDDLE
    if left > 1:
        return THREE_WIDE_RIGHT
    if right > 1:
        return THREE_WIDE_LEFT
    return CAR_LEFT if left else CAR_RIGHT


def occupied(neighbours: list[Alongside]) -> frozenset[str]:
    """Which sides currently have somebody on them."""
    return frozenset(car.side for car in neighbours)


def calls(
    now: dict[str, int] | frozenset[str], before: frozenset[str]
) -> list[tuple[str, str, bool]]:
    """(side, what to say, whether it is urgent), for what changed.

    **Both directions.** A spotter that only says "car left" leaves the driver
    holding a line they no longer need to hold, waiting for a call that never
    comes — which is worse than not being told in the first place, because they
    are now deliberately not using a piece of track. So a side going clear is a
    call in its own right.

    Both are urgent. The all-clear was ranked below the warning on the
    reasoning that only a warning can arrive too late to matter, and that is
    wrong from the seat: a driver holding a line for a car that left two
    corners ago is giving up track they could be using, and they hold it until
    they are told otherwise.

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

    arrived = [side for side in (LEFT, RIGHT)
               if side in tally and side not in before]
    changed: list[tuple[str, str, bool]] = []
    if arrived:
        # One call for the arrival, whichever side or sides it was: the
        # situation is what is being described, not each car in turn.
        changed.append((arrived[0], arrival(tally) or "", True))
    for side in (LEFT, RIGHT):
        if side in before and side not in tally:
            changed.append((side, f"clear {side}", True))
    return [entry for entry in changed if entry[1]]


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


def standing(tally: dict[str, int]) -> tuple[str, str] | None:
    """(key, what to say) while the situation has not changed.

    The key is the **shape of the situation**, not the words, and that is the
    whole design. It has to stay the same for as long as the situation does, or
    the repeat interval never governs anything: a key that changed when the
    wording did would make the follow-up a brand new call and it would go out
    on the very next tick, a heartbeat after the arrival.

    Keying on the counts instead means one car becoming two *is* a new call,
    and goes out immediately — which is right, because it is news.

    Cars on both sides get `HOLD_YOUR_LINE` rather than `STILL_THERE`, because
    they are different instructions: one means do not move that way, and the
    other means do not move.
    """
    left, right = tally.get(LEFT, 0), tally.get(RIGHT, 0)
    if not left and not right:
        return None
    key = f"{left}:{right}"
    return key, (HOLD_YOUR_LINE if left and right else STILL_THERE)


def arrival_key(tally: dict[str, int]) -> str:
    """The key an arrival shares with the repeats that follow it."""
    return f"{tally.get(LEFT, 0)}:{tally.get(RIGHT, 0)}"


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
