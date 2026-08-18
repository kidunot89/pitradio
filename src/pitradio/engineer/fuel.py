"""How much fuel it takes to reach the end, and what that is as a tank fill.

The question a driver actually asks on the way to the pit entry is not "how
many litres" — the sim's fuel screen is a **percentage of the tank**, and that
is the number they have to dial in with about four seconds to do it. So the
answer is a percentage, and the litres are the working.

**Consumption is measured, never assumed.** A car's fuel use depends on the
circuit, the fuel map, the traffic and how the person is driving it, and a
figure from anywhere else is a confident number that puts somebody out of fuel
on the last lap. Litres per lap here is what *this* car has been using over
*these* laps, and until it has done a lap there is no answer — which is said,
rather than covered up with a guess.

**Everything rounds towards more fuel.** Running out is a retirement and
carrying half a litre too much is a tenth a lap, so every uncertainty resolves
the same way: laps remaining round up, the margin is added rather than trimmed,
and a fill that comes out above the tank's capacity is reported as full rather
than quietly clipped and presented as though it would reach the end.

Pure arithmetic. No sim, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: How many recent laps the burn rate is averaged over.
#:
#: Enough to ride out one lap spent behind a slow car, few enough to follow a
#: change of fuel map or the tyres coming in. A whole-stint average is the
#: wrong answer after anything changes, and a single lap is noise.
RECENT_LAPS = 5

#: Litres carried over and above the calculation.
#:
#: For the lap you are on when the flag falls, the pit entry and exit that the
#: lap count does not include, and the fact that the last lap of a race is
#: rarely the most economical one anybody has driven. Crew Chief carries a
#: comparable cushion for the same reasons.
MARGIN_LITRES = 1.0

#: A lap time below this is not a lap, and dividing a race's remaining seconds
#: by it produces a lap count that is nonsense.
MIN_LAP_SECONDS = 20.0


@dataclass(frozen=True)
class Need:
    """What to put in, and everything needed to say why."""

    #: Litres to have on board leaving the pit box.
    litres: float
    #: As a percentage of the tank, which is what the sim's own screen asks
    #: for. Never above 100 — see `capped`.
    percent: float
    #: Laps still to run after the stop.
    laps: float
    #: Litres a lap, as measured.
    per_lap: float
    #: Whether the honest answer was more than the tank holds. **Not a
    #: rounding detail**: it means this stop cannot be the last one, and a
    #: driver told "one hundred percent" without being told that will plan a
    #: race that does not work.
    capped: bool = False


class Usage:
    """What this car burns a lap, learned by watching the tank.

    One reading per completed lap, kept as a short history. Refuelling shows up
    as the tank going *up*, which is not consumption and is discarded — without
    that a pit stop would record a negative lap and drag the average to
    something that says the car uses no fuel at all.
    """

    def __init__(self, recent: int = RECENT_LAPS) -> None:
        self._recent = max(1, recent)
        self._laps: list[float] = []
        self._last_lap: int = -1
        self._last_fuel: float = 0.0

    def reset(self) -> None:
        self._laps.clear()
        self._last_lap = -1
        self._last_fuel = 0.0

    def observe(self, laps: int, fuel: float) -> None:
        """Record the tank at this moment. Only lap changes count for anything."""
        laps, fuel = int(laps), float(fuel)
        if fuel <= 0:
            # The sim is not publishing it, or the car is not on track yet.
            return
        if self._last_lap < 0 or laps < self._last_lap:
            # First look, or the session restarted.
            self._last_lap, self._last_fuel = laps, fuel
            self._laps.clear()
            return
        if laps == self._last_lap:
            return

        used = self._last_fuel - fuel
        self._last_lap, self._last_fuel = laps, fuel
        if used <= 0:
            # The tank went up: a pit stop, or a session reset. Not a lap's
            # consumption, and averaging it in would say the car burns nothing.
            return
        self._laps.append(used)
        del self._laps[:-self._recent]

    @property
    def per_lap(self) -> float:
        """Litres a lap, or zero until a lap has been completed."""
        return sum(self._laps) / len(self._laps) if self._laps else 0.0

    @property
    def laps_measured(self) -> int:
        return len(self._laps)


def laps_left(*, laps_done: int, max_laps: int = 0, elapsed: float = 0.0,
              ends_at: float = 0.0, lap_time: float = 0.0) -> float:
    """Laps still to run, or 0 when it cannot be worked out.

    Two kinds of race and they are answered differently. A lap race subtracts;
    a timed one divides what is left of the clock by a lap time and **rounds
    up**, because the flag falls at the end of the lap you are on when the
    clock runs out — so the part-lap remaining is a whole lap of fuel.

    `max_laps` is trusted only when it is a plausible number. LMU writes
    `INT_MAX` there for a timed session, and taking that at face value would
    ask for two billion laps' worth of fuel.
    """
    if 0 < max_laps < 10000:
        return float(max(0, max_laps - max(0, laps_done)))
    remaining = ends_at - elapsed
    if remaining <= 0 or lap_time < MIN_LAP_SECONDS:
        return 0.0
    return float(math.ceil(remaining / lap_time))


def needed(*, remaining: float, pit_in: float, per_lap: float,
           capacity: float, margin: float = MARGIN_LITRES) -> Need | None:
    """The fill for a stop `pit_in` laps from now, or None if it cannot be said.

    `pit_in` of 1 is "next time round". The laps that matter are the ones
    *after* the stop: fuel already in the tank covers the ones before it, which
    is why this does not need to know how much is in there now.

    None rather than a guess when consumption has not been measured or the race
    length is unknown. A fuel number invented from nothing is the one kind of
    wrong answer here that ends somebody's race.
    """
    if per_lap <= 0 or remaining <= 0 or capacity <= 0:
        return None

    after = max(0.0, remaining - max(0.0, pit_in))
    litres = after * per_lap + margin
    percent = litres / capacity * 100.0
    if percent >= 100.0:
        # **Said, not silently clipped.** A tank that will not reach the end
        # means this cannot be the last stop, and a driver told "one hundred
        # percent" without being told that plans a race that does not work.
        return Need(capacity, 100.0, after, per_lap, capped=True)
    # Up to the next whole percent, for the same reason everything else here
    # rounds that way, and because the sim's own screen is in whole percent.
    return Need(litres, float(math.ceil(percent)), after, per_lap)
