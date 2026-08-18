"""The engineer end to end, with a made-up sim behind it.

Driven through the real `EngineerService` against a stub plugin registry, a
stub speaker and a clock the test controls — so behaviours, repeat intervals,
routines and command routing are all exercised as they actually run, not as
they are described.

The most important assertions in here are the negative ones. `handle` returning
True is what makes the worker throw somebody's words away instead of sending
them, so every case where it must return False is a message that would
otherwise vanish.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from pitradio import config as config_mod
from pitradio.engineer import notifications, service, spotter
from pitradio.plugins import base
from pitradio.plugins.base import Car, SessionInfo, Standings

TRACK = 1200.0
CORNERS = (300.0, 800.0)


# -- stubs ----------------------------------------------------------------


class Store:
    def __init__(self):
        self.config = config_mod.Config()
        self.config.engineer.enabled = True


class Speaker:
    """Records utterances instead of making a sound."""

    def __init__(self):
        self.said: list[list[str]] = []
        self.urgent: list[list[str]] = []
        self.cleared = 0
        self.settings = None
        self.primed = []

    def start(self):
        pass

    def stop(self, timeout=None):
        pass

    def configure(self, settings):
        self.settings = settings

    def prime(self, phrases):
        self.primed = list(phrases)

    def say(self, utterance, *, urgent=False):
        self.said.append(list(utterance))
        if urgent:
            self.urgent.append(list(utterance))

    def clear(self):
        self.cleared += 1

    def lines(self) -> list[str]:
        return [" ".join(line) for line in self.said]

    def spoken(self) -> str:
        return " | ".join(self.lines())


class Host:
    def voices(self):
        return []

    def close(self):
        pass


@dataclass
class Plugins:
    """One sim, publishing whatever the test sets on it."""

    session: SessionInfo = field(default_factory=SessionInfo)
    standings: Standings = field(default_factory=Standings)
    #: What this sim claims it can supply. None means "nobody said", which is
    #: read as "everything" so a plugin written before capabilities existed
    #: does not silently lose its behaviours.
    provides: frozenset[str] | None = None
    #: The profile's plugin settings — where the engineer's per-sim overrides
    #: live.
    settings: dict = field(default_factory=dict)

    def any_telemetry(self):
        return ("stub", self.session) if self.session.has_data else ("", SessionInfo())

    def standings_for(self, plugin_id):
        return self.standings

    def settings_for(self, plugin_id, stored=None):
        return {**self.settings, **(stored or {})}

    def provides_for(self, plugin_id):
        return self.provides


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engineer(clock):
    return service.EngineerService(
        Store(), Plugins(), speaker=Speaker(), host=Host(), clock=clock)


def car(driver, *, distance=0.0, speed=50.0, laps=0, last_lap=0.0,
        player=False, place=1, position=(0.0, 0.0, 0.0), in_pits=False,
        sector=1, cur1=0.0, cur2=0.0, last1=0.0, last2=0.0,
        vehicle_class=""):
    return Car(
        slot=abs(hash(driver)) % 1000, driver=driver, place=place,
        control=0 if player else 2, is_player=player, position=position,
        lap_dist=distance, speed=speed, laps=laps, last_lap=last_lap,
        in_pits=in_pits, sector=sector, cur_sector1=cur1, cur_sector2=cur2,
        last_sector1=last1, last_sector2=last2, vehicle_class=vehicle_class)


#: The sim clock, which advances by itself between frames unless a test says
#: otherwise. It has to move: the spotter's overlap and clear delays are
#: measured against it, and a frozen clock means an overlap that has lasted no
#: time at all and is therefore never called.
_CLOCK = {"now": 0.0}


def publish(engineer, *cars, elapsed=None):
    """Hand the engineer one frame of the sim and let it tick."""
    if elapsed is None:
        _CLOCK["now"] += 0.2
        elapsed = _CLOCK["now"]
    engineer.plugins.session = SessionInfo(
        track="Sebring", track_length=TRACK, elapsed=elapsed, cars=tuple(cars))
    engineer._tick()


def enable(engineer, notification_id, *, repeat=0.0):
    engineer.store.config.engineer.notifications[notification_id] = \
        config_mod.NotificationConfig(enabled=True, repeat_seconds=repeat)


def disable(engineer, notification_id):
    engineer.store.config.engineer.notifications[notification_id] = \
        config_mod.NotificationConfig(enabled=False)


# -- what reaches the chat box --------------------------------------------


def test_an_ordinary_message_is_not_taken_by_the_engineer(engineer):
    for said in ("box this lap", "sorry about that", "P3 is quick here",
                 "my sector three is terrible", "target time is a twenty three"):
        assert engineer.handle(said) is False, said


def test_a_routine_command_is_taken(engineer):
    assert engineer.handle("begin hot lap trainer") is True


def test_nothing_is_taken_while_the_engineer_is_off(engineer):
    """Switched off it must be completely inert, or a config nobody has looked
    at eats messages."""
    engineer.store.config.engineer.enabled = False
    assert engineer.handle("begin hot lap trainer") is False


def test_a_distinctive_phrase_needs_no_name(engineer):
    """"begin hot lap trainer" cannot collide with racing speech, so demanding
    the engineer's name in front of it would be pedantry."""
    assert engineer.handle("begin hot lap trainer GT3 P1") is True


def test_the_name_is_what_it_answers_to(engineer):
    engineer.store.config.engineer.name = "Radio"
    assert engineer.handle("Radio") is True               # acknowledged
    assert engineer.handle("Chief") is False              # renamed, so not it


