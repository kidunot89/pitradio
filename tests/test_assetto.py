"""What the Assetto Corsa plugin reads out of the shared memory pages.

Driven against pages **built byte for byte from the same field table the plugin
reads with**, which is worth being honest about: it proves the reader is
self-consistent and that widths, UTF-16 strings and sentinels are handled, and
it cannot prove the table matches the game. That needs `--telemetry` with one
of the three actually running.

What it *can* catch is everything else, and most of the risk here is
everything else: a lap time sentinel becoming an eleven-hour best lap, a
`wchar_t` decoded as UTF-8, a track length derived from a wrap, sector indices
in the wrong convention.
"""

from __future__ import annotations

import struct

import pytest

from pitradio.plugins import acpmf
from pitradio.plugins.assetto import (
    AssettoCorsaPlugin,
    TrackLength,
    player_name,
    seconds,
)

_PACK = {"i": "<i", "f": "<f"}
_WIDTH = {"i": 4, "f": 4}


def page(layout, table, values: dict, size: int) -> bytes:
    """Build one page from the same table the plugin reads."""
    raw = bytearray(size)
    for name, kind, count in layout:
        if name not in values:
            continue
        offset = table[name][0]
        given = values[name]
        if kind == "w":
            encoded = str(given).encode("utf-16-le")[: (count - 1) * 2]
            raw[offset:offset + len(encoded)] = encoded
            continue
        entries = given if isinstance(given, (list, tuple)) else [given]
        for slot, entry in enumerate(entries[:count]):
            struct.pack_into(_PACK[kind], raw,
                             offset + slot * _WIDTH[kind], entry)
    return bytes(raw)


def static_page(**values) -> bytes:
    base = {"smVersion": "1.7", "acVersion": "1.16", "numCars": 3,
            "numberOfSessions": 1, "track": "spa", "playerName": "Geoff",
            "playerSurname": "Taylor", "playerNick": "G.Taylor",
            "sectorCount": 3, "carModel": "porsche_991_gt3_r"}
    base.update(values)
    return page(acpmf.STATIC_LAYOUT, acpmf.STATIC, base, acpmf.STATIC_SIZE)


def graphics_page(**values) -> bytes:
    coordinates = [0.0] * (acpmf.MAX_CARS * 3)
    # Me at the origin, one car to the left, one well away.
    coordinates[0:3] = [0.0, 0.0, 10.0]
    coordinates[3:6] = [-3.0, 0.0, 11.0]
    coordinates[6:9] = [0.0, 0.0, 0.0]

    base = {
        "status": acpmf.STATUS_LIVE, "session": 1, "completedLaps": 4,
        "position": 2, "iCurrentTime": 45_500, "iLastTime": 95_500,
        "iBestTime": 94_000, "isInPit": 0, "currentSectorIndex": 1,
        "lastSectorTime": 30_000, "numberOfLaps": 10,
        "normalizedCarPosition": 0.5, "distanceTraveled": 3491.0,
        "activeCars": 3, "carCoordinates": coordinates,
        "carID": [0, 12, 7] + [0] * (acpmf.MAX_CARS - 3),
    }
    base.update(values)
    return page(acpmf.GRAPHICS_LAYOUT, acpmf.GRAPHICS, base,
                acpmf.GRAPHICS_SIZE)


def physics_page(speed_kmh=180.0) -> bytes:
    return page(acpmf.PHYSICS_LAYOUT, acpmf.PHYSICS,
                {"speedKmh": speed_kmh, "gear": 4, "rpms": 7000},
                acpmf.PHYSICS_SIZE)


@pytest.fixture
def plugin(monkeypatch):
    made = AssettoCorsaPlugin()
    pages = (static_page(), graphics_page(), physics_page())
    monkeypatch.setattr(made, "_read", lambda: pages)
    return made


# -- the pages ------------------------------------------------------------


def test_strings_are_utf16_not_utf8():
    """`wchar_t` on Windows is two bytes. Decoded as UTF-8 a track name comes
    back as its first letter followed by rubbish, which looks enough like a bad
    read to send somebody hunting for the wrong bug."""
    raw = static_page(track="Nürburgring")
    assert acpmf.text(raw, acpmf.STATIC, "track") == "Nürburgring"


