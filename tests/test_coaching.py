"""Lap traces, finding corners in them, and comparing two laps.

This is the part of the engineer that makes a claim about somebody's driving,
so it is the part most worth being able to check without a car. Every lap below
is built from a speed profile rather than recorded, which means a corner can be
put exactly where the test wants it and the answer is knowable in advance.

The recurring theme is refusing to answer. A trace that does not reach across a
corner, a gap in the middle of one, a lap that went through the pits — all of
them produce nothing rather than a plausible number, because a plausible number
here is indistinguishable from a real one and would be acted on.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pitradio.engineer import coaching

TRACK = 1200.0
CORNERS = (300.0, 800.0)
TOP_SPEED = 60.0
APEX_SPEED = 20.0
#: How long the braking and acceleration ramps are, either side of an apex.
RAMP = 120.0


def speed_at(distance: float, corners=CORNERS, scale: float = 1.0,
             slow_between: tuple[float, float] | None = None) -> float:
    """A plausible speed profile: flat out, dipping to an apex at each corner."""
    nearest = min(abs(distance - corner) for corner in corners)
    dip = max(0.0, 1.0 - nearest / RAMP)
    speed = TOP_SPEED - (TOP_SPEED - APEX_SPEED) * dip
    # No window means the whole lap is scaled; a window scales only inside it.
    if slow_between is None or slow_between[0] <= distance <= slow_between[1]:
        speed *= scale
    return speed


def build(step: float = 5.0, **kwargs) -> coaching.LapTrace:
    """A whole lap, with the clock integrated from the speed profile."""
    trace = coaching.LapTrace("Reference")
    elapsed = 0.0
    distance = 0.0
    while distance <= TRACK:
        speed = speed_at(distance, **kwargs)
        trace.add(distance, elapsed, speed)
        elapsed += step / speed
        distance += step
    trace.lap_time = elapsed
    return trace


# -- the trace ------------------------------------------------------------


def test_samples_that_go_backwards_are_dropped():
    """The sim correcting itself, or a car being sent to the pits. Reordering
    would silently compare against a different piece of track."""
    trace = coaching.LapTrace()
    assert trace.add(100.0, 1.0, 50.0) is True
    assert trace.add(90.0, 1.1, 50.0) is False
    assert [sample.distance for sample in trace.samples] == [100.0]


def test_time_between_two_points_is_read_off_the_clock():
    trace = build()
    # 100m of flat-out running at the top speed, which is knowable exactly.
    taken = trace.time_between(20.0, 120.0)
    assert taken == pytest.approx(100.0 / TOP_SPEED, rel=0.02)


def test_a_stretch_the_trace_never_covered_has_no_answer():
    """Joining mid-lap must not produce an opinion about the half you missed."""
    trace = coaching.LapTrace()
    for distance in range(600, 800, 5):
        trace.add(float(distance), distance / 50.0, 50.0)
    assert trace.time_between(100.0, 200.0) is None
    assert trace.covers(100.0, 200.0) is False


def test_a_hole_in_the_recording_is_not_interpolated_over():
    """A gap means nothing was measured there. Filling it in invents a time for
    a stretch of track nobody drove past the app."""
    trace = coaching.LapTrace()
    trace.add(0.0, 0.0, 50.0)
    trace.add(400.0, 8.0, 50.0)      # far beyond MAX_SAMPLE_GAP
    assert trace.time_at(200.0) is None
    assert trace.time_between(0.0, 200.0) is None


# -- finding corners ------------------------------------------------------


def test_corners_are_found_where_the_lap_slowed_down():
    corners = coaching.find_corners(build())
    assert len(corners) == 2
    assert corners[0].apex == pytest.approx(CORNERS[0], abs=10.0)
    assert corners[1].apex == pytest.approx(CORNERS[1], abs=10.0)


def test_corners_are_numbered_from_the_line():
    corners = coaching.find_corners(build())
    assert [corner.number for corner in corners] == [1, 2]


def test_entry_comes_before_the_apex_and_exit_after():
    for corner in coaching.find_corners(build()):
        assert corner.entry < corner.apex < corner.exit


def test_a_kink_is_not_a_corner():
    """A fast bend loses nobody any time, and calling it turn three renumbers
    every corner after it."""
    shallow = coaching.LapTrace()
    elapsed, distance = 0.0, 0.0
    while distance <= TRACK:
        nearest = min(abs(distance - corner) for corner in CORNERS)
        # A 4 m/s dip, below the 8 m/s a corner has to be worth.
        speed = TOP_SPEED - 4.0 * max(0.0, 1.0 - nearest / RAMP)
        shallow.add(distance, elapsed, speed)
        elapsed += 5.0 / speed
        distance += 5.0
    assert coaching.find_corners(shallow) == []


def test_the_two_halves_of_a_chicane_are_one_corner():
    """Two calls two hundred milliseconds apart are not two pieces of
    information, and numbering them separately shifts every corner after."""
    chicane = build(corners=(400.0, 460.0))
    corners = coaching.find_corners(chicane, min_gap=120.0)
    assert len(corners) == 1


def test_a_trace_too_short_to_hold_a_corner_finds_none():
    trace = coaching.LapTrace()
    trace.add(0.0, 0.0, 50.0)
    assert coaching.find_corners(trace) == []


# -- comparing ------------------------------------------------------------


def test_a_slower_exit_shows_up_as_lost_time_on_the_exit():
    reference = build()
    # Same lap, 10% slower from the first apex to the end of its exit ramp.
    mine = build(scale=0.9, slow_between=(CORNERS[0], CORNERS[0] + RAMP))

    corner = coaching.find_corners(reference)[0]
    deltas = {delta.phase: delta.seconds
              for delta in coaching.compare_corner(mine, reference, corner)}

    assert deltas[coaching.EXIT] > 0.05
    assert abs(deltas[coaching.ENTRY]) < 0.01


def test_a_slower_entry_shows_up_on_the_entry():
    reference = build()
    # Stopping one sample short of the apex: a sample's speed is what the clock
    # is integrated with for the step *after* it, so a window that includes the
    # apex leaks into the exit.
    mine = build(scale=0.9, slow_between=(CORNERS[0] - RAMP, CORNERS[0] - 5.0))

    corner = coaching.find_corners(reference)[0]
    deltas = {delta.phase: delta.seconds
              for delta in coaching.compare_corner(mine, reference, corner)}

    assert deltas[coaching.ENTRY] > 0.05
    assert abs(deltas[coaching.EXIT]) < 0.01


def test_being_quicker_is_a_negative_delta():
    """Signed, not absolute: telling somebody they are already faster
    somewhere is worth as much as telling them they are not."""
    reference = build(scale=0.9)
    mine = build()
    corner = coaching.find_corners(reference)[0]
    deltas = coaching.compare_corner(mine, reference, corner)
    assert all(delta.seconds < 0 for delta in deltas)


def test_a_corner_neither_lap_covers_produces_nothing():
    reference = build()
    partial = coaching.LapTrace()
    for distance in range(900, 1100, 5):
        partial.add(float(distance), distance / 50.0, 50.0)

    corner = coaching.find_corners(reference)[0]     # around 300m
    assert coaching.compare_corner(partial, reference, corner) == []


def test_only_the_worse_half_is_worth_saying():
    deltas = [coaching.PhaseDelta(coaching.ENTRY, 0.05),
              coaching.PhaseDelta(coaching.EXIT, 0.30)]
    assert coaching.worst(deltas, 0.08).phase == coaching.EXIT


def test_a_corner_the_two_laps_agree_on_says_nothing():
    deltas = [coaching.PhaseDelta(coaching.ENTRY, 0.01),
              coaching.PhaseDelta(coaching.EXIT, -0.02)]
    assert coaching.worst(deltas, 0.08) is None


# -- the lap book ---------------------------------------------------------


@dataclass
class FakeCar:
    driver: str = "Driver"
    lap_dist: float = 0.0
    speed: float = 50.0
    laps: int = 0
    last_lap: float = 0.0
    in_pits: bool = False
    is_player: bool = False


def drive(book, driver="Driver", *, lap_time=90.0, laps=1, start=0.0,
          end=TRACK, in_pits=False, elapsed=0.0):
    """Run one car round one lap and cross the line."""
    car = FakeCar(driver=driver, laps=laps - 1, in_pits=in_pits)
    distance = start
    while distance <= end:
        car.lap_dist = distance
        book.observe(car, elapsed)
        elapsed += 0.1
        distance += 10.0
    # Crossing the line: the lap counter goes up and a time appears with it.
    finished = FakeCar(driver=driver, lap_dist=0.0, laps=laps, last_lap=lap_time)
    return book.observe(finished, elapsed)


def test_a_completed_lap_becomes_that_driver_s_best():
    book = coaching.LapBook(TRACK)
    finished = drive(book, lap_time=90.0)
    assert finished is not None
    assert book.best_for("Driver").lap_time == 90.0


def test_only_a_quicker_lap_replaces_the_best():
    book = coaching.LapBook(TRACK)
    drive(book, lap_time=90.0, laps=1)
    drive(book, lap_time=95.0, laps=2)
    assert book.best_for("Driver").lap_time == 90.0
    drive(book, lap_time=88.0, laps=3)
    assert book.best_for("Driver").lap_time == 88.0


def test_a_lap_through_the_pits_is_never_the_reference():
    """A quick lap that was actually a shortcut through the pit lane would
    become the target everybody is measured against, and nothing about it
    would look wrong."""
    book = coaching.LapBook(TRACK)
    drive(book, lap_time=50.0, in_pits=True)
    assert book.best_for("Driver") is None


def test_a_lap_joined_halfway_through_is_not_kept():
    book = coaching.LapBook(TRACK)
    drive(book, lap_time=90.0, start=TRACK / 2)
    assert book.best_for("Driver") is None


def test_a_lap_with_no_time_on_it_is_not_kept():
    """An out lap, or a car that has just joined. Zero is how the sim says
    "no valid time", and treating it as one would beat every real lap."""
    book = coaching.LapBook(TRACK)
    drive(book, lap_time=0.0)
    assert book.best_for("Driver") is None


def test_a_stationary_car_records_nothing():
    book = coaching.LapBook(TRACK)
    book.observe(FakeCar(lap_dist=100.0, speed=0.0), 1.0)
    assert not book.current["Driver"].samples


def test_the_fastest_lap_in_the_session_is_findable():
    book = coaching.LapBook(TRACK)
    drive(book, "Slow", lap_time=95.0)
    drive(book, "Quick", lap_time=88.0)
    assert book.fastest().driver == "Quick"


def test_a_new_track_throws_everything_away():
    """Lap distances belong to a circuit. Carrying them over would compare Le
    Mans against Sebring with a straight face."""
    book = coaching.LapBook(TRACK)
    drive(book, lap_time=90.0)
    book.reset(4000.0)
    assert book.best_for("Driver") is None
    assert book.track_length == 4000.0