def test_there_is_a_name_even_when_it_is_cleared(engineer):
    """The name is how a command is addressed, so an empty one would silently
    narrow the engineer to its bare phrases."""
    engineer.store.config.engineer.name = ""
    assert engineer.display_name() == "Chief"
    assert engineer.handle("Chief") is True


def test_the_spotter_phrases_are_rendered_up_front(engineer):
    """The first "car left" of a session is the one that would otherwise pay
    for a synthesiser while somebody is already alongside."""
    engineer.refresh_voice(force=True)
    assert any("car left" in phrase for phrase in engineer.speaker.primed)


def test_the_fallback_voice_speaks_briskly_by_default(engineer):
    """A synthesiser's natural pace is tuned for reading prose to somebody
    sitting still; a race call competes with an engine and expires in corners."""
    from pitradio.engineer import tts

    engineer.refresh_voice(force=True)
    assert engineer.speaker.settings.rate == tts.DEFAULT_RATE
    assert tts.DEFAULT_RATE > 0


def test_a_chosen_pace_wins_over_the_default(engineer):
    engineer.store.config.engineer.rate = -2
    engineer.refresh_voice(force=True)
    assert engineer.speaker.settings.rate == -2


def test_a_disabled_routine_stops_answering(engineer):
    engineer.store.config.engineer.routines = {
        "hot_lap_trainer": config_mod.RoutineConfig(enabled=False)}
    assert engineer.handle("begin hot lap trainer") is False


def test_a_configured_phrase_replaces_the_defaults(engineer):
    """The user's own words win outright. Leaving the shipped ones live would
    mean a phrase they did not choose still fires."""
    engineer.store.config.engineer.routines = {
        "hot_lap_trainer": config_mod.RoutineConfig(
            phrases=["initiate build procedures {target}"])}

    assert engineer.handle("initiate build procedures") is True
    assert engineer.handle("begin hot lap trainer") is False


def test_a_failure_inside_the_engineer_lets_the_message_through(engineer, monkeypatch):
    """The worst bug this feature could have is eating a message on an error
    path, so matching failing has to mean "not a command"."""
    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(service.phrases, "match_command", boom)
    assert engineer.handle("begin hot lap trainer") is False


# -- starting and stopping ------------------------------------------------


def test_a_routine_ends_on_its_own_end_phrase(engineer):
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.plugins.standings = Standings(overall={1: "Rival"})
    engineer.handle("begin hot lap trainer")
    assert engineer.active is not None

    engineer.handle("end hot lap trainer")
    assert engineer.active is None
    assert "standing down" in engineer.speaker.spoken()


def test_the_global_stop_ends_whatever_is_running(engineer):
    """A driver who wants the engineer to shut up should not have to remember
    which phrase started it."""
    engineer.handle("begin sector trainer P1 sector 2")
    engineer.handle("stop")
    assert engineer.active is None


def test_one_routine_s_end_phrase_does_not_stop_another(engineer):
    """Misremembering which one you started must not silently stop the other."""
    engineer.handle("begin hot lap trainer")
    running = engineer.active
    engineer.handle("end sector trainer")
    assert engineer.active is running


def test_starting_one_routine_stands_the_other_down(engineer):
    """Two routines commenting on the same lap is what makes people switch the
    whole thing off."""
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.handle("begin hot lap trainer Rival")
    engineer.handle("begin sector trainer Rival sector 2")
    assert engineer.active.id == "sector_trainer"


def test_a_routine_that_declines_to_start_is_not_left_running(engineer):
    """The sector trainer needs a sector. Without one it asks, and must not sit
    there looking active while doing nothing."""
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.handle("begin sector trainer Rival")

    assert engineer.active is None
    assert "which sector" in engineer.speaker.spoken()


def test_the_name_alone_gets_an_answer(engineer):
    engineer.handle("Chief")
    assert "go ahead" in engineer.speaker.spoken()


# -- naming a target ------------------------------------------------------


def test_a_target_can_be_named_by_class_and_position(engineer):
    """Endurance grids run several classes, so "P1" has three answers and only
    naming the class picks the one meant."""
    engineer.plugins.standings = Standings(
        overall={1: "Hyper Leader", 5: "GT Leader"},
        by_class={"Hypercar": {1: "Hyper Leader"}, "LMGT3": {1: "GT Leader"}})
    publish(engineer, car("Me", player=True),
            car("GT Leader", place=5, vehicle_class="LMGT3"))

    engineer.handle("begin hot lap trainer GT3 P1")
    assert engineer.active.target == "GT Leader"


def test_a_bare_position_is_the_overall_order(engineer):
    """Which is the column the timing screen shows by default."""
    engineer.plugins.standings = Standings(overall={1: "Leader", 3: "Third"})
    publish(engineer, car("Me", player=True), car("Third", place=3))

    engineer.handle("begin hot lap trainer P3")
    assert engineer.active.target == "Third"


def test_an_unknown_target_is_said_rather_than_silently_ignored(engineer):
    publish(engineer, car("Me", player=True))
    engineer.handle("begin hot lap trainer Hamilton")
    assert "can't find" in engineer.speaker.spoken()
    assert engineer.active is None


def test_a_target_with_no_lap_yet_is_still_taken(engineer):
    """They probably will set one, and the routine should start working then
    rather than needing to be asked again."""
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.handle("begin hot lap trainer Rival")

    assert engineer.active.target == "Rival"
    assert "no lap on record" in engineer.speaker.spoken()


