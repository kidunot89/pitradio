"""What the iRacing plugin reads out of the shared memory block.

Driven against a block **built byte for byte by hand**, because that is the
only honest check available on a machine without iRacing: the layout is
`struct` offsets and the plugin is a set of channel names, and both are wrong
in ways that produce no error. A misnamed channel reads as absent and the
behaviour goes quiet; a wrong offset reads a *different* channel and produces
plausible numbers the engineer would then talk about.

What this cannot check is the assumption underneath: that iRacing puts what I
think it does in `CarIdxLapDistPct`, that `CarLeftRight` uses the values mapped
here, that `CarIdxTrackSurface` is -1 in the garage. Those need the game.
"""

from __future__ import annotations

import struct

import pytest

from pitradio.plugins import irsdk
from pitradio.plugins.iracing import IRacingPlugin, Speeds, sides, track_length

SESSION = """---
WeekendInfo:
 TrackName: watkinsglen
 TrackLength: 5.55 km
 TrackDisplayName: Watkins Glen International
DriverInfo:
 DriverCarIdx: 0
 Drivers:
 - CarIdx: 0
   UserName: Geoff Taylor
   CarNumber: "64"
   CarClassShortName: GT3
   CarIsPaceCar: 0
 - CarIdx: 1
   UserName: Nick Tandy
   CarNumber: "911"
   CarClassShortName: GT3
   CarIsPaceCar: 0
 - CarIdx: 2
   UserName: Pace Car
   CarNumber: "0"
   CarClassShortName: PACE
   CarIsPaceCar: 1
"""

#: (name, type, count) for the channels the plugin actually asks for.
CHANNELS = (
    ("SessionTime", irsdk.DOUBLE, 1),
    ("PlayerCarIdx", irsdk.INT, 1),
    ("CarLeftRight", irsdk.BITFIELD, 1),
    ("CarIdxTrackSurface", irsdk.INT, 3),
    ("CarIdxLapDistPct", irsdk.FLOAT, 3),
    ("CarIdxLapCompleted", irsdk.INT, 3),
    ("CarIdxLastLapTime", irsdk.FLOAT, 3),
    ("CarIdxBestLapTime", irsdk.FLOAT, 3),
    ("CarIdxOnPitRoad", irsdk.BOOL, 3),
    ("CarIdxPosition", irsdk.INT, 3),
)

_PACK = {irsdk.DOUBLE: "<d", irsdk.INT: "<i", irsdk.BITFIELD: "<I",
         irsdk.FLOAT: "<f", irsdk.BOOL: "<?"}
_WIDTH = {irsdk.DOUBLE: 8, irsdk.INT: 4, irsdk.BITFIELD: 4, irsdk.FLOAT: 4,
          irsdk.BOOL: 1}


def build(values: dict, *, session=SESSION, status=irsdk.STATUS_CONNECTED,
          ticks=(1, 9, 3, 2)) -> bytes:
    """A real iRacing block: header, var headers, four buffers, session string.

    `ticks` decides which buffer is newest, so the rotation can be exercised
    rather than assumed.
    """
    var_offset = 4096
    buffer_offset = 16384
    buffer_length = 4096
    session_offset = 65536

    layout, cursor = {}, 0
    for name, kind, count in CHANNELS:
        layout[name] = (kind, cursor, count)
        cursor += _WIDTH[kind] * count

    encoded = session.encode("utf-8")
    size = session_offset + len(encoded) + 16
    block = bytearray(size)

    struct.pack_into(
        "<12i", block, 0,
        2, status, 60, 1, len(encoded), session_offset,
        len(CHANNELS), var_offset, len(ticks), buffer_length, 0, 0)

    for index, tick in enumerate(ticks):
        struct.pack_into("<2i2i", block, irsdk.HEADER_SIZE + index * 16,
                         tick, buffer_offset + index * buffer_length, 0, 0)

    for index, (name, kind, count) in enumerate(CHANNELS):
        struct.pack_into(
            "<3i4s32s64s32s", block, var_offset + index * irsdk.VAR_HEADER_SIZE,
            kind, layout[name][1], count, b"\x00" * 4,
            name.encode("ascii"), b"desc", b"unit")

    # Written into the newest buffer, which is the one the plugin must pick.
    newest = buffer_offset + ticks.index(max(ticks)) * buffer_length
    for name, entries in values.items():
        kind, offset, _count = layout[name]
        for slot, entry in enumerate(entries):
            struct.pack_into(_PACK[kind], block,
                             newest + offset + slot * _WIDTH[kind], entry)

    block[session_offset:session_offset + len(encoded)] = encoded
    return bytes(block)