def test_fields_read_back_as_they_were_written():
    raw = graphics_page()
    assert acpmf.number(raw, acpmf.GRAPHICS, "completedLaps") == 4
    assert acpmf.number(raw, acpmf.GRAPHICS, "normalizedCarPosition") == \
        pytest.approx(0.5)


def test_a_field_past_the_end_is_none_rather_than_a_guess():
    """Zero is a real lap count, a real sector index and a real coordinate."""
    raw = graphics_page()
    assert acpmf.number(raw, acpmf.GRAPHICS, "carID", 999) is None
    assert acpmf.number(raw, acpmf.GRAPHICS, "nonesuch") is None
    assert acpmf.number(b"", acpmf.GRAPHICS, "completedLaps") is None


def test_car_coordinates_come_out_per_car():
    raw = graphics_page()
    assert acpmf.coordinates(raw, 0) == (0.0, 0.0, 10.0)
    assert acpmf.coordinates(raw, 1) == (-3.0, 0.0, 11.0)


def test_pages_that_do_not_look_like_assetto_corsa_are_refused():
    """Wrong offsets do not raise. They produce a nameless track and a grid of
    hundreds, and the engineer would talk about them."""
    assert acpmf.plausible(static_page(), graphics_page()) is True
    assert acpmf.plausible(static_page(numCars=9000), graphics_page()) is False
    assert acpmf.plausible(static_page(), graphics_page(status=99)) is False
    assert acpmf.plausible(b"", b"") is False


# -- times ----------------------------------------------------------------


def test_the_no_time_sentinel_is_not_a_lap():
    """Left alone it becomes a lap of eleven hours — which never wins anything,
    but is a perfectly valid best lap as far as the trainers are concerned, and
    they would target it."""
    assert seconds(99_999_999) == 0.0
    assert seconds(0) == 0.0
    assert seconds(-1) == 0.0
    assert seconds(None) == 0.0


def test_a_real_time_is_milliseconds():
    assert seconds(95_500) == pytest.approx(95.5)


# -- who is driving -------------------------------------------------------


def test_the_nickname_is_preferred():
    """It is what Assetto Corsa shows other people online."""
    assert player_name(static_page()) == "G.Taylor"


def test_the_real_name_is_the_fallback():
    assert player_name(static_page(playerNick="")) == "Geoff Taylor"


def test_there_is_always_a_name():
    """It is the key the lap book files laps under, and an empty one would file
    every lap under the same blank name."""
    assert player_name(
        static_page(playerNick="", playerName="", playerSurname="")) == "You"


# -- track length ---------------------------------------------------------


def test_the_track_length_is_measured_rather_than_looked_up():
    """Distance covered over fraction of a lap covered is the length, whether
    `distanceTraveled` counts the lap or the session."""
    length = TrackLength()
    assert length.observe(0.10, 700.0) == 0.0        # nothing to compare yet
    assert length.observe(0.20, 1400.0) == pytest.approx(7000.0)


def test_crossing_the_line_does_not_produce_a_length():
    """The fraction wraps to zero there and the division goes negative."""
    length = TrackLength()
    length.observe(0.98, 6860.0)
    assert length.observe(0.01, 7070.0) == 0.0


def test_a_step_too_small_to_divide_is_ignored():
    length = TrackLength()
    length.observe(0.100, 700.0)
    assert length.observe(0.101, 707.0) == 0.0


def test_an_implausible_length_is_rejected():
    """A kart track at one end, the Nordschleife at the other. Outside that it
    came from a wrap or a teleport."""
    length = TrackLength()
    length.observe(0.10, 0.0)
    assert length.observe(0.20, 10.0) == 0.0         # a 100m lap

    far = TrackLength()
    far.observe(0.10, 0.0)
    assert far.observe(0.20, 100_000.0) == 0.0


def test_the_length_is_kept_once_found():
    """It does not change during a session, and later samples are noisier
    rather than better."""
    length = TrackLength()
    length.observe(0.10, 700.0)
    length.observe(0.20, 1400.0)
    length.observe(0.30, 2107.0)
    assert length.metres == pytest.approx(7000.0)


# -- the cars -------------------------------------------------------------