def test_the_sector_trainer_takes_a_driver_and_a_sector(engineer):
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.handle("begin sector trainer Rival, sector 3")

    assert engineer.active.target == "Rival"
    assert engineer.active.sector == 3


def test_the_parameters_can_be_given_in_either_order(engineer):
    """Whisper's commas are not to be relied on and nobody says a command the
    same way twice."""
    publish(engineer, car("Me", player=True), car("Rival"))
    engineer.handle("begin sector trainer sector 3 Rival")

    assert engineer.active.target == "Rival"
    assert engineer.active.sector == 3


# -- behaviours -----------------------------------------------------------


def drive_a_lap(engineer, *, driver="Me", lap_time=90.0, laps=1, elapsed=0.0,
                player=True, others=()):
    """Take a car round and across the line, so a lap is recorded."""
    for distance in range(0, int(TRACK), 20):
        publish(engineer,
                car(driver, player=player, distance=float(distance)), *others,
                elapsed=elapsed)
        elapsed += 0.2
    publish(engineer,
            car(driver, player=player, distance=5.0, laps=laps,
                last_lap=lap_time), *others, elapsed=elapsed)


def test_a_finished_lap_is_read_out(engineer):
    enable(engineer, notifications.LAP_TIME)
    drive_a_lap(engineer, lap_time=83.4)
    assert "one twenty three point four zero" in engineer.speaker.spoken()


def test_lap_times_can_be_switched_off(engineer):
    disable(engineer, notifications.LAP_TIME)
    drive_a_lap(engineer, lap_time=83.4)
    assert engineer.speaker.said == []


def test_a_new_fastest_lap_is_announced_but_the_first_one_is_not(engineer):
    """The first lap of a session is not somebody taking the fastest lap, it is
    the only lap. Announcing it would be announcing nothing."""
    enable(engineer, notifications.FASTEST_LAP)
    disable(engineer, notifications.LAP_TIME)

    drive_a_lap(engineer, lap_time=90.0, laps=1)
    assert engineer.speaker.said == []

    drive_a_lap(engineer, lap_time=88.0, laps=2)
    assert "fastest lap of the session" in engineer.speaker.spoken()


def test_a_slower_lap_takes_nothing(engineer):
    enable(engineer, notifications.FASTEST_LAP)
    disable(engineer, notifications.LAP_TIME)

    drive_a_lap(engineer, lap_time=88.0, laps=1)
    drive_a_lap(engineer, lap_time=92.0, laps=2)
    assert engineer.speaker.said == []


def cross_sectors(engineer, driver="Me", *, times=(30.0, 35.0, 25.0), player=True,
                  vehicle_class="", alongside_player=None):
    """Take a car through all three sector boundaries with the given times.

    `alongside_player` publishes the player's own car in every frame as well,
    which is what the class filter reads the driver's own class from.
    """
    first, second, third = times
    lap = first + second + third
    extra = [alongside_player] if alongside_player is not None else []

    def frame(**kwargs):
        publish(engineer, car(driver, player=player, vehicle_class=vehicle_class,
                              **kwargs), *extra)

    # In sector 1, then 2, then 3 (which the sim calls 0), then the line.
    frame(sector=1, distance=10.0)
    frame(sector=2, distance=400.0, cur1=first)
    frame(sector=0, distance=800.0, cur1=first, cur2=first + second)
    frame(sector=1, distance=10.0, laps=1, last_lap=lap,
          last1=first, last2=first + second)


def test_sector_times_come_out_of_the_cumulative_splits(engineer):
    """The sim publishes sector two as "one plus two", and never publishes
    sector three at all."""
    enable(engineer, notifications.SECTOR_PERFORMANCE)
    disable(engineer, notifications.LAP_TIME)

    cross_sectors(engineer, times=(30.0, 35.0, 25.0))
    assert engineer.sectors.best_for("Me", 1) == pytest.approx(30.0)
    assert engineer.sectors.best_for("Me", 2) == pytest.approx(35.0)
    assert engineer.sectors.best_for("Me", 3) == pytest.approx(25.0)


def test_a_better_sector_is_called_and_a_worse_one_too(engineer):
    enable(engineer, notifications.SECTOR_PERFORMANCE)
    disable(engineer, notifications.LAP_TIME)

    cross_sectors(engineer, times=(30.0, 35.0, 25.0))
    engineer.speaker.said.clear()
    cross_sectors(engineer, times=(31.0, 35.0, 25.0))

    said = engineer.speaker.spoken()
    assert "sector one" in said
    assert "down" in said           # a second slower than before


def test_a_sector_within_the_threshold_is_not_worth_saying(engineer):
    enable(engineer, notifications.SECTOR_PERFORMANCE)
    disable(engineer, notifications.LAP_TIME)

    cross_sectors(engineer, times=(30.0, 35.0, 25.0))
    engineer.speaker.said.clear()
    # A hundredth over thirty seconds is not something anybody drove
    # differently.
    cross_sectors(engineer, times=(30.01, 35.01, 25.01))
    assert engineer.speaker.said == []


def test_a_new_fastest_sector_is_announced(engineer):
    enable(engineer, notifications.FASTEST_SECTOR)
    disable(engineer, notifications.LAP_TIME)

    cross_sectors(engineer, "Rival", player=False, times=(30.0, 35.0, 25.0))
    engineer.speaker.said.clear()
    cross_sectors(engineer, "Rival", player=False, times=(28.0, 35.0, 25.0))

    assert "has taken" in engineer.speaker.spoken()
    assert "sector one" in engineer.speaker.spoken()


# -- the spotter ----------------------------------------------------------


