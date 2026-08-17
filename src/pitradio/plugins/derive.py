"""Working out what a sim does not publish.

Sims are inconsistent about the most ordinary things. iRacing gives a speed for
the player's car and for nobody else; the Project CARS block carries none in
the part of it worth trusting. The engineer needs one for every car it records
a trace for — the lap book ignores a stationary car, so a speed left at zero
means no samples at all and a trainer that never sees a lap.

Pure, and shared, because the second sim that needed this would otherwise have
got a slightly different copy.
"""

from __future__ import annotations

#: Below this two reads are too close together for the difference between them
#: to be a speed rather than the noise on a distance.
MIN_DELTA_SECONDS = 0.05

#: Metres per second past which the reading is not a car. Formula machinery
#: tops out around 103 m/s and the fastest ovals a little over, so this leaves
#: room while still catching a car that was moved rather than driven.
MAX_SPEED = 130.0


class Speeds:
    """Per-car speed, from how far each moved between two reads.

    A speed trap rather than a speedometer: distance over time, which is
    exactly right on average and slightly behind through a corner.
    """

    def __init__(self) -> None:
        #: driver -> (clock, distance round the lap)
        self._seen: dict[str, tuple[float, float]] = {}

    def reset(self) -> None:
        self._seen.clear()

    def of(self, driver: str, elapsed: float, distance: float,
           length: float = 0.0) -> float:
        """That car's speed now, or zero when it cannot be said.

        `length` is the lap in metres, used only to make sense of the moment a
        car crosses the line and its distance drops to nearly nothing. Without
        it that sample is skipped instead — a tenth of a second of no speed
        once a lap, which costs nothing, and is much better than inventing one.
        """
        previous = self._seen.get(driver)
        self._seen[driver] = (elapsed, distance)
        if previous is None:
            return 0.0

        was_at, was_distance = previous
        span = elapsed - was_at
        if span < MIN_DELTA_SECONDS:
            return 0.0

        moved = distance - was_distance
        if moved < 0:
            if length <= 0:
                return 0.0
            # Crossed the line: what was left of the old lap plus what has been
            # done of the new one.
            moved += length

        # Sanity, not arithmetic. A car sent to the pits, a session restart or
        # a tow all jump the distance by hundreds of metres between two reads,
        # and every one of them comes out of the subtraction as a well-formed
        # enormous speed. Checking the *speed* catches them whichever direction
        # they jumped: a teleport from 4000m to 100m wraps to 1100m, which read
        # as 1100 metres per second.
        speed = moved / span
        return speed if 0.0 <= speed <= MAX_SPEED else 0.0