def test_the_player_comes_out_with_what_the_engineer_needs(plugin):
    plugin.session()                    # a first read, so the length is known
    me = plugin._safe_cars()[0]

    assert me.is_player is True and me.control == 0
    assert me.driver == "G.Taylor"
    assert me.laps == 4
    assert me.last_lap == pytest.approx(95.5)
    assert me.best_lap == pytest.approx(94.0)
    assert me.speed == pytest.approx(50.0)          # 180 km/h
    assert me.in_pits is False


def test_other_cars_are_positions_and_nothing_else(plugin):
    """The pages carry an id and a set of coordinates, and that is the whole of
    it."""
    cars = plugin._safe_cars()
    others = [car for car in cars if not car.is_player]

    assert [car.driver for car in others] == ["Car 12"]
    assert others[0].position == (-3.0, 0.0, 11.0)
    assert others[0].laps == 0 and others[0].last_lap == 0.0


def test_a_car_at_the_origin_is_not_on_track(plugin):
    """Which is where the pages leave a slot nobody is using."""
    assert "Car 7" not in [car.driver for car in plugin._safe_cars()]


def test_other_cars_are_not_offered_as_drivers(plugin):
    """A mention nobody recognises is a message that reads as a mistake."""
    assert plugin.drivers() == ["G.Taylor"]
    assert plugin.vocabulary() == ["G.Taylor"]


def test_there_are_no_standings(plugin):
    """The page gives the player's own position and nobody's name, so "P3"
    could only resolve to the player or to nothing."""
    assert plugin.standings().overall == {}
    assert not plugin.standings()


def test_the_session_clock_is_the_current_lap_time(plugin):
    """There is no session clock in these pages — only the time left, which
    counts down and is zero in a lap-limited race."""
    assert plugin.session().elapsed == pytest.approx(45.5)


def test_the_session_carries_the_track_and_the_cars(plugin):
    session = plugin.session()
    assert session.track == "spa"
    assert session.has_data is True
    assert len(session.cars) == 2


def test_there_is_no_voice_room(plugin):
    assert plugin.session().key == ""


def test_what_it_claims_to_provide_matches_what_it_has():
    """No `field`: the pages carry no lap data or names for anybody but the
    player, so anything comparing you to the grid would compare you to
    yourself and call it the session."""
    from pitradio.plugins import base

    made = AssettoCorsaPlugin()
    assert base.PROVIDES_POSITIONS in made.provides
    assert base.PROVIDES_LAPS in made.provides
    assert base.PROVIDES_SECTORS in made.provides
    assert base.PROVIDES_FIELD not in made.provides


def test_nothing_readable_costs_the_data_not_the_app(monkeypatch):
    made = AssettoCorsaPlugin()
    monkeypatch.setattr(made, "_read", lambda: None)

    assert made.drivers() == []
    assert made.session().has_data is False
    assert made.standings().overall == {}


# -- sectors --------------------------------------------------------------


def test_the_sector_index_is_translated_to_the_app_s_numbering(monkeypatch):
    """`Car.sector` numbers sector three as 0, which is rFactor's convention
    and the one the sector book untangles."""
    made = AssettoCorsaPlugin()

    def at(index):
        monkeypatch.setattr(
            made, "_read",
            lambda: (static_page(), graphics_page(currentSectorIndex=index),
                     physics_page()))
        return made._safe_cars()[0].sector

    assert at(0) == 1
    assert at(1) == 2
    assert at(2) == 0


def test_sector_times_accumulate_into_cumulative_splits(monkeypatch):
    """Assetto Corsa gives a sector index and the time of the one that just
    ended, and nothing cumulative — so the running totals are kept here."""
    made = AssettoCorsaPlugin()

    def read(index, last_ms, laps=4):
        pages = (static_page(),
                 graphics_page(currentSectorIndex=index, lastSectorTime=last_ms,
                               completedLaps=laps),
                 physics_page())
        monkeypatch.setattr(made, "_read", lambda: pages)
        return made._safe_cars()[0]

    read(0, 0)                       # in sector one
    car = read(1, 30_000)            # sector one done in 30s
    assert car.cur_sector1 == pytest.approx(30.0)

    car = read(2, 35_000)            # sector two done in 35s
    assert car.cur_sector1 == pytest.approx(30.0)
    assert car.cur_sector2 == pytest.approx(65.0)