FRAME = {
    "SessionTime": [1234.5],
    "PlayerCarIdx": [0],
    "CarLeftRight": [2],
    "CarIdxTrackSurface": [3, 3, -1],
    "CarIdxLapDistPct": [0.25, 0.5, 0.0],
    "CarIdxLapCompleted": [4, 5, 0],
    "CarIdxLastLapTime": [95.5, 93.25, -1.0],
    "CarIdxBestLapTime": [94.0, 92.0, -1.0],
    "CarIdxOnPitRoad": [False, True, False],
    "CarIdxPosition": [2, 1, 0],
}


@pytest.fixture
def plugin(monkeypatch):
    """A plugin wired to a block in memory rather than to the game."""
    made = IRacingPlugin()
    raw = build(FRAME)

    def read():
        header = irsdk.Header(raw)
        return raw, header, irsdk.channels(raw, header), header.latest()

    monkeypatch.setattr(made, "_read", read)
    return made


# -- the block ------------------------------------------------------------


def test_the_newest_buffer_is_the_one_read():
    """By tick count, never by index. Taking a fixed buffer works until the sim
    wraps onto it mid-read, and then one car's distance comes from one frame
    and its speed from another."""
    raw = build(FRAME, ticks=(1, 9, 3, 2))
    header = irsdk.Header(raw)
    assert header.latest() == 16384 + 1 * 4096

    raw = build(FRAME, ticks=(7, 2, 3, 4))
    assert irsdk.Header(raw).latest() == 16384


def test_channels_are_found_by_name():
    raw = build(FRAME)
    found = irsdk.channels(raw, irsdk.Header(raw))
    assert "CarIdxLapDistPct" in found
    assert found["CarIdxLapDistPct"].count == 3
    assert found["SessionTime"].type == irsdk.DOUBLE


def test_values_read_back_as_they_were_written():
    raw = build(FRAME)
    header = irsdk.Header(raw)
    found = irsdk.channels(raw, header)
    offset = header.latest()

    assert irsdk.value(raw, offset, found["SessionTime"]) == pytest.approx(1234.5)
    assert irsdk.values(raw, offset, found["CarIdxLapDistPct"]) == \
        pytest.approx([0.25, 0.5, 0.0])
    assert irsdk.values(raw, offset, found["CarIdxOnPitRoad"]) == [False, True, False]


def test_reading_past_the_end_is_none_rather_than_a_guess():
    """A zero here is indistinguishable from a real reading of zero."""
    raw = build(FRAME)
    header = irsdk.Header(raw)
    channel = irsdk.channels(raw, header)["CarIdxLapDistPct"]
    assert irsdk.value(raw, header.latest(), channel, 99) is None
    assert irsdk.value(raw, header.latest(), None) is None


def test_a_block_that_is_not_in_a_session_is_not_connected():
    """iRacing leaves the mapping in place when you leave the car."""
    raw = build(FRAME, status=0)
    assert irsdk.Header(raw).connected is False


# -- the session string ---------------------------------------------------


def test_the_driver_list_comes_out_of_the_session_string():
    raw = build(FRAME)
    parsed = irsdk.parse_session(irsdk.session_string(raw, irsdk.Header(raw)))
    names = [d["UserName"] for d in parsed["DriverInfo"]["Drivers"]]
    assert names == ["Geoff Taylor", "Nick Tandy", "Pace Car"]


def test_a_list_indented_level_with_its_key_still_parses():
    """The shape a flat stack-based parser gets wrong, and iRacing's own."""
    parsed = irsdk.parse_session(
        "Root:\n Things:\n - Id: 1\n   Name: one\n - Id: 2\n   Name: two\n")
    assert parsed["Root"]["Things"] == [{"Id": 1, "Name": "one"},
                                        {"Id": 2, "Name": "two"}]


def test_a_quoted_number_stays_a_string():
    """Car numbers are "007" and must not become 7."""
    parsed = irsdk.parse_session('Root:\n CarNumber: "007"\n')
    assert parsed["Root"]["CarNumber"] == "007"


def test_a_line_that_makes_no_sense_is_skipped_not_fatal():
    """A ~200KB document from another process; refusing to read the driver list
    because one line was odd would be a poor trade."""
    parsed = irsdk.parse_session("Root:\n Good: 1\n this line has no colon\n")
    assert parsed["Root"]["Good"] == 1


def test_an_empty_session_string_is_empty_rather_than_an_error():
    assert irsdk.parse_session("") == {}


# -- track length ---------------------------------------------------------


def test_track_length_is_read_with_its_unit():
    assert track_length("5.55 km") == pytest.approx(5550.0)
    assert track_length("2.5 mi") == pytest.approx(4023.36)
    assert track_length(5.55) == pytest.approx(5550.0)


def test_an_unreadable_track_length_is_zero_not_a_guess():
    """It decides whether a recorded lap covered the whole circuit, and a wrong
    length silently accepts half-laps as reference laps."""
    assert track_length("") == 0.0
    assert track_length(None) == 0.0
    assert track_length("who knows") == 0.0


# -- the spotter ----------------------------------------------------------


