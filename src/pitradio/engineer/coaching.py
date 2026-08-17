"""Comparing one lap against another, corner by corner.

The whole feature in one sentence: record where every car was and how fast it
was going, keep each driver's quickest lap, and when the driver goes through a
corner say how the two compare.

Three decisions carry it, and each is the reason something else is simple:

* **Corners are found in the data, not looked up.** There is no track map here
  and there is not going to be one: it would need a file per circuit, would go
  stale with every layout change, and would leave the feature working on the
  four tracks somebody got round to. A corner is a place where the quick lap
  slowed down and sped up again, which is true on every circuit in every sim.
  The cost is that they are numbered rather than named — "turn four", not
  "Arnage".

* **Time is read off the trace, never integrated.** Each sample carries the
  clock as well as the distance, so the time through a stretch of track is one
  subtraction between two interpolated points. Integrating ds/v instead would
  accumulate the error of every sample in between, and at the sample rate the
  sim actually publishes that error is larger than the differences being
  reported.

* **Silence is the default.** A corner where the two laps agree produces
  nothing. An engineer that says something at every corner is one the driver
  turns off in three laps, and the delta that matters is the one that is not
  like the others.

Pure, and deliberately: every case in here is one that would otherwise only
turn up mid-stint with a wheel in your hands.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

#: Below this a car is stationary or crawling, and dividing anything by it
#: produces nonsense. Also what "in the pits" looks like from the outside.
CRAWL_SPEED = 3.0

#: How far apart two samples may be before the stretch between them is not
#: worth timing. The scoring block updates a handful of times a second, so a
#: car at 300km/h moves ~15m between samples; 60m means several updates were
#: missed and the interpolation would be a guess.
MAX_SAMPLE_GAP = 60.0


@dataclass(frozen=True)
class Sample:
    """One reading: how far round, how long into the lap, how fast."""

    distance: float
    elapsed: float
    speed: float


@dataclass
class LapTrace:
    """One lap by one driver, as a series of samples along the track.

    Samples are kept in distance order and strictly increasing, which is what
    lets everything else use a bisect rather than a scan. A reading that goes
    backwards — the sim correcting itself, or a car being teleported to the
    pits — is dropped rather than reordered: it is not a lap any more, and
    keeping it would silently compare against a different piece of track.
    """

    driver: str = ""
    lap_time: float = 0.0
    samples: list[Sample] = field(default_factory=list)

    def add(self, distance: float, elapsed: float, speed: float) -> bool:
        """Record a reading. False if it was not usable, which is normal."""
        if self.samples and distance <= self.samples[-1].distance:
            return False
        self.samples.append(Sample(float(distance), float(elapsed), float(speed)))
        return True

    @property
    def start(self) -> float:
        return self.samples[0].distance if self.samples else 0.0

    @property
    def end(self) -> float:
        return self.samples[-1].distance if self.samples else 0.0

    def covers(self, begin: float, finish: float) -> bool:
        """Whether both ends of a stretch fall inside what was recorded.

        A trace that starts halfway round — the driver joined mid-lap, or the
        sim was minimised — must not answer questions about the half it never
        saw. It would answer them plausibly, which is the problem.
        """
        return bool(self.samples) and self.start <= begin and finish <= self.end

    def _interpolate(self, distance: float, attribute: str) -> float | None:
        """A value at a distance, between the two samples either side of it."""
        if len(self.samples) < 2 or not self.covers(distance, distance):
            return None

        distances = [sample.distance for sample in self.samples]
        index = bisect.bisect_left(distances, distance)
        if index == 0:
            return getattr(self.samples[0], attribute)

        before, after = self.samples[index - 1], self.samples[index]
        span = after.distance - before.distance
        if span <= 0:
            return getattr(self.samples[index], attribute)
        if span > MAX_SAMPLE_GAP:
            # A hole in the recording. Interpolating across it would invent a
            # time for a stretch of track nothing was ever measured on.
            return None

        fraction = (distance - before.distance) / span
        low, high = getattr(before, attribute), getattr(after, attribute)
        return low + (high - low) * fraction

    def time_at(self, distance: float) -> float | None:
        return self._interpolate(distance, "elapsed")

    def speed_at(self, distance: float) -> float | None:
        return self._interpolate(distance, "speed")

    def time_between(self, begin: float, finish: float) -> float | None:
        """Seconds spent between two points on the track, or None.

        None whenever it cannot be answered honestly — the trace does not reach
        that far, or there is a gap in the middle of it. Every caller treats
        that as "say nothing", which is the only safe thing to do with a number
        that would sound exactly as authoritative as a real one.
        """
        started, finished = self.time_at(begin), self.time_at(finish)
        if started is None or finished is None or finished < started:
            return None
        return finished - started

    def __bool__(self) -> bool:
        return bool(self.samples)


@dataclass(frozen=True)
class Corner:
    """A place the quick lap slowed down for, and the stretch either side.

    `entry` is where the braking began, `apex` the slowest point, `exit` where
    the speed had recovered. Entry and exit are compared separately because
    they are separate mistakes: braking too late and getting on the power too
    early feel identical from the seat and are fixed by opposite things.
    """

    number: int
    entry: float
    apex: float
    exit: float

    @property
    def length(self) -> float:
        return self.exit - self.entry


def _grid(trace: LapTrace, step: float) -> list[tuple[float, float]]:
    """(distance, speed) resampled evenly, so window sizes mean metres.

    Sampling straight from the trace instead would make every window depend on
    how fast the car was going when the sim happened to publish, and corner
    detection would find different corners on a quick lap than a slow one.

    A hole in the recording drops out rather than being invented, so the grid
    is even in distance except across a gap. A corner found either side of one
    can have a mis-sized entry or exit window; it cannot produce a wrong time,
    because `time_between` refuses to interpolate over the same gap.
    """
    if len(trace.samples) < 2:
        return []
    points: list[tuple[float, float]] = []
    distance = trace.start
    while distance <= trace.end:
        speed = trace.speed_at(distance)
        if speed is not None:
            points.append((distance, speed))
        distance += step
    return points


def _crest(speeds: list[float], index: int, direction: int, reach: int) -> int:
    """Walk out from an apex to where the car stopped speeding up.

    That is where braking began on one side and where the corner was done with
    on the other — which is a better answer than a fixed number of metres,
    because a hairpin and a fast sweeper do not have the same shape. Capped at
    `reach` so a long straight does not put the entry point half a lap back.
    """
    furthest = index
    for step in range(1, reach + 1):
        probe = index + direction * step
        if probe < 0 or probe >= len(speeds):
            break
        if speeds[probe] < speeds[furthest]:
            break
        furthest = probe
    return furthest


def find_corners(
    trace: LapTrace,
    *,
    step: float = 5.0,
    window: float = 150.0,
    min_drop: float = 8.0,
    min_gap: float = 120.0,
) -> list[Corner]:
    """Every corner in a lap, numbered from the start line.

    A point counts as a corner when it is the slowest thing within `window`
    metres either side *and* the car was at least `min_drop` m/s faster on both
    sides of it. The second half is what keeps a fast kink from being called a
    corner: a chicane at Monza drops 40 m/s, the kink before Blanchimont drops
    almost nothing, and only one of them is somewhere a driver loses time.

    `min_gap` merges the halves of a chicane into one corner. Two calls two
    hundred milliseconds apart are not two pieces of information.
    """
    points = _grid(trace, step)
    if len(points) < 3:
        return []

    reach = max(1, int(window / step))
    speeds = [speed for _distance, speed in points]

    apexes: list[int] = []
    for index in range(reach, len(points) - reach):
        speed = speeds[index]
        before = speeds[index - reach:index]
        after = speeds[index + 1:index + 1 + reach]
        if not before or not after:
            continue
        if speed > min(before) or speed > min(after):
            continue
        if max(before) - speed < min_drop or max(after) - speed < min_drop:
            continue
        apexes.append(index)

    # Merged first and numbered afterwards, so turn four is turn four however
    # the two halves of the chicane before it happened to be detected.
    merged: list[int] = []
    for index in apexes:
        if merged and points[index][0] - points[merged[-1]][0] < min_gap:
            # The other half of a chicane, or the same slow point found twice.
            # The slower of the two is the one a driver would call the corner.
            if speeds[index] < speeds[merged[-1]]:
                merged[-1] = index
            continue
        merged.append(index)

    return [
        Corner(
            number,
            points[_crest(speeds, index, -1, reach)][0],
            points[index][0],
            points[_crest(speeds, index, +1, reach)][0],
        )
        for number, index in enumerate(merged, 1)
    ]


#: The two halves of a corner, as the engineer names them.
ENTRY = "entry"
EXIT = "exit"


@dataclass(frozen=True)
class PhaseDelta:
    """How the two laps compared through half a corner.

    `seconds` is the driver's time minus the target's, so **positive means the
    target was quicker**. Signed rather than absolute because the interesting
    call is often the other way round — telling somebody they are already
    faster somewhere is worth as much as telling them they are not.
    """

    phase: str
    seconds: float

    @property
    def lost(self) -> bool:
        return self.seconds > 0


def compare_corner(
    driver: LapTrace, target: LapTrace, corner: Corner
) -> list[PhaseDelta]:
    """Entry and exit deltas for one corner, or nothing if either lap is short.

    Both traces have to reach across the whole corner. Comparing what one of
    them recorded against a stretch the other never saw is how a coaching tool
    ends up confidently telling somebody about a corner they were in the pits
    for.
    """
    deltas: list[PhaseDelta] = []
    for phase, begin, finish in (
        (ENTRY, corner.entry, corner.apex),
        (EXIT, corner.apex, corner.exit),
    ):
        mine = driver.time_between(begin, finish)
        theirs = target.time_between(begin, finish)
        if mine is None or theirs is None:
            continue
        deltas.append(PhaseDelta(phase, mine - theirs))
    return deltas


def worst(deltas: list[PhaseDelta], threshold: float) -> PhaseDelta | None:
    """The half of the corner worth mentioning, if either is.

    One call per corner, never two. "You lost a tenth on entry and gained a
    tenth on exit" is a sentence that arrives while the driver is already
    braking for the next one, and the half they can do something about is the
    one that cost the most.
    """
    if not deltas:
        return None
    candidate = max(deltas, key=lambda delta: abs(delta.seconds))
    return candidate if abs(candidate.seconds) >= threshold else None


class LapBook:
    """Everybody's lap in progress, and everybody's best one so far.

    Fed a snapshot at a time from whatever is polling the sim, which keeps this
    testable with a list of made-up cars and no sim at all.

    A lap is kept only when the sim agrees it was one: it ends at the line with
    a lap time attached, it was recorded from near enough the start, and the
    car was never in the pits during it. A quick lap that was actually a
    shortcut through the pit lane would otherwise become the target everybody
    is measured against, and nothing about it would look wrong.
    """

    #: How much of the lap must have been recorded for it to be usable as a
    #: target. Joining a session mid-lap is normal; comparing against the two
    #: thirds of a lap somebody happened to catch is not.
    COVERAGE = 0.9

    def __init__(self, track_length: float = 0.0) -> None:
        self.track_length = float(track_length or 0.0)
        self.current: dict[str, LapTrace] = {}
        self.best: dict[str, LapTrace] = {}
        #: Drivers whose lap in progress touched the pit lane. Cleared when the
        #: next lap starts.
        self._tainted: set[str] = set()
        self._laps: dict[str, int] = {}
        #: driver -> the class they are racing in. Kept because "the fastest
        #: lap" means something different on a multi-class grid: a GT3 driver
        #: is not racing the Hypercars and their lap time is not news.
        self.classes: dict[str, str] = {}

    def reset(self, track_length: float = 0.0) -> None:
        """Start again — a new track, or a new session on the same one.

        Best laps do not survive it. They are distances into *this* circuit,
        and carrying them over would compare Le Mans against Sebring using the
        same numbers, which produces a confident answer about nothing.
        """
        self.track_length = float(track_length or 0.0)
        self.current.clear()
        self.best.clear()
        self._tainted.clear()
        self._laps.clear()
        self.classes.clear()

    def observe(self, car, elapsed: float) -> LapTrace | None:
        """Record where a car is. Returns the lap it just finished, if it did.

        `car` is a `plugins.base.Car`; only the fields that exist off Windows
        are touched, so this can be driven from a plain object in a test.
        """
        driver = getattr(car, "driver", "") or ""
        if not driver:
            return None

        vehicle_class = str(getattr(car, "vehicle_class", "") or "")
        if vehicle_class:
            self.classes[driver] = vehicle_class

        laps = int(getattr(car, "laps", 0) or 0)
        previous = self._laps.get(driver)
        self._laps[driver] = laps

        finished: LapTrace | None = None
        if previous is not None and laps > previous:
            finished = self._complete(driver, float(getattr(car, "last_lap", 0.0) or 0.0))

        if getattr(car, "in_pits", False):
            self._tainted.add(driver)

        trace = self.current.setdefault(driver, LapTrace(driver))
        speed = float(getattr(car, "speed", 0.0) or 0.0)
        distance = float(getattr(car, "lap_dist", 0.0) or 0.0)
        if speed > CRAWL_SPEED:
            trace.add(distance, float(elapsed), speed)
        return finished

    def _complete(self, driver: str, lap_time: float) -> LapTrace | None:
        """Close off a lap and keep it if it beats what that driver had."""
        trace = self.current.pop(driver, None)
        tainted = driver in self._tainted
        self._tainted.discard(driver)

        if trace is None or not trace.samples or lap_time <= 0 or tainted:
            return None

        trace.lap_time = lap_time
        if self.track_length > 0:
            covered = trace.end - trace.start
            if covered < self.track_length * self.COVERAGE:
                return None

        best = self.best.get(driver)
        if best is None or lap_time < best.lap_time:
            self.best[driver] = trace
        return trace

    def best_for(self, driver: str) -> LapTrace | None:
        return self.best.get(driver)

    def fastest(self, vehicle_class: str = "") -> LapTrace | None:
        """The quickest lap on record, optionally within one class.

        A class filter rather than always overall, because on an endurance grid
        the two are different facts and only one of them is about your race.
        Naming a class nobody is in yields nothing rather than falling back to
        overall — a wrong answer stated confidently is worse than no answer.
        """
        laps = [trace for driver, trace in self.best.items()
                if trace.lap_time > 0
                and (not vehicle_class or self.classes.get(driver) == vehicle_class)]
        return min(laps, key=lambda trace: trace.lap_time) if laps else None

    def class_of(self, driver: str) -> str:
        return self.classes.get(driver, "")