def alongside(engineer, *, left=False, right=False, distance=10.0, gap=1.0):
    """Put the player on the move with cars beside them, and tick.

    `gap` is how far up the road the other cars are, which is what the
    alongside range is measured against.

    **Two frames, not one.** Crew Chief's overlap delay means a car has to
    still be there on the next look before it is called, and a helper that
    published a single frame would be testing a spotter with that turned off.
    """
    others = []
    if left:
        others.append(car("Left", position=(-3.0, 0.0, distance + gap)))
    if right:
        others.append(car("Right", position=(3.0, 0.0, distance + gap)))
    for _ in range(2):
        publish(engineer, car("Me", player=True, position=(0.0, 0.0, distance)),
                *others)


def test_the_spotter_keeps_saying_it_while_the_car_is_still_there(engineer, clock):
    """The behaviour you asked for: it repeats until the traffic has gone."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)          # no heading yet
    alongside(engineer, left=True, distance=10.0)
    first = list(engineer.speaker.lines())
    assert any("left" in line for line in first)

    # Nothing new within the interval.
    clock.advance(1.0)
    alongside(engineer, left=True, distance=20.0)
    assert engineer.speaker.lines() == first

    # And again once it has passed.
    clock.advance(3.0)
    alongside(engineer, left=True, distance=30.0)
    assert len(engineer.speaker.lines()) == len(first) + 1


def test_the_side_going_clear_is_called_immediately(engineer, clock):
    """It does not wait out the repeat interval, because it is new
    information rather than the same call again."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    engineer.speaker.said.clear()

    clock.advance(0.2)
    alongside(engineer, distance=20.0)
    assert "clear left" in engineer.speaker.spoken()


def test_the_two_sides_are_tracked_apart(engineer, clock):
    """A car arriving on the right must not reset the call about the left."""
    enable(engineer, notifications.SPOTTER, repeat=30.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    engineer.speaker.said.clear()

    clock.advance(0.2)
    alongside(engineer, left=True, right=True, distance=20.0)
    said = engineer.speaker.spoken()
    # Somebody either side is a situation, not two calls: Crew Chief says
    # where *you* are, which is the thing that can be acted on.
    assert spotter.IN_THE_MIDDLE in said
    assert said.count("car left") == 0


def test_the_spotter_is_silent_until_it_is_switched_on(engineer):
    disable(engineer, notifications.SPOTTER)
    disable(engineer, notifications.LAP_TIME)
    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    assert engineer.speaker.said == []


# -- the spotter's geometry -----------------------------------------------


def test_a_car_alongside_is_called():
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    neighbours = spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (3.0, 0.0, 11.0)})

    assert len(neighbours) == 1
    assert spotter.call(neighbours) in ("car left", "car right")


def test_swapping_sides_reverses_the_call():
    """Which side is which depends on the sim's own axes, and that could not be
    checked without a car on a track — so it is a setting, not a guess."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    normal = spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (3.0, 0.0, 11.0)})
    swapped = spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (3.0, 0.0, 11.0)}, swap=True)

    assert normal[0].side != swapped[0].side


def test_a_car_directly_in_front_is_not_alongside():
    """Reported from the car: nose-to-tail traffic was being called "car left".

    Alongside means beside you *across* the track. Near enough the same line
    and one car is simply following the other, however close they are.
    """
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    assert spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Ahead": (0.4, 0.0, 13.0)}) == []


def test_a_car_directly_behind_is_not_alongside():
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    assert spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Behind": (0.4, 0.0, 7.0)}) == []


def test_a_queue_behind_the_car_beside_you_is_still_one_car():
    """Also reported: "two cars left" when there was one, with another tucked
    in behind it. The second is not beside you and calling it makes the driver
    look for something that is not there."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    neighbours = spotter.alongside(
        (0.0, 0.0, 10.0), facing,
        {"Beside": (-3.0, 0.0, 10.5), "BehindThem": (-3.0, 0.0, 3.0)},
        holding=frozenset({"left"}))

    assert spotter.counts(neighbours) == {"left": 1}
    assert spotter.call(neighbours) == "car left"


def test_a_held_car_keeps_the_side_called_without_being_counted():
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    neighbours = spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Drifted": (-3.0, 0.0, 15.0)},
        holding=frozenset({"left"}))

    assert neighbours and neighbours[0].overlapping is False
    assert spotter.counts(neighbours) == {"left": 0}


def test_two_cars_on_one_side_says_which_side_you_are_on():
    """"Two cars left" is the same fact stated the harder way round: a driver
    hearing it has to work out where that leaves them, while they are busy."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    neighbours = spotter.alongside(
        (0.0, 0.0, 10.0), facing,
        {"Near": (-2.0, 0.0, 10.5), "Wide": (-5.0, 0.0, 9.5)})
    assert spotter.call(neighbours) == spotter.THREE_WIDE_RIGHT


# -- the heading the axes depend on ---------------------------------------


def test_a_heading_needs_real_ground_covered():
    """The fault underneath the other two: through slow traffic consecutive
    reads are centimetres apart, the direction between them is the sim's own
    rounding, and a car in front resolves as a car beside."""
    heading = spotter.Heading()
    assert heading.update((0.0, 0.0, 0.0)) is None
    assert heading.update((0.0, 0.0, 0.2)) is None
    assert heading.update((0.0, 0.0, 0.4)) is None


def test_a_heading_settles_once_the_car_has_moved():
    heading = spotter.Heading()
    for z in (0.0, 2.0, 4.0, 6.0):
        facing = heading.update((0.0, 0.0, z))
    assert facing == pytest.approx((0.0, 1.0))


def test_the_last_good_heading_survives_slowing_down():
    """A car braking to a stop still points the way it was a moment ago, and
    forgetting that would silence the spotter exactly when traffic is closest."""
    heading = spotter.Heading()
    for z in (0.0, 2.0, 4.0, 6.0):
        heading.update((0.0, 0.0, z))
    for _ in range(6):
        facing = heading.update((0.0, 0.0, 6.0))
    assert facing == pytest.approx((0.0, 1.0))


def test_a_car_far_ahead_is_not_alongside():
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    assert spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (3.0, 0.0, 90.0)}) == []


def test_a_car_on_the_next_straight_is_not_alongside():
    """Where a plain distance check starts shouting about nobody."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    assert spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (40.0, 0.0, 11.0)}) == []