def test_car_left_right_maps_to_sides():
    assert sides(0) == {}                       # off
    assert sides(1) == {}                       # clear
    assert sides(2) == {"left": 1}
    assert sides(3) == {"right": 1}
    assert sides(4) == {"left": 1, "right": 1}
    assert sides(5) == {"left": 2}
    assert sides(6) == {"right": 2}


def test_an_unknown_left_right_value_says_nobody():
    """A spotter inventing a car is worse than one that missed it: the driver
    leaves room for somebody who is not there, and stops trusting the next
    call."""
    assert sides(99) == {}
    assert sides(None) == {}


# -- derived speed --------------------------------------------------------


def test_speed_is_derived_from_how_far_a_car_moved():
    """iRacing publishes a speed for the player and nobody else."""
    speeds = Speeds()
    assert speeds.of("A", 0.0, 0.0, 5000.0) == 0.0        # nothing to compare
    assert speeds.of("A", 1.0, 50.0, 5000.0) == pytest.approx(50.0)


def test_two_reads_too_close_together_produce_no_speed():
    """Below a threshold this measures the noise on a lap-distance fraction."""
    speeds = Speeds()
    speeds.of("A", 0.0, 0.0, 5000.0)
    assert speeds.of("A", 0.001, 0.1, 5000.0) == 0.0


def test_crossing_the_line_does_not_read_as_going_backwards():
    speeds = Speeds()
    speeds.of("A", 0.0, 4950.0, 5000.0)
    assert speeds.of("A", 1.0, 10.0, 5000.0) == pytest.approx(60.0)


def test_being_teleported_to_the_pits_produces_no_speed():
    speeds = Speeds()
    speeds.of("A", 0.0, 4000.0, 5000.0)
    assert speeds.of("A", 1.0, 100.0, 5000.0) == 0.0


# -- the cars -------------------------------------------------------------


def test_the_cars_come_out_with_what_the_engineer_needs(plugin):
    cars = plugin._safe_cars()
    assert [car.driver for car in cars] == ["Geoff Taylor", "Nick Tandy"]

    me, rival = cars
    assert me.is_player is True and me.control == 0
    assert rival.is_player is False
    assert me.lap_dist == pytest.approx(0.25 * 5550.0)
    assert rival.lap_dist == pytest.approx(0.5 * 5550.0)
    assert me.laps == 4 and rival.laps == 5
    assert me.last_lap == pytest.approx(95.5)
    assert rival.in_pits is True
    assert me.place == 2 and rival.place == 1
    assert me.vehicle_class == "GT3"


def test_a_car_not_in_the_world_is_left_out(plugin):
    """Lap distance zero is a real place — the start line — so a car in the
    garage reported there would be sitting on it."""
    assert "Pace Car" not in [car.driver for car in plugin._safe_cars()]


def test_the_pace_car_is_excluded_unless_asked_for(plugin):
    """Not offered as a driver to mention or a lap to chase."""
    assert "Pace Car" not in plugin.drivers()


def test_no_lap_time_is_zero_rather_than_negative(plugin):
    """Negative is how iRacing says "no time", and letting it through would
    beat every real lap."""
    assert all(car.last_lap >= 0 and car.best_lap >= 0
               for car in plugin._safe_cars())


def test_the_session_carries_the_track_and_the_spotter_call(plugin):
    session = plugin.session()
    assert session.track == "Watkins Glen International"
    assert session.track_length == pytest.approx(5550.0)
    assert session.elapsed == pytest.approx(1234.5)
    assert session.alongside == {"left": 1}
    assert session.has_data is True


def test_there_is_no_voice_room(plugin):
    """Without other cars' world positions there is no proximity, and a room
    with no proximity in a forty-car field is not switched on by accident."""
    assert plugin.session().key == ""
    assert not plugin.session()


def test_standings_are_built_overall_and_per_class(plugin):
    standings = plugin.standings()
    assert standings.overall == {1: "Nick Tandy", 2: "Geoff Taylor"}
    assert standings.by_class["GT3"] == {1: "Nick Tandy", 2: "Geoff Taylor"}


def test_what_it_claims_to_provide_matches_what_it_has():
    """iRacing publishes no other-car world positions and no per-car sector
    splits. Claiming either is how a behaviour ends up switched on and
    permanently silent."""
    from pitradio.plugins import base

    plugin = IRacingPlugin()
    assert base.PROVIDES_LAPS in plugin.provides
    assert base.PROVIDES_SPOTTER in plugin.provides
    assert base.PROVIDES_POSITIONS not in plugin.provides
    assert base.PROVIDES_SECTORS not in plugin.provides


def test_nothing_readable_costs_the_data_not_the_app(monkeypatch):
    made = IRacingPlugin()
    monkeypatch.setattr(made, "_read", lambda: None)

    assert made.drivers() == []
    assert made.standings().overall == {}
    assert made.session().has_data is False
