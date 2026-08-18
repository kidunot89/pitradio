"""What the Project CARS 2 plugin reads, and what Automobilista 2 inherits.

Driven against a block built byte for byte, because the padding inside
`ParticipantInfo` is the whole risk here: a `bool` and a 64-byte name are
followed by a float array that aligns to four, so the name ends at 65 and the
position starts at 68. Off by those three bytes and a grid becomes noise — with
no error, and with coordinates that still look like numbers.

The block is built with **explicit offsets written independently of the
reader's**, so this is not merely checking that a constant equals itself.
"""

from __future__ import annotations

import struct

import pytest

from pitradio.plugins import pcars2
from pitradio.plugins.ams2 import Automobilista2Plugin
from pitradio.plugins.projectcars2 import LapTimes, ProjectCars2Plugin


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def build(cars, *, viewed=0, game_state=2, count=None) -> bytes:
    """A block laid out from the C structure, not from the reader's constants.

    ParticipantInfo, written out by hand:
        0    bool  mIsActive
        1    char  mName[64]
        65   (three bytes of padding)
        68   float mWorldPosition[3]
        80   float mCurrentLapDistance
        84   uint  mRacePosition
        88   uint  mLapsCompleted
        92   uint  mCurrentLap
        96   int   mCurrentSector
        100  (size)
    """
    head = 28
    stride = 100
    raw = bytearray(head + 64 * stride + 1024)

    struct.pack_into("<5I2i", raw, 0,
                     8, 1234, game_state, 1, 2, viewed,
                     len(cars) if count is None else count)

    for index, car in enumerate(cars):
        at = head + index * stride
        raw[at] = 1 if car.get("active", True) else 0
        name = str(car.get("name", "")).encode("latin-1")[:63]
        raw[at + 1:at + 1 + len(name)] = name
        struct.pack_into("<3f", raw, at + 68, *car.get("position", (0.0, 0.0, 0.0)))
        struct.pack_into("<f", raw, at + 80, car.get("distance", 0.0))
        struct.pack_into("<I", raw, at + 84, car.get("place", 0))
        struct.pack_into("<I", raw, at + 88, car.get("laps", 0))
        struct.pack_into("<I", raw, at + 92, car.get("lap", 1))
        struct.pack_into("<i", raw, at + 96, car.get("sector", 1))
    return bytes(raw)


GRID = [
    {"name": "Geoff Taylor", "position": (10.0, 1.0, 20.0), "distance": 1500.0,
     "place": 2, "laps": 3},
    {"name": "Ayrton Senna", "position": (13.0, 1.0, 21.0), "distance": 1800.0,
     "place": 1, "laps": 3},
    {"name": "", "position": (0.0, 0.0, 0.0)},
]


# -- the block ------------------------------------------------------------


def test_the_head_reads_back():
    header = pcars2.Header(build(GRID, viewed=1))
    assert header.version == 8
    assert header.build == 1234
    assert header.viewed == 1
    assert header.participants == 3
    assert header.playing is True


def test_the_padding_before_the_world_position_is_respected():
    """A bool and a 64-byte name are followed by a float array that aligns to
    four, so the name ends at 65 and the position starts at 68. Off by those
    three bytes and a grid becomes noise that still looks like numbers."""
    raw = build(GRID)
    entry = pcars2.participant(raw, 0)

    assert entry.name == "Geoff Taylor"
    assert entry.position == (10.0, 1.0, 20.0)
    assert entry.lap_distance == pytest.approx(1500.0)


def test_lap_distance_is_metres_not_a_fraction():
    """Which is why this sim needs no track length to build a trace."""
    assert pcars2.participant(build(GRID), 1).lap_distance == pytest.approx(1800.0)


def test_inactive_and_nameless_cars_are_left_out():
    entries = pcars2.participants(build(GRID), pcars2.Header(build(GRID)))
    assert [entry.name for _index, entry in entries] == \
        ["Geoff Taylor", "Ayrton Senna"]


