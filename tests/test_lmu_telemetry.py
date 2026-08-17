"""What the LMU plugin actually reads out of the shared memory block.

Driven against the **real vendored struct**, filled in by hand. That is the
whole point of this file: `LMUObjectOut` is a ctypes structure and ctypes does
not need Windows to lay one out, so a block can be built in memory anywhere and
the plugin pointed straight at it.

Which matters more than it sounds. Every other test of this plugin stubs it
out, so a field name that does not exist — `mLastSector` for `mLastSector2`, or
`mSectorIndex` for `mSector` — would raise only on a machine with the game
running, and only at the moment somebody was driving. Reading a *wrong but real*
field is worse still: it produces plausible numbers, and the engineer would
confidently talk about them.

The engineer added a dozen of these reads at once. This is what checks them.
"""

from __future__ import annotations

import sys

import pytest

from pitradio.plugins import lmu

lmu_data = pytest.importorskip(
    "pylmusharedmemory.lmu_data",
    reason="the vendored LMU struct definitions are not importable")


TRACK_LENGTH = 6000.0


def build(cars, *, track="Sebring", elapsed=123.5, address=0, port=0):
    """A plugin wired to a hand-filled block. Returns (plugin, keepalive).

    The block has to be kept alive by the caller: the plugin holds a reference
    through `_data`, and a garbage-collected structure would leave it reading
    freed memory — which is the one failure mode that would not look like a
    test problem.
    """
    block = lmu_data.LMUObjectOut()
    info = block.scoring.scoringInfo
    info.mTrackName = track.encode("utf-8")
    info.mLapDist = TRACK_LENGTH
    info.mCurrentET = elapsed
    info.mNumVehicles = len(cars)
    info.mServerPublicIP = address
    info.mServerPort = port

    for index, spec in enumerate(cars):
        vehicle = block.scoring.vehScoringInfo[index]
        vehicle.mID = spec.get("slot", index)
        vehicle.mDriverName = spec["driver"].encode("utf-8")
        vehicle.mVehicleClass = spec.get("vehicle_class", "LMGT3").encode("utf-8")
        vehicle.mPlace = spec.get("place", index + 1)
        vehicle.mControl = spec.get("control", 2)
        vehicle.mIsPlayer = spec.get("is_player", False)
        vehicle.mInPits = spec.get("in_pits", False)
        vehicle.mLapDist = spec.get("lap_dist", 0.0)
        vehicle.mTotalLaps = spec.get("laps", 0)
        vehicle.mLastLapTime = spec.get("last_lap", -1.0)
        vehicle.mBestLapTime = spec.get("best_lap", -1.0)
        vehicle.mSector = spec.get("sector", 1)
        vehicle.mCurSector1 = spec.get("cur1", -1.0)
        vehicle.mCurSector2 = spec.get("cur2", -1.0)
        vehicle.mLastSector1 = spec.get("last1", -1.0)
        vehicle.mLastSector2 = spec.get("last2", -1.0)

        x, y, z = spec.get("position", (0.0, 0.0, 0.0))
        vehicle.mPos.x, vehicle.mPos.y, vehicle.mPos.z = x, y, z
        vx, vy, vz = spec.get("velocity", (0.0, 0.0, 0.0))
        vehicle.mLocalVel.x = vx
        vehicle.mLocalVel.y = vy
        vehicle.mLocalVel.z = vz

    plugin = lmu.LeMansUltimatePlugin()
    plugin._data = block
    return plugin, block


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    """Never ask the running game which car is on screen.

    Left alone this is answered for real on any machine with LMU open, and
    every assertion below would silently depend on what the person at that desk
    was watching. CLAUDE.md records this after it happened.
    """
    monkeypatch.setattr(lmu, "_fetch_standings",
                        lambda timeout: (_ for _ in ()).throw(OSError("no game")))


# -- the fields the engineer reads ----------------------------------------


def test_the_lap_fields_come_through():
    plugin, _block = build([{
        "driver": "G.Taylor", "lap_dist": 1234.5, "laps": 7,
        "last_lap": 95.25, "best_lap": 94.5, "is_player": True,
        "velocity": (0.0, 0.0, -60.0),
    }])
    car = plugin._cars()[0]

    assert car.driver == "G.Taylor"
    assert car.lap_dist == pytest.approx(1234.5)
    assert car.laps == 7
    assert car.last_lap == pytest.approx(95.25)
    assert car.best_lap == pytest.approx(94.5)
    assert car.is_player is True


def test_speed_is_the_length_of_the_velocity_vector():
    """The block publishes velocity in the car's own axes, not a scalar. Taking
    the longitudinal component alone reads low through a slide — exactly where
    somebody is losing the time a coaching routine exists to find."""
    plugin, _block = build([{
        "driver": "G.Taylor", "velocity": (3.0, 0.0, -4.0),
    }])
    assert plugin._cars()[0].speed == pytest.approx(5.0)


def test_no_lap_time_reads_as_zero_not_as_a_negative():
    """The block says "no time" with a negative. Letting that through would
    make every car's best lap beat every real one."""
    plugin, _block = build([{
        "driver": "G.Taylor", "last_lap": -1.0, "best_lap": -1.0,
    }])
    car = plugin._cars()[0]

    assert car.last_lap == 0.0
    assert car.best_lap == 0.0


