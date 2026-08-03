"""Trigger gestures for a message waiting to be sent.

Pure timing logic, and the reason it is pure: every one of these cases is one
that would otherwise only be discovered by holding a button mid-race and
watching the wrong thing happen.
"""

import pytest

import gestures


@pytest.fixture
def g():
    return gestures.Gestures(tap_ms=300, double_tap_ms=350)


# -- single tap ------------------------------------------------------------


def test_a_lone_tap_sends_once_its_window_closes(g):
    g.press(0.0)
    assert g.release(0.1) is None      # might yet become a double
    assert g.elapsed(0.2) is None
    assert g.elapsed(0.5) == gestures.SEND


def test_a_tap_is_not_acted_on_immediately(g):
    """Acting at once would make a double-tap impossible to express."""
    g.press(0.0)
    g.release(0.05)
    assert g.waiting is True
    assert g.elapsed(0.05) is None


def test_the_deadline_is_what_the_worker_waits_for(g):
    g.press(0.0)
    g.release(0.1)
    assert g.deadline(0.1) == pytest.approx(0.35)
    assert g.deadline(0.4) == pytest.approx(0.05)
    assert g.deadline(1.0) == 0.0


def test_no_deadline_when_nothing_is_pending(g):
    assert g.deadline(0.0) is None


# -- double tap ------------------------------------------------------------


def test_two_quick_taps_clear(g):
    g.press(0.0)
    assert g.release(0.05) is None
    g.press(0.10)
    assert g.release(0.15) == gestures.CLEAR


def test_a_second_tap_after_the_window_is_a_new_tap_not_a_clear(g):
    """Otherwise a send followed by a stray press would clear the next message."""
    g.press(0.0)
    g.release(0.05)
    assert g.elapsed(0.5) == gestures.SEND

    g.press(0.6)
    assert g.release(0.65) is None
    assert g.elapsed(1.1) == gestures.SEND


def test_a_slow_second_tap_does_not_clear(g):
    g.press(0.0)
    g.release(0.05)
    g.press(0.5)                        # past double_tap_ms
    assert g.release(0.55) is None


def test_clearing_leaves_nothing_pending(g):
    g.press(0.0)
    g.release(0.05)
    g.press(0.10)
    assert g.release(0.15) == gestures.CLEAR
    assert g.waiting is False
    assert g.elapsed(10.0) is None


# -- hold ------------------------------------------------------------------


def test_a_hold_is_a_retry(g):
    g.press(0.0)
    assert g.release(0.5) == gestures.RETRY


def test_a_press_exactly_at_the_threshold_is_a_hold(g):
    g.press(0.0)
    assert g.release(0.3) == gestures.RETRY


def test_a_hold_abandons_a_half_finished_double_tap(g):
    """Tap-then-hold must record, not send whatever the tap was going to."""
    g.press(0.0)
    g.release(0.05)
    assert g.waiting is True

    g.press(0.10)
    assert g.release(0.60) == gestures.RETRY
    assert g.waiting is False
    assert g.elapsed(10.0) is None


# -- robustness ------------------------------------------------------------


def test_a_release_with_no_press_is_ignored(g):
    """The trigger can be released after the pending state began mid-press."""
    assert g.release(1.0) is None


def test_reset_drops_everything(g):
    g.press(0.0)
    g.release(0.05)
    g.reset()
    assert g.waiting is False
    assert g.deadline(0.1) is None


def test_zero_double_tap_window_sends_immediately():
    """A user who does not want the delay can turn the gesture off."""
    g = gestures.Gestures(tap_ms=300, double_tap_ms=0)
    g.press(0.0)
    g.release(0.05)
    assert g.elapsed(0.05) == gestures.SEND


# -- config ----------------------------------------------------------------


def test_gesture_timings_are_configurable():
    import config

    cfg = config.Config.from_dict({"review": {"tap_ms": 200, "double_tap_ms": 250}})
    assert (cfg.review.tap_ms, cfg.review.double_tap_ms) == (200, 250)
    assert cfg.validate() == []


@pytest.mark.parametrize("field", ["tap_ms", "double_tap_ms"])
@pytest.mark.parametrize("value", [-1, 5000, "fast", None])
def test_absurd_gesture_timings_are_rejected(field, value):
    """A tap window of an hour makes the trigger look broken, not configurable."""
    import config

    cfg = config.Config.from_dict({"review": {field: value}})
    assert any(f"review.{field}" in p for p in cfg.validate())