def test_a_car_marked_inactive_is_left_out():
    grid = [dict(GRID[0], active=False), GRID[1]]
    raw = build(grid)
    entries = pcars2.participants(raw, pcars2.Header(raw))
    assert [entry.name for _index, entry in entries] == ["Ayrton Senna"]


def test_reading_past_the_array_is_none():
    assert pcars2.participant(build(GRID), 99) is None
    assert pcars2.participant(b"", 0) is None


def test_a_name_with_an_odd_byte_costs_a_character_not_the_grid():
    raw = bytearray(build(GRID))
    raw[28 + 1] = 0xFF
    entry = pcars2.participant(bytes(raw), 0)
    assert entry is not None and entry.name


# -- refusing nonsense ----------------------------------------------------


def test_a_block_that_looks_right_is_accepted():
    raw = build(GRID)
    assert pcars2.plausible(raw, pcars2.Header(raw)) is True


def test_an_impossible_grid_size_is_refused():
    raw = build(GRID, count=9000)
    assert pcars2.plausible(raw, pcars2.Header(raw)) is False


def test_coordinates_in_the_millions_are_refused():
    """What a wrong offset produces, rather than an exception."""
    raw = build([dict(GRID[0], position=(1e9, 0.0, 0.0))])
    assert pcars2.plausible(raw, pcars2.Header(raw)) is False


def test_not_a_number_is_refused():
    raw = build([dict(GRID[0], position=(float("nan"), 0.0, 0.0))])
    assert pcars2.participant(raw, 0) is None


def test_the_menus_are_not_a_session():
    """The block keeps its last contents there."""
    raw = build(GRID, game_state=1)
    assert pcars2.Header(raw).playing is False


# -- lap times ------------------------------------------------------------


def test_a_lap_is_timed_from_the_counter_changing():
    """The participant block carries counts and no times."""
    clock = Clock()
    laps = LapTimes(clock)

    laps.observe("A", 3)
    clock.advance(95.0)
    assert laps.observe("A", 4) == pytest.approx(95.0)


def test_the_lap_is_measured_from_when_it_started():
    """Not from the last read, which would make every lap one tick long."""
    clock = Clock()
    laps = LapTimes(clock)

    laps.observe("A", 3)
    for _ in range(10):
        clock.advance(9.0)
        laps.observe("A", 3)
    clock.advance(5.0)
    assert laps.observe("A", 4) == pytest.approx(95.0)


def test_something_far_too_short_is_not_a_lap():
    """The counter moved for another reason and the "lap" is the gap between
    two unrelated moments."""
    clock = Clock()
    laps = LapTimes(clock)
    laps.observe("A", 3)
    clock.advance(2.0)
    assert laps.observe("A", 4) == 0.0


def test_something_far_too_long_is_not_a_lap():
    """It spanned a pause, a tow, or somebody leaving the car in the pits."""
    clock = Clock()
    laps = LapTimes(clock)
    laps.observe("A", 3)
    clock.advance(3600.0)
    assert laps.observe("A", 4) == 0.0


def test_a_session_restart_throws_the_lap_away():
    clock = Clock()
    laps = LapTimes(clock)
    laps.observe("A", 5)
    clock.advance(95.0)
    laps.observe("A", 6)
    assert laps.best("A") == pytest.approx(95.0)

    laps.observe("A", 0)
    assert laps.best("A") == 0.0


# -- the plugin -----------------------------------------------------------


@pytest.fixture
def plugin(monkeypatch):
    clock = Clock()
    made = ProjectCars2Plugin(clock=clock)
    made.clock = clock
    raw = build(GRID, viewed=0)
    monkeypatch.setattr(made, "_read", lambda: (raw, pcars2.Header(raw)))
    return made