def test_the_sector_fields_come_through():
    plugin, _block = build([{
        "driver": "G.Taylor", "sector": 2,
        "cur1": 30.5, "cur2": 65.25, "last1": 30.1, "last2": 64.9,
    }])
    car = plugin._cars()[0]

    assert car.sector == 2
    assert car.cur_sector1 == pytest.approx(30.5)
    assert car.cur_sector2 == pytest.approx(65.25)
    assert car.last_sector1 == pytest.approx(30.1)
    assert car.last_sector2 == pytest.approx(64.9)


def test_the_sim_numbers_sector_three_as_zero():
    """Its own convention, and not the obvious one. Passed through as reported
    so the one module that has to know it is the one that documents it."""
    plugin, _block = build([{"driver": "G.Taylor", "sector": 0}])
    assert plugin._cars()[0].sector == 0


def test_the_pit_flag_comes_through():
    plugin, _block = build([{"driver": "G.Taylor", "in_pits": True}])
    assert plugin._cars()[0].in_pits is True


def test_positions_come_through_for_every_car():
    """Not just the player's — which is what lets the spotter and proximity
    voice be decided locally, without publishing anything."""
    plugin, _block = build([
        {"driver": "G.Taylor", "position": (10.0, 1.0, 20.0), "is_player": True},
        {"driver": "N.Tandy", "position": (13.0, 1.0, 21.0)},
    ])
    cars = {car.driver: car.position for car in plugin._cars()}

    assert cars["G.Taylor"] == pytest.approx((10.0, 1.0, 20.0))
    assert cars["N.Tandy"] == pytest.approx((13.0, 1.0, 21.0))


# -- the session ----------------------------------------------------------


def test_the_session_carries_the_track_length_and_clock():
    """The engineer needs both: the length to tell a full recorded lap from a
    partial one, and the clock so every sample in a session came off the same
    one."""
    plugin, _block = build([{"driver": "G.Taylor"}], elapsed=456.75)
    session = plugin.session()

    assert session.track == "Sebring"
    assert session.track_length == pytest.approx(TRACK_LENGTH)
    assert session.elapsed == pytest.approx(456.75)


def test_a_single_player_session_still_has_data():
    """No server means no room to join, and the engineer must not care. This is
    the distinction `has_data` exists for, and offline practice is the most
    likely place to want a coaching routine."""
    plugin, _block = build([{"driver": "G.Taylor", "is_player": True}],
                           address=0, port=0)
    session = plugin.session()

    assert not session          # nobody to be in a room with
    assert session.has_data     # but a car on a track
    assert session.player() is not None


def test_a_multiplayer_session_gets_a_room_key():
    plugin, _block = build([{"driver": "G.Taylor"}],
                           address=3232235777, port=54297)
    assert plugin.session().key


# -- what the rest of the engineer makes of it ----------------------------


def test_the_sector_book_reads_a_real_block():
    """End to end from the struct: the plugin's fields into sector times.

    A field name that existed but meant something else would produce numbers
    here rather than an error, so the times are asserted rather than merely
    the absence of a crash.
    """
    from pitradio.engineer import sectors

    book = sectors.SectorBook()
    frames = [
        {"sector": 1, "lap_dist": 100.0},
        {"sector": 2, "lap_dist": 2100.0, "cur1": 30.0},
        {"sector": 0, "lap_dist": 4100.0, "cur1": 30.0, "cur2": 65.0},
        {"sector": 1, "lap_dist": 10.0, "laps": 1, "last_lap": 95.0,
         "last1": 30.0, "last2": 65.0},
    ]
    times = {}
    for frame in frames:
        plugin, _block = build([{"driver": "G.Taylor", **frame}])
        finished = book.observe(plugin._cars()[0])
        if finished is not None:
            times[finished.sector] = finished.seconds

    assert times[1] == pytest.approx(30.0)
    assert times[2] == pytest.approx(35.0)
    assert times[3] == pytest.approx(30.0)


def test_the_boundaries_are_learned_from_where_cars_cross_them():
    """The sim does not publish where its sectors begin, so they are observed —
    the same way corners are found in the data rather than in a track map."""
    from pitradio.engineer import sectors

    book = sectors.SectorBook()
    for frame in ({"sector": 1, "lap_dist": 5.0},
                  {"sector": 2, "lap_dist": 2000.0, "cur1": 30.0},
                  {"sector": 0, "lap_dist": 4000.0, "cur1": 30.0, "cur2": 65.0}):
        plugin, _block = build([{"driver": "G.Taylor", **frame}])
        book.observe(plugin._cars()[0])

    assert book.boundaries[2] == pytest.approx(2000.0)
    assert book.boundaries[3] == pytest.approx(4000.0)


def test_a_name_with_an_accent_survives_the_block():
    """Fixed-width bytes decoded with `replace`: a mangled character must cost
    that character, not the driver list."""
    plugin, _block = build([{"driver": "Sébastien Loeb"}])
    assert plugin._cars()[0].driver == "Sébastien Loeb"


def test_a_car_with_no_name_is_skipped():
    """An empty slot in the array, which is normal — the block is fixed-size."""
    plugin, _block = build([{"driver": "G.Taylor"}, {"driver": ""}])
    assert [car.driver for car in plugin._cars()] == ["G.Taylor"]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="a real game may be publishing on this machine")
def test_a_plugin_with_no_block_reports_no_cars():
    """The ordinary case: the sim is not running."""
    assert lmu.LeMansUltimatePlugin()._cars() == []
