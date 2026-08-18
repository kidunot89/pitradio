"""What to fill the tank to, and why it is a percentage.

The number on the sim's fuel screen is a percentage of the tank, and the driver
has about four seconds on the way to the pit entry to dial it in. Litres are
the working; the percentage is the answer.
"""

from __future__ import annotations

import pytest

from pitradio.engineer import fuel, queries

# -- learning what the car burns -----------------------------------------


def test_nothing_is_claimed_before_a_lap_has_been_run():
    """A fuel number invented from nothing is the one wrong answer here that
    ends somebody's race."""
    usage = fuel.Usage()
    usage.observe(laps=0, fuel=75.0)
    assert usage.per_lap == 0.0
    assert fuel.needed(remaining=10, pit_in=1, per_lap=usage.per_lap,
                       capacity=75.0) is None


def test_a_lap_s_use_is_the_difference_across_the_line():
    usage = fuel.Usage()
    usage.observe(laps=0, fuel=75.0)
    usage.observe(laps=1, fuel=72.0)
    usage.observe(laps=2, fuel=69.0)
    assert usage.per_lap == pytest.approx(3.0)


def test_refuelling_is_not_a_lap_that_used_no_fuel():
    """The tank going up is a pit stop. Averaged in it would say the car burns
    nothing, and the next answer would be to put nothing in."""
    usage = fuel.Usage()
    usage.observe(laps=0, fuel=30.0)
    usage.observe(laps=1, fuel=27.0)
    usage.observe(laps=2, fuel=75.0)          # stopped and filled up
    usage.observe(laps=3, fuel=72.0)
    assert usage.per_lap == pytest.approx(3.0)


def test_only_recent_laps_count():
    """A whole-stint average is the wrong answer after the fuel map changes."""
    usage = fuel.Usage(recent=2)
    usage.observe(laps=0, fuel=100.0)
    for lap, level in enumerate([95.0, 90.0, 87.0, 84.0], start=1):
        usage.observe(laps=lap, fuel=level)
    assert usage.per_lap == pytest.approx(3.0)


def test_a_restart_throws_the_history_away():
    usage = fuel.Usage()
    usage.observe(laps=8, fuel=40.0)
    usage.observe(laps=9, fuel=37.0)
    usage.observe(laps=0, fuel=75.0)          # session restarted
    assert usage.per_lap == 0.0


# -- how much race is left ------------------------------------------------


def test_a_lap_race_subtracts():
    assert fuel.laps_left(laps_done=12, max_laps=30) == 18


def test_a_timed_race_divides_and_rounds_up():
    """The flag falls at the end of the lap you are on when the clock runs
    out, so the part-lap left is a whole lap of fuel."""
    assert fuel.laps_left(laps_done=5, elapsed=100.0, ends_at=460.0,
                          lap_time=100.0) == 4


def test_lmus_int_max_is_not_a_lap_count():
    """LMU writes INT_MAX into `mMaxLaps` for a timed session. Taken at face
    value it would ask for two billion laps' worth of fuel."""
    assert fuel.laps_left(laps_done=5, max_laps=2147483647, elapsed=100.0,
                          ends_at=460.0, lap_time=100.0) == 4


def test_without_a_lap_time_a_timed_race_cannot_be_answered():
    assert fuel.laps_left(laps_done=5, elapsed=100.0, ends_at=460.0) == 0


# -- the fill -------------------------------------------------------------


def test_the_laps_before_the_stop_are_not_fuelled_for():
    """What is in the tank now covers those. Only the ones after the stop are
    the question."""
    near = fuel.needed(remaining=20, pit_in=1, per_lap=3.0, capacity=100.0)
    far = fuel.needed(remaining=20, pit_in=5, per_lap=3.0, capacity=100.0)
    assert near.laps == 19 and far.laps == 15
    assert near.percent > far.percent


def test_the_answer_carries_a_margin_and_rounds_up():
    """Running out is a retirement; half a litre spare is a tenth a lap."""
    need = fuel.needed(remaining=11, pit_in=1, per_lap=3.0, capacity=100.0)
    # 10 laps x 3L = 30L, plus the margin, as a percentage of 100L.
    assert need.litres == pytest.approx(31.0)
    assert need.percent == 31


def test_a_stop_now_fuels_for_the_whole_remainder():
    need = fuel.needed(remaining=10, pit_in=0, per_lap=2.0, capacity=100.0)
    assert need.laps == 10


def test_a_tank_that_will_not_reach_the_end_is_said_so():
    """**Not silently clipped to 100%.** It means this cannot be the last stop,
    and a driver told "one hundred percent" without being told that plans a
    race that does not work."""
    need = fuel.needed(remaining=60, pit_in=1, per_lap=3.0, capacity=75.0)
    assert need.capped is True
    assert need.percent == 100.0
    assert need.litres == 75.0


def test_a_race_of_unknown_length_gets_no_answer():
    assert fuel.needed(remaining=0, pit_in=1, per_lap=3.0, capacity=75.0) is None


# -- when the stop is -----------------------------------------------------


@pytest.mark.parametrize(
    ("said", "laps"),
    [
        ("", 1.0),                                  # asked on the way in
        ("on the next lap", 1.0),
        ("next lap", 1.0),
        ("next time round", 1.0),
        ("in 3 laps", 3.0),
        ("in three laps", 3.0),
        ("3 laps", 3.0),
        ("when I pit in five laps", 5.0),
        ("now", 0.0),
        ("this lap", 0.0),
    ],
)
def test_when_the_stop_is(said, laps):
    assert queries.pit_in(said) == laps


def test_words_that_are_not_about_pitting_are_not_a_lap_count():
    """"How much fuel do I need to get through this stint on these tyres" is
    not a question this can answer, and taking it as one would put a
    percentage on the radio and the sentence nowhere."""
    assert queries.pit_in("to get through this stint on these tyres") is None