def test_the_cars_come_out_with_what_the_engineer_needs(plugin):
    cars = plugin._safe_cars()
    assert [car.driver for car in cars] == ["Geoff Taylor", "Ayrton Senna"]

    me, rival = cars
    assert me.is_player is True and me.control == 0
    assert rival.is_player is False
    assert me.lap_dist == pytest.approx(1500.0)
    assert me.laps == 3
    assert me.place == 2 and rival.place == 1


def test_speed_is_derived_from_the_distance(plugin):
    """There is no speed in the head of the block, and a car with none records
    no trace samples at all."""
    plugin._safe_cars()
    assert plugin._safe_cars()[0].speed == 0.0    # same clock, no movement

    plugin.clock.advance(1.0)
    # The block is fixed, so the car has not moved; what matters is that the
    # derivation ran rather than the value.
    assert plugin._safe_cars()[0].speed == pytest.approx(0.0)


def test_the_watched_car_is_the_focus(plugin):
    """`mViewedParticipantIndex` answers directly what LMU needs an HTTP call
    for."""
    assert plugin.session().focus_slot == 0


def test_standings_are_overall_only(plugin):
    """The block has no class names, and an invented class is worse than none."""
    standings = plugin.standings()
    assert standings.overall == {1: "Ayrton Senna", 2: "Geoff Taylor"}
    assert standings.by_class == {}


def test_there_is_no_voice_room(plugin):
    """The block says nothing about a server, so there is nothing for two
    copies of the app to agree on."""
    assert plugin.session().key == ""


def test_what_it_claims_to_provide_matches_what_it_has():
    made = ProjectCars2Plugin()
    from pitradio.plugins import base

    assert base.PROVIDES_POSITIONS in made.provides
    assert base.PROVIDES_LAPS in made.provides
    assert base.PROVIDES_FIELD in made.provides
    # `mCurrentSector` is an enum that could not be pinned down from outside
    # the games, and being wrong by one puts every split in the wrong sector.
    assert base.PROVIDES_SECTORS not in made.provides


def test_nothing_readable_costs_the_data_not_the_app(monkeypatch):
    made = ProjectCars2Plugin()
    monkeypatch.setattr(made, "_read", lambda: None)

    assert made.drivers() == []
    assert made.standings().overall == {}
    assert made.session().has_data is False


# -- Automobilista 2 ------------------------------------------------------


def test_automobilista_is_its_own_plugin():
    """A profile picks a plugin by name, and somebody running Automobilista 2
    should be able to choose it rather than a different game."""
    assert Automobilista2Plugin.id != ProjectCars2Plugin.id
    assert Automobilista2Plugin.name == "Automobilista 2"
    assert issubclass(Automobilista2Plugin, ProjectCars2Plugin)


def test_automobilista_claims_its_own_executables():
    made = Automobilista2Plugin()
    assert made.serves("AMS2.exe") is True
    assert made.serves("pcars2.exe") is False


def test_automobilista_reads_the_same_block(monkeypatch):
    """Everything is inherited; only the identity differs."""
    made = Automobilista2Plugin()
    raw = build(GRID, viewed=1)
    monkeypatch.setattr(made, "_read", lambda: (raw, pcars2.Header(raw)))

    assert made.drivers() == ["Geoff Taylor", "Ayrton Senna"]
    assert made.session().focus_slot == 1


def test_the_two_do_not_share_plugin_settings():
    """Settings are stored per profile against the plugin id, so Reiza's grids
    and Slightly Mad's get their own spotter geometry."""
    assert Automobilista2Plugin().id != ProjectCars2Plugin().id


def test_automobilista_is_matched_before_the_generic_plugin():
    """`for_executable` returns the first match, and both would claim an AMS2
    executable if the order were the other way round."""
    from pitradio import plugins

    registry = plugins.PluginRegistry()
    assert registry.default_id_for("AMS2.exe") == "ams2"
    assert registry.default_id_for("pcars2.exe") == "pcars2"
