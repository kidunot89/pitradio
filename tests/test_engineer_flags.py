"""Flags, incidents, and pulling out of a spin.

The division these pin is the one Crew Chief draws and this app had blurred:
the spotter answers "who is beside me", which is geometry, and flags answer
"what has happened to the track", which is not. Deriving the second from the
first is what put a warning in every braking zone.
"""

from __future__ import annotations

from pitradio.engineer import coaching, flags
from pitradio.plugins.base import Car


def a_car(name: str, *, distance: float = 0.0, speed: float = 60.0,
          sector: int = 1, pits: bool = False) -> Car:
    return Car(slot=0, driver=name, lap_dist=distance, speed=speed,
               sector=sector, in_pits=pits)


# -- what counts as an incident -------------------------------------------


def test_a_car_slowing_for_a_corner_is_not_an_incident():
    watch = flags.Incidents(hold=2.0)
    braking = [a_car("Ahead", speed=25.0)]

    assert watch.update(braking, 0.0) == []
    assert watch.update(braking, 5.0) == []


def test_a_car_has_to_stay_stopped_for_two_seconds():
    watch = flags.Incidents(hold=2.0)
    spun = [a_car("Ahead", speed=0.0)]

    assert watch.update(spun, 0.0) == []
    assert watch.update(spun, 1.5) == []
    assert watch.update(spun, 2.5) == ["Ahead"]


def test_a_car_in_its_pit_box_is_not_an_incident():
    """Or every pit stop in the race is a yellow flag."""
    watch = flags.Incidents(hold=1.0)
    stopped = [a_car("Ahead", speed=0.0, pits=True)]

    assert watch.update(stopped, 0.0) == []
    assert watch.update(stopped, 5.0) == []


def test_getting_going_again_restarts_the_clock():
    watch = flags.Incidents(hold=2.0)
    watch.update([a_car("Ahead", speed=0.0)], 0.0)
    watch.update([a_car("Ahead", speed=50.0)], 1.0)
    # Spun a second time; two seconds from *now*, not from the first spin.
    assert watch.update([a_car("Ahead", speed=0.0)], 1.5) == []
    assert watch.update([a_car("Ahead", speed=0.0)], 4.0) == ["Ahead"]


def test_a_car_that_has_left_the_session_is_forgotten():
    watch = flags.Incidents(hold=2.0)
    watch.update([a_car("Gone", speed=0.0)], 0.0)
    watch.update([a_car("Someone", speed=50.0)], 1.0)
    # Back with a fresh clock rather than instantly called.
    assert watch.update([a_car("Gone", speed=0.0)], 1.5) == []


# -- where it is ----------------------------------------------------------


def test_an_incident_behind_you_is_not_ahead_of_you():
    own = a_car("Me", distance=3000.0)
    spun = a_car("Spun", distance=1000.0, speed=0.0)

    found = flags.incidents(own, [own, spun], ["Spun"], track_length=5000.0)
    # Round the lap, which is 3,000m away rather than 2,000m behind.
    assert found[0].ahead == 3000.0
    assert flags.nearest_ahead(found, metres=600.0) is None


def test_the_nearest_incident_ahead_is_the_one_called():
    own = a_car("Me", distance=1000.0)
    near = a_car("Near", distance=1200.0, speed=0.0)
    far = a_car("Far", distance=1500.0, speed=0.0)

    found = flags.incidents(own, [own, near, far], ["Near", "Far"],
                            track_length=5000.0)
    assert flags.nearest_ahead(found).driver == "Near"


def test_the_sim_s_sector_numbering_is_untangled():
    """0 is sector three, and nothing outside these two modules sees that."""
    assert flags.sector_of(a_car("X", sector=0)) == 3
    assert flags.sector_of(a_car("X", sector=1)) == 1
    assert flags.sector_of(a_car("X", sector=2)) == 2


def test_a_corner_number_beats_a_sector_number():
    corners = [coaching.Corner(number=6, entry=1100.0, apex=1200.0,
                               exit=1300.0)]
    own = a_car("Me", distance=1000.0)
    spun = a_car("Spun", distance=1200.0, speed=0.0, sector=2)

    found = flags.incidents(
        own, [own, spun], ["Spun"], track_length=5000.0,
        corner_at=lambda where: coaching.corner_at(corners, where))
    assert flags.Watch().incident_call(found[0]) == "car stopped in turn 6"


def test_without_a_reference_lap_the_sector_is_named():
    own = a_car("Me", distance=1000.0)
    spun = a_car("Spun", distance=1200.0, speed=0.0, sector=2)

    found = flags.incidents(own, [own, spun], ["Spun"], track_length=5000.0)
    assert flags.Watch().incident_call(found[0]) == "car stopped in sector 2"


# -- only changes are spoken ----------------------------------------------


def test_a_caution_is_called_on_both_edges_and_not_between():
    watch = flags.Watch()
    assert watch.caution_changed(True) == "full course yellow"
    assert watch.caution_changed(True) is None
    assert watch.caution_changed(False) == "green flag"
    assert watch.caution_changed(False) is None


def test_a_blue_flag_going_away_is_not_news():
    """It means the car went past, which the driver watched happen."""
    watch = flags.Watch()
    assert watch.blue_changed(True) == "blue flag"
    assert watch.blue_changed(True) is None
    assert watch.blue_changed(False) is None


def test_an_incident_is_called_once_rather_than_every_lap():
    watch = flags.Watch()
    incident = flags.Incident("Spun", 1200.0, 200.0, sector=2)
    assert watch.incident_call(incident) == "car stopped in sector 2"
    assert watch.incident_call(incident) is None


def test_a_car_that_recovers_and_spins_again_is_called_again():
    watch = flags.Watch()
    incident = flags.Incident("Spun", 1200.0, 200.0, sector=2)
    watch.incident_call(incident)
    watch.forget([])                      # they got going again
    assert watch.incident_call(incident) == "car stopped in sector 2"


# -- through the notification ---------------------------------------------


def spoken(calls) -> list[str]:
    return [" ".join(str(part) for part in call.utterance) for call in calls]


def test_the_grid_before_the_start_is_not_a_rejoin(engineer_context):
    """Stationary, on the line, with the field behind — every input the rejoin
    advice looks at, and none of it means what it would mean mid-race."""
    from pitradio.engineer import notifications

    behaviour = notifications.FlagNotification()
    grid = [a_car("Me", distance=0.0, speed=0.0),
            a_car("Behind", distance=-20.0, speed=0.0)]
    assert spoken(behaviour.check(engineer_context(grid, me="Me"))) == []


def test_stopping_after_a_lap_does_ask_about_rejoining(engineer_context):
    from pitradio.engineer import notifications

    behaviour = notifications.FlagNotification()
    behaviour.check(engineer_context(
        [a_car("Me", distance=1000.0, speed=60.0)], me="Me"))
    calls = behaviour.check(engineer_context(
        [a_car("Me", distance=1200.0, speed=0.0),
         a_car("Coming", distance=1100.0, speed=60.0)], me="Me"))
    assert any("hold" in line for line in spoken(calls))


def test_the_pit_lane_is_not_a_rejoin(engineer_context):
    from pitradio.engineer import notifications

    behaviour = notifications.FlagNotification()
    behaviour.check(engineer_context(
        [a_car("Me", distance=1000.0, speed=60.0)], me="Me"))
    calls = behaviour.check(engineer_context(
        [a_car("Me", distance=1200.0, speed=0.0, pits=True)], me="Me"))
    assert spoken(calls) == []