def test_height_alone_does_not_put_somebody_on_your_door():
    """A bridge, a banking, or the Le Mans esses."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    neighbours = spotter.alongside(
        (0.0, 0.0, 10.0), facing, {"Rival": (3.0, 30.0, 11.0)})
    assert len(neighbours) == 1


def test_a_stationary_car_has_no_heading():
    """Everything on the circuit would otherwise swing around a parked car."""
    assert spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 0.1)) is None


def test_a_side_that_has_not_changed_produces_no_call():
    """Repeating while a car is still there is a timer, not a state change."""
    assert spotter.calls(frozenset({"left"}), frozenset({"left"})) == []


def test_both_directions_are_calls():
    arriving = spotter.calls(frozenset({"left"}), frozenset())
    leaving = spotter.calls(frozenset(), frozenset({"left"}))

    assert arriving == [("left", "car left", True)]
    # Urgent too. A driver holding a line for a car that left two corners ago
    # is giving up track they could be using, and they hold it until told
    # otherwise — so the all-clear cannot queue behind a lap time either.
    assert leaving == [("left", "clear left", True)]


# -- track changes --------------------------------------------------------


def test_the_engineer_works_offline(engineer):
    """No game server, no session key — and it must still do everything.

    Voice chat needs a server because there has to be somebody to be in a room
    with. The engineer does not, and offline practice is the *most* likely
    place to want a coaching routine. `SessionInfo.__bool__` is False here and
    `has_data` is True, which is exactly the distinction.
    """
    enable(engineer, notifications.LAP_TIME)
    offline = SessionInfo(track="Sebring", track_length=TRACK, key="",
                          cars=(car("Me", player=True),))
    assert not offline           # no room to join
    assert offline.has_data      # but a car on a track

    drive_a_lap(engineer, lap_time=83.4)
    assert "one twenty three point four zero" in engineer.speaker.spoken()

    engineer.handle("begin hot lap trainer")
    assert engineer.active is not None


# -- what the sim can and cannot supply -----------------------------------


def test_a_behaviour_whose_data_is_missing_is_skipped(engineer):
    """A tick-box that is on, looks fine and is permanently silent is
    indistinguishable from a bug — and is how somebody comes to file one.

    iRacing is the real case: it publishes how far round the lap each car is
    and nothing about where that is in space, so the spotter's geometry cannot
    be done at all.
    """
    engineer.plugins.provides = frozenset({base.PROVIDES_LAPS})
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    assert engineer.speaker.said == []


def test_a_behaviour_whose_data_is_present_still_runs(engineer):
    engineer.plugins.provides = frozenset({base.PROVIDES_POSITIONS})
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    assert any("left" in line for line in engineer.speaker.lines())


def test_a_plugin_that_says_nothing_keeps_its_behaviours(engineer):
    """None means "nobody said", not "this sim has nothing".

    A plugin written before capabilities existed would otherwise lose every
    behaviour the moment the check was added, which is a silent regression for
    anybody's third-party plugin.
    """
    engineer.plugins.provides = None
    enable(engineer, notifications.LAP_TIME)

    drive_a_lap(engineer, lap_time=83.4)
    assert "one twenty three point four zero" in engineer.speaker.spoken()


def test_the_reason_a_behaviour_is_quiet_is_said_once(engineer, caplog):
    """Silence nobody can explain is the failure this whole check exists to
    avoid, so it has to reach the log — and only once, not ten times a
    second."""
    engineer.plugins.provides = frozenset()
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    with caplog.at_level("INFO"):
        for _ in range(5):
            alongside(engineer, left=True, distance=10.0)

    # Once per behaviour, not once per tick. Other behaviours are unsupported
    # here too and each says so on its own — that is the point of the wording.
    said = [line for line in caplog.text.splitlines()
            if "does not publish" in line and "Spotter" in line]
    assert len(said) == 1
    assert "positions" in said[0]


def test_a_sim_that_does_its_own_spotting_needs_no_positions(engineer):
    """iRacing publishes no other-car world positions at all — only how far
    round the lap each one is, which cannot answer "who is beside me" on a
    circuit that doubles back. It publishes its own left/right call instead,
    and that is a better answer than any geometry."""
    engineer.plugins.provides = frozenset({base.PROVIDES_SPOTTER})
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    # Everybody at the origin: geometry could say nothing here.
    for elapsed in (1.0, 1.2):            # the overlap delay wants a second look
        engineer.plugins.session = SessionInfo(
            track="Watkins", track_length=TRACK, elapsed=elapsed,
            cars=(car("Me", player=True, speed=60.0),), alongside={"left": 1})
        engineer._tick()

    assert "car left" in engineer.speaker.spoken()


def test_the_sim_s_own_count_reaches_the_call(engineer):
    """Two cars stacked down one side is a different problem from one, and the
    moment they arrive is when that matters most."""
    engineer.plugins.provides = frozenset({base.PROVIDES_SPOTTER})
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    for elapsed in (1.0, 1.2):
        engineer.plugins.session = SessionInfo(
            track="Watkins", track_length=TRACK, elapsed=elapsed,
            cars=(car("Me", player=True, speed=60.0),), alongside={"right": 2})
        engineer._tick()

    assert spotter.THREE_WIDE_LEFT in engineer.speaker.spoken()


def test_the_sim_s_spotter_clears_a_side_like_any_other(engineer):
    engineer.plugins.provides = frozenset({base.PROVIDES_SPOTTER})
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    empty = SessionInfo(track="Watkins", track_length=TRACK,
                        cars=(car("Me", player=True, speed=60.0),))
    for elapsed in (1.0, 1.2):
        engineer.plugins.session = replace(empty, elapsed=elapsed,
                                           alongside={"left": 1})
        engineer._tick()
    engineer.speaker.said.clear()

    for elapsed in (2.0, 2.3):            # and the clear delay wants one too
        engineer.plugins.session = replace(empty, elapsed=elapsed, alongside={})
        engineer._tick()
    assert "clear left" in engineer.speaker.spoken()


def test_only_your_own_class_is_worth_a_call(engineer):
    """A GT3 driver is not racing the Hypercars, and being told one has taken
    the fastest lap is noise at best and misleading at worst."""
    enable(engineer, notifications.FASTEST_SECTOR)
    disable(engineer, notifications.LAP_TIME)

    me = car("Me", player=True, vehicle_class="LMGT3")

    # A Hypercar taking a sector outright says nothing to a GT3 driver.
    cross_sectors(engineer, "Hyper", player=False, vehicle_class="Hypercar",
                  times=(20.0, 30.0, 20.0), alongside_player=me)
    engineer.speaker.said.clear()
    cross_sectors(engineer, "Hyper", player=False, vehicle_class="Hypercar",
                  times=(18.0, 30.0, 20.0), alongside_player=me)
    assert engineer.speaker.said == []


def test_the_whole_field_is_used_when_the_filter_is_off(engineer):
    engineer.store.config.engineer.own_class_only = False
    enable(engineer, notifications.FASTEST_SECTOR)
    disable(engineer, notifications.LAP_TIME)
    me = car("Me", player=True, vehicle_class="LMGT3")

    cross_sectors(engineer, "Hyper", player=False, vehicle_class="Hypercar",
                  times=(20.0, 30.0, 20.0), alongside_player=me)
    engineer.speaker.said.clear()
    cross_sectors(engineer, "Hyper", player=False, vehicle_class="Hypercar",
                  times=(18.0, 30.0, 20.0), alongside_player=me)
    assert "has taken" in engineer.speaker.spoken()


def test_a_sim_with_no_classes_is_unaffected(engineer):
    """There is only one class to be in, so filtering by it filters nothing."""
    enable(engineer, notifications.FASTEST_SECTOR)
    disable(engineer, notifications.LAP_TIME)

    cross_sectors(engineer, "Rival", player=False, times=(30.0, 35.0, 25.0))
    engineer.speaker.said.clear()
    cross_sectors(engineer, "Rival", player=False, times=(28.0, 35.0, 25.0))
    assert "has taken" in engineer.speaker.spoken()


def test_a_car_is_not_called_until_it_is_properly_alongside(engineer):
    """Announcing at the outer range calls a car still most of a length back,
    which the driver cannot see, does not believe, and learns to ignore."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    # Inside the hold range (5.0m) but outside the enter range (4.5m), so
    # nothing is said until they come properly alongside.
    alongside(engineer, left=True, distance=10.0, gap=4.8)
    assert engineer.speaker.said == []

    alongside(engineer, left=True, distance=20.0, gap=2.0)   # properly alongside
    assert any("left" in line for line in engineer.speaker.lines())


