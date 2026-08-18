"""Deciding when it is safe to pull back onto the track.

The case this exists for: stationary, on the racing line, with something
closing. "Is anything beside me" answers *no* — nothing is, you are off the
track — and acting on that is how a driver gets collected.

Every uncertainty here has to resolve towards waiting, so most of these check
that the answer is **no** in situations where a naive one would say yes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pitradio.engineer import rejoin

TRACK = 5000.0


@dataclass
class Car:
    driver: str
    lap_dist: float = 0.0
    speed: float = 0.0
    in_pits: bool = False


# -- getting up to speed ---------------------------------------------------


def test_a_stopped_car_needs_the_whole_acceleration():
    """The number the naive check leaves out entirely."""
    assert rejoin.time_to_safe(0.0) == pytest.approx(rejoin.SAFE_SPEED / rejoin.ACCELERATION)


def test_a_car_already_going_needs_nothing():
    assert rejoin.time_to_safe(rejoin.SAFE_SPEED + 10) == 0.0


def test_a_rolling_car_needs_less():
    assert 0 < rejoin.time_to_safe(20.0) < rejoin.time_to_safe(0.0)


# -- who is coming ---------------------------------------------------------


def test_a_car_behind_and_closing_is_approaching():
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    others = [Car("Rival", lap_dist=800.0, speed=50.0)]

    coming = rejoin.approaching(own, others, track_length=TRACK)
    assert [entry.driver for entry in coming] == ["Rival"]
    assert coming[0].metres == pytest.approx(200.0)
    assert coming[0].seconds == pytest.approx(4.0)


def test_a_car_going_slower_is_not_approaching():
    """It is not going to arrive, and waiting for it would mean waiting for
    the whole field."""
    own = Car("Me", lap_dist=1000.0, speed=60.0)
    assert rejoin.approaching(
        own, [Car("Slow", lap_dist=800.0, speed=40.0)], track_length=TRACK) == []


def test_a_car_ahead_is_not_behind():
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    assert rejoin.approaching(
        own, [Car("Ahead", lap_dist=1200.0, speed=60.0)], track_length=TRACK) == []


def test_a_car_round_the_lap_is_behind_not_ahead():
    """On a circuit the car "ahead" by lap distance is the one about to arrive."""
    own = Car("Me", lap_dist=100.0, speed=0.0)
    coming = rejoin.approaching(
        own, [Car("Rival", lap_dist=4900.0, speed=50.0)], track_length=TRACK)
    assert coming and coming[0].metres == pytest.approx(200.0)


def test_a_car_in_the_pits_is_not_traffic():
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    assert rejoin.approaching(
        own, [Car("Boxed", lap_dist=900.0, speed=50.0, in_pits=True)],
        track_length=TRACK) == []


def test_the_nearest_arrival_comes_first():
    own = Car("Me", lap_dist=2000.0, speed=0.0)
    coming = rejoin.approaching(own, [
        Car("Far", lap_dist=1600.0, speed=50.0),
        Car("Near", lap_dist=1900.0, speed=50.0),
    ], track_length=TRACK)
    assert [entry.driver for entry in coming] == ["Near", "Far"]


# -- the verdict -----------------------------------------------------------


def test_an_empty_track_is_clear():
    verdict = rejoin.safe_to_rejoin(Car("Me", speed=0.0), [], track_length=TRACK)
    assert verdict.clear is True
    assert verdict.seconds is None


def test_a_gap_that_looks_like_plenty_is_not():
    """**The whole point.** 200m at 60m/s is 3.3 seconds, which sounds ample —
    and a stopped car needs five before it stops being a moving chicane."""
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    others = [Car("Rival", lap_dist=800.0, speed=60.0)]

    verdict = rejoin.safe_to_rejoin(own, others, track_length=TRACK)
    assert verdict.clear is False
    assert verdict.seconds == pytest.approx(200 / 60, rel=0.01)
    assert verdict.needed > verdict.seconds
    assert verdict.waiting_for > 0


def test_a_real_gap_is_clear():
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    others = [Car("Rival", lap_dist=200.0, speed=60.0)]      # 800m back
    assert rejoin.safe_to_rejoin(own, others, track_length=TRACK).clear is True


def test_a_car_already_rolling_needs_less_room():
    """The same gap that is unsafe from a standstill is fine once moving."""
    others = [Car("Rival", lap_dist=800.0, speed=60.0)]
    stopped = rejoin.safe_to_rejoin(
        Car("Me", lap_dist=1000.0, speed=0.0), others, track_length=TRACK)
    rolling = rejoin.safe_to_rejoin(
        Car("Me", lap_dist=1000.0, speed=28.0), others, track_length=TRACK)

    assert stopped.clear is False
    assert rolling.clear is True


def test_the_margin_is_included():
    """Reaction time, and the fact that the numbers underneath are a sim's
    five-times-a-second view of the world."""
    own = Car("Me", speed=rejoin.SAFE_SPEED)         # needs no acceleration
    verdict = rejoin.safe_to_rejoin(own, [], track_length=TRACK)
    assert verdict.needed == pytest.approx(rejoin.MARGIN_SECONDS)


def test_it_says_who_it_is_waiting_for():
    own = Car("Me", lap_dist=1000.0, speed=0.0)
    verdict = rejoin.safe_to_rejoin(
        own, [Car("Rival", lap_dist=900.0, speed=60.0)], track_length=TRACK)
    assert verdict.driver == "Rival"