def test_a_call_is_held_out_to_the_wider_range(engineer):
    """Or it would flicker on and off as two cars breathe."""
    enable(engineer, notifications.SPOTTER, repeat=30.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0, gap=2.0)
    engineer.speaker.said.clear()

    # Drifted back past the enter range but still inside the leave range.
    alongside(engineer, left=True, distance=20.0, gap=4.8)
    assert "clear" not in engineer.speaker.spoken()


# -- per-sim overrides ----------------------------------------------------


def test_the_spotter_range_comes_from_the_sim_s_plugin_settings(engineer):
    """A prototype and a GT car are different lengths, and sims disagree about
    where a car's origin sits — so the number belongs on the profile."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)
    # A car 20m up the road is not alongside in a sim whose cars are 5m...
    engineer.plugins.settings = {"spotter_car_length": 5}
    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0, gap=20.0)
    assert engineer.speaker.said == []

    # ...but is where they are trucks. One number moves every range, which is
    # the point: a length is a fact about the cars, and the four distances are
    # all statements about it.
    engineer.plugins.settings = {"spotter_car_length": 30}
    engineer.behaviours.reset()
    alongside(engineer, distance=40.0)
    alongside(engineer, left=True, distance=50.0, gap=20.0)
    assert any("left" in line for line in engineer.speaker.lines())


def test_swapping_sides_is_a_per_sim_setting(engineer):
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    alongside(engineer, left=True, distance=10.0)
    normal = engineer.speaker.spoken()

    engineer.plugins.settings = {"spotter_swap_sides": True}
    engineer.behaviours.reset()
    engineer.speaker.said.clear()
    alongside(engineer, distance=40.0)
    alongside(engineer, left=True, distance=50.0)
    swapped = engineer.speaker.spoken()

    assert ("left" in normal) != ("left" in swapped)


def test_a_new_track_clears_everything(engineer):
    """Lap distances and sector boundaries belong to a circuit. Carrying them
    over would compare Le Mans against Sebring with a straight face."""
    cross_sectors(engineer)
    assert engineer.sectors.best

    engineer.plugins.session = SessionInfo(
        track="Le Mans", track_length=13600.0,
        cars=(car("Me", player=True),))
    engineer._tick()

    assert engineer.book.best == {}
    assert engineer.sectors.best == {}
    assert engineer.book.track_length == 13600.0


# -- what the spotter says the second time --------------------------------


def test_a_repeat_says_still_there_rather_than_the_call_again():
    """"Car left" twice makes the driver re-check a side they already know
    about. Crew Chief's own vocabulary, and for that reason."""
    one_left = {spotter.LEFT: 1}
    key, again = spotter.standing(one_left)

    assert spotter.arrival(one_left) == spotter.CAR_LEFT
    assert again == spotter.STILL_THERE
    # **The same key**, or the repeat would be a new call and go out on the
    # very next tick instead of on the repeat interval.
    assert key == spotter.arrival_key(one_left)


def test_cars_on_both_sides_repeat_hold_your_line():
    """A different instruction from "still there" on one side: that one means
    do not move *that way*, and this one means do not move."""
    _key, text = spotter.standing({spotter.LEFT: 1, spotter.RIGHT: 1})
    assert text == spotter.HOLD_YOUR_LINE


def test_a_second_car_arriving_is_a_new_call_not_a_repeat():
    one = spotter.arrival_key({spotter.LEFT: 1})
    two = spotter.arrival_key({spotter.LEFT: 2})

    assert one != two
    assert spotter.arrival({spotter.LEFT: 2}) == spotter.THREE_WIDE_RIGHT


def test_both_sides_going_clear_at_once_is_one_call():
    changes = spotter.calls({}, frozenset({"left", "right"}))
    assert [text for _side, text, _urgent in changes] == [spotter.CLEAR_ALL_ROUND]


# -- ranges come from the size of the cars --------------------------------


def test_every_spotter_distance_moves_with_the_car():
    small = spotter.ranges(2.0, 1.5)
    big = spotter.ranges(6.5, 2.5)

    for key in ("overlap", "metres", "min_lateral", "width"):
        assert small[key] < big[key], key
    # And the leave range is past the enter range, or a call would clear
    # itself the instant it was made.
    assert small["metres"] > small["overlap"]


# -- what is up the road --------------------------------------------------


def a_car(name: str, distance: float, speed: float) -> Car:
    return Car(slot=0, driver=name, lap_dist=distance, speed=speed)


def test_braking_for_a_corner_is_not_a_hazard():
    """The rule that fired here was "much slower than you", which is the
    definition of a braking zone. It is gone; only a stopped car is called."""
    own = a_car("Me", 100.0, 70.0)
    braking = a_car("Ahead", 150.0, 30.0)

    assert spotter.ahead(own, [own, braking], track_length=5000.0) is None


def test_a_car_has_to_stay_stopped_before_it_is_called():
    own = a_car("Me", 100.0, 70.0)
    spun = a_car("Ahead", 150.0, 0.5)
    stopped = spotter.Stopped(hold=1.0)

    assert spotter.ahead(own, [own, spun], track_length=5000.0,
                         stopped=stopped, now=0.0) is None
    hazard = spotter.ahead(own, [own, spun], track_length=5000.0,
                           stopped=stopped, now=1.5)
    assert hazard is not None and hazard.driver == "Ahead"
    assert spotter.hazard_call(hazard) == "car stopped ahead"


def test_getting_going_again_clears_the_stopped_memory():
    own = a_car("Me", 100.0, 70.0)
    stopped = spotter.Stopped(hold=1.0)

    spotter.ahead(own, [own, a_car("Ahead", 150.0, 0.5)],
                  track_length=5000.0, stopped=stopped, now=0.0)
    # Rejoined, so the clock starts again rather than carrying on.
    spotter.ahead(own, [own, a_car("Ahead", 150.0, 40.0)],
                  track_length=5000.0, stopped=stopped, now=0.5)
    assert spotter.ahead(own, [own, a_car("Ahead", 150.0, 0.5)],
                         track_length=5000.0, stopped=stopped, now=1.2) is None


# -- asking the engineer a question ---------------------------------------


def grid(engineer, entries, *, elapsed=0.0):
    """Drive a whole grid round a lap, so the books have times for all of them.

    The whole grid in every frame, not one car at a time: the classes the
    engineer will match a question against come from the session it can see
    *now*, and a grid published a car at a time has only ever had one class in
    it at once.
    """
    for distance in range(0, int(TRACK), 20):
        publish(engineer, *(
            car(name, distance=float(distance), laps=0, speed=60.0,
                player=name == entries[0][0], vehicle_class=klass)
            for name, klass, _seconds in entries), elapsed=elapsed)
        elapsed += 0.2
    publish(engineer, *(
        car(name, distance=5.0, laps=1, speed=60.0, last_lap=seconds,
            player=name == entries[0][0], vehicle_class=klass)
        for name, klass, seconds in entries), elapsed=elapsed)


ENDURANCE = (("Estre", "LMGT3", 112.5), ("Nato", "Hypercar", 104.0))


def test_who_has_the_fastest_lap_is_answered_and_never_sent(engineer):
    """The trigger key sends to the whole session, so a question that reached
    the chat box would be somebody's words thrown away -- but so would a
    question that was not recognised. Both halves matter."""
    grid(engineer, ENDURANCE)
    engineer.speaker.said.clear()

    # The player is in the GT3 car, and `own_class_only` is on, so the answer
    # is about the race they are actually in rather than the overall one.
    assert engineer.handle("Chief, who has the fastest lap") is True
    spoken = engineer.speaker.spoken()
    assert "Estre" in spoken and "Nato" not in spoken


def test_a_question_can_name_a_class(engineer):
    grid(engineer, ENDURANCE)
    engineer.speaker.said.clear()

    engineer.handle("Chief, who has the fastest lap in Hypercar")
    spoken = engineer.speaker.spoken()
    assert "Nato" in spoken and "Estre" not in spoken


def test_a_class_nobody_is_in_is_said_so_rather_than_answered_anyway(engineer):
    """Falling back to the overall answer would be a wrong answer stated
    confidently, and the driver would have no way to tell."""
    grid(engineer, ENDURANCE)
    engineer.speaker.said.clear()

    assert engineer.handle("Chief, who has the fastest lap in LMP1") is True
    spoken = engineer.speaker.spoken()
    assert "Estre" not in spoken and "Nato" not in spoken


def test_the_fastest_sector_needs_a_sector(engineer):
    """Asked without one it asks back, rather than picking one."""
    grid(engineer, ENDURANCE)
    engineer.speaker.said.clear()

    assert engineer.handle("Chief, who has the fastest sector") is True
    assert engineer.speaker.said


def test_an_ordinary_sentence_that_starts_like_a_question_is_not_one(engineer):
    """An argument has no end, so this would otherwise be read as a question
    about a class called "of my life that one" and never reach the chat box.
    The defence is that the argument space is closed -- see
    `queries.understood`."""
    grid(engineer, ENDURANCE)
    assert engineer.handle("who has the fastest lap of my life that one") is False


def test_the_same_sentence_addressed_is_a_question(engineer):
    """Somebody who said the engineer's name was talking to it, whatever
    followed. Only the unaddressed path has to be careful."""
    grid(engineer, ENDURANCE)
    assert engineer.handle("Chief, who has the fastest lap of my life") is True


# -- Crew Chief's rules, ported ------------------------------------------


def test_a_car_flying_past_is_not_a_car_alongside():
    """Crew Chief's `max_closing_speed_for_spotter`, and the rule that catches
    the lapping car: something arriving 12 m/s quicker crosses the whole
    overlap window in under a second, so the call describes a car that has
    gone and the driver holds a line for nothing."""
    facing = spotter.heading((0.0, 0.0, 0.0), (0.0, 0.0, 10.0))
    beside = {"Passing": (-3.0, 0.0, 10.5)}

    assert spotter.alongside((0.0, 0.0, 10.0), facing, beside,
                             speeds={"Passing": 80.0}, my_speed=50.0) == []
    # Racing alongside at a similar pace is exactly what this is for.
    assert spotter.alongside((0.0, 0.0, 10.0), facing, beside,
                             speeds={"Passing": 52.0}, my_speed=50.0)


def test_the_spotter_says_nothing_below_walking_pace(engineer):
    """Crew Chief's `min_speed_for_spotter`. Below it you are in the pit lane,
    on the grid, or crawling out of a spin, and every car around you is
    stationary or passing at walking pace."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    for _ in range(3):
        publish(engineer,
                car("Me", player=True, speed=2.0, position=(0.0, 0.0, 10.0)),
                car("Left", speed=2.0, position=(-3.0, 0.0, 10.5)))
    assert engineer.speaker.said == []


def test_the_spotter_falls_silent_under_a_full_course_yellow(engineer):
    """Crew Chief's `fcy_stop_spotter_immediately`, on by default. The whole
    field is bunched at a crawl and permanently overlapping; every call would
    be true and useless."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    for elapsed in (1.0, 1.2, 1.4):
        engineer.plugins.session = SessionInfo(
            track="Sebring", track_length=TRACK, elapsed=elapsed,
            full_course_yellow=True,
            cars=(car("Me", player=True, speed=30.0, position=(0.0, 0.0, 10.0)),
                  car("Left", speed=30.0, position=(-3.0, 0.0, 10.5))))
        engineer._tick()
    assert engineer.speaker.said == []


def test_an_overlap_has_to_last_before_it_is_called(engineer):
    """Crew Chief's `spotter_overlap_delay`. Two cars at the same corner cross
    in and out of overlap as they breathe, and without it the spotter
    chatters."""
    enable(engineer, notifications.SPOTTER, repeat=3.0)
    disable(engineer, notifications.LAP_TIME)

    alongside(engineer, distance=0.0)
    engineer.speaker.said.clear()
    # One frame only, so the overlap has lasted no time at all.
    publish(engineer, car("Me", player=True, position=(0.0, 0.0, 10.0)),
            car("Left", position=(-3.0, 0.0, 11.0)), elapsed=99.0)
    assert engineer.speaker.said == []


def test_the_gap_for_clear_is_metres_not_another_car_length():
    """Half a metre of daylight is half a metre whatever you are driving. A
    second car length would give a kart two metres of hysteresis and hang the
    call on long after they had gone."""
    kart = spotter.ranges(2.0, 1.5)
    assert kart["metres"] - kart["overlap"] == spotter.GAP_FOR_CLEAR
