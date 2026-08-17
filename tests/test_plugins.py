"""The plugin registry and its contract.

Plugins are compiled into the build and registered statically — Nuitka cannot
follow an import it never sees, so runtime discovery would ship a binary with
no plugins and no indication why.

The rule that matters most: a plugin fault must cost session data, never the
message. These tests mostly check that misbehaving plugins are contained.
"""

from dataclasses import replace

import pytest

from pitradio import config, plugins, voice
from pitradio.plugins.base import SessionInfo, SessionPlugin

# rF2/LMU's mControl values, named where they are used.
CONTROL_LOCAL_PLAYER = 0
CONTROL_LOCAL_AI = 1


class Working(SessionPlugin):
    id = "fake"
    name = "Fake Sim"
    executables = ("fake.exe",)

    def drivers(self):
        return ["Geoff Taylor", "Nick Tandy"]

    def is_connected(self):
        return True

    def status(self):
        return "connected"


class Exploding(SessionPlugin):
    id = "boom"
    name = "Exploding Sim"
    executables = ("boom.exe",)

    def start(self):
        raise RuntimeError("start blew up")

    def stop(self):
        raise RuntimeError("stop blew up")

    def drivers(self):
        raise RuntimeError("drivers blew up")

    def status(self):
        raise RuntimeError("status blew up")


# -- registration --------------------------------------------------------


def test_lmu_ships_by_default():
    registry = plugins.PluginRegistry()
    assert registry.by_id("lmu") is not None


def test_plugin_ids_are_unique():
    """Profiles store the id, so a duplicate would make resolution ambiguous."""
    ids = [p.id for p in plugins.PluginRegistry().plugins]
    assert len(ids) == len(set(ids))
    assert all(ids), "every plugin needs a non-empty id"


def test_registry_is_static_not_discovered():
    """Runtime discovery would ship a frozen build with no plugins at all."""
    assert isinstance(plugins.BUILTIN, tuple)
    assert plugins.LeMansUltimatePlugin in plugins.BUILTIN


# -- lookup --------------------------------------------------------------


def test_lookup_is_by_id_so_one_plugin_can_serve_several_games():
    registry = plugins.PluginRegistry((Working,))
    assert registry.drivers_for("fake") == ["Geoff Taylor", "Nick Tandy"]


def test_unset_plugin_yields_nothing():
    registry = plugins.PluginRegistry((Working,))
    assert registry.drivers_for("") == []
    assert registry.drivers_for(None) == []


def test_unknown_plugin_id_yields_nothing():
    """A config naming a plugin that no longer ships must not raise."""
    registry = plugins.PluginRegistry((Working,))
    assert registry.drivers_for("removed-plugin") == []


def test_executable_matching_only_supplies_a_default():
    registry = plugins.PluginRegistry((Working,))
    assert registry.default_id_for("fake.exe") == "fake"
    assert registry.default_id_for("something-else.exe") == ""


def test_choices_offer_automatic_first():
    """Unset means "work it out", not "none" — see resolve()."""
    choices = plugins.PluginRegistry((Working,)).choices()
    assert choices[0] == ("", "(automatic)")
    assert ("fake", "Fake Sim") in choices


# -- containment ---------------------------------------------------------


def test_a_plugin_that_raises_on_drivers_costs_only_its_data():
    registry = plugins.PluginRegistry((Exploding,))
    assert registry.drivers_for("boom") == []


def test_a_plugin_that_raises_on_start_does_not_stop_the_app():
    registry = plugins.PluginRegistry((Exploding, Working))
    registry.start_all()
    assert registry.drivers_for("fake") == ["Geoff Taylor", "Nick Tandy"]


def test_a_plugin_that_raises_on_stop_does_not_block_shutdown():
    plugins.PluginRegistry((Exploding,)).stop_all()


def test_a_plugin_that_raises_on_status_still_lists():
    rows = plugins.PluginRegistry((Exploding,)).describe()
    assert len(rows) == 1
    assert "error" in rows[0][1]


def test_a_plugin_that_cannot_be_constructed_is_skipped():
    class Broken(SessionPlugin):
        id = "broken"

        def __init__(self):
            raise RuntimeError("no")

    registry = plugins.PluginRegistry((Broken, Working))
    assert [p.id for p in registry.plugins] == ["fake"]


# -- profile integration -------------------------------------------------


def test_profiles_carry_the_plugin_choice():
    profile = config.Profile()
    assert profile.plugin == ""

    cfg = config.Config.from_dict(
        {"profiles": {"game.exe": {"plugin": "lmu"}}})
    assert cfg.profile_for("game.exe")[0].plugin == "lmu"


def test_the_same_plugin_can_be_set_on_two_games():
    """The whole reason the choice lives on the profile."""
    cfg = config.Config.from_dict({
        "profiles": {"one.exe": {"plugin": "lmu"}, "two.exe": {"plugin": "lmu"}}
    })
    assert cfg.profile_for("one.exe")[0].plugin == "lmu"
    assert cfg.profile_for("two.exe")[0].plugin == "lmu"


def test_the_shipped_lmu_profile_has_the_plugin_assigned():
    import json
    from pathlib import Path

    raw = json.loads(
        (Path(__file__).parent.parent / "config.default.json").read_text(encoding="utf-8"))
    cfg = config.Config.from_dict(raw)
    assert cfg.profile_for("le mans ultimate.exe")[0].plugin == "lmu"


def test_plugin_choice_survives_a_round_trip(tmp_path):
    cfg = config.Config()
    cfg.profiles["game.exe"] = config.Profile(plugin="lmu")

    path = tmp_path / "config.json"
    config.save(path, cfg)
    assert config.load(path).profiles["game.exe"].plugin == "lmu"


# -- the LMU plugin's read path ------------------------------------------


@pytest.fixture
def lmu_with_data(monkeypatch):
    """The real plugin against a zeroed struct, so field names are exercised.

    A wrong field name would otherwise be swallowed by the plugin's own error
    handling and show up as "no drivers, ever" on someone's machine.

    The game's HTTP API is stubbed out as unreachable. Left alone it would be
    *answered* on the machine LMU is installed on, and every focus assertion
    below would quietly depend on what the person at that desk was watching.
    """
    lmu_data = pytest.importorskip(
        "pylmusharedmemory.lmu_data", reason="vendored reader unavailable")

    def unreachable(_timeout):
        raise OSError("no game here")

    monkeypatch.setattr(plugins.lmu, "_fetch_standings", unreachable)

    plugin = plugins.LeMansUltimatePlugin()
    plugin._data = lmu_data.LMUObjectOut()
    return plugin


@pytest.fixture
def watching(monkeypatch):
    """Point the game's API at a chosen slot."""
    def focus_on(slot):
        monkeypatch.setattr(
            plugins.lmu, "_fetch_standings",
            lambda _timeout: [
                {"driverName": "Someone", "slotID": slot, "hasFocus": True}])

    return focus_on


def test_lmu_reads_driver_names(lmu_with_data):
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 2
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Geoff Taylor"
    lmu_with_data._data.scoring.vehScoringInfo[1].mDriverName = b"Nick Tandy"

    assert lmu_with_data.drivers() == ["Geoff Taylor", "Nick Tandy"]


def test_lmu_skips_blank_names(lmu_with_data):
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 3
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Geoff Taylor"
    lmu_with_data._data.scoring.vehScoringInfo[1].mDriverName = b""

    assert lmu_with_data.drivers() == ["Geoff Taylor"]


def test_lmu_clamps_a_nonsense_vehicle_count(lmu_with_data):
    """A stale block can hold garbage; reading it must not walk off the array."""
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 999_999
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Geoff Taylor"

    assert lmu_with_data.drivers() == ["Geoff Taylor"]


def test_lmu_handles_a_negative_count(lmu_with_data):
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = -5
    assert lmu_with_data.drivers() == []


@pytest.fixture
def lmu_absent(monkeypatch):
    """The sim not running, whether or not it is.

    Off Windows this is the only outcome there is, which is why it went
    unnoticed: on the machine LMU is *installed* on — the one place the plugin
    can be exercised for real — an open mapping made these fail, and a suite
    that only passes when the sim is closed is no use to whoever is racing.
    """
    from pitradio.plugins import lmu

    monkeypatch.setattr(lmu, "_open_existing_mapping", lambda name, size: None)


def test_lmu_reports_not_connected_without_the_sim(lmu_absent):
    plugin = plugins.LeMansUltimatePlugin()
    assert plugin.drivers() == []
    assert "not connected" in plugin.status()


# -- opening vs creating -------------------------------------------------


def test_the_lmu_plugin_never_creates_the_mapping():
    """It must open LMU's block, never make one.

    mmap.mmap(fileno=0, tagname=...) calls CreateFileMapping on Windows, which
    *creates* the block when absent. With LMU closed that fabricated a
    page-file-backed block named LMU_Data full of zeros: the plugin reported
    itself connected to a session that did not exist, and left a phantom
    mapping under the game's own name. OpenFileMappingW only ever opens.
    """
    import ast
    from pathlib import Path

    # Every plugin, not just LMU: the rule is silent when broken, so a second
    # sim getting it wrong would look exactly like a session that is running.
    folder = Path(__file__).parent.parent / "src" / "pitradio" / "plugins"
    offenders = []
    for path in sorted(folder.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        # The AST, not the text: the docstrings explaining this trap mention
        # `tagname`, and banning the word would flag the explanation itself.
        creating = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and any(kw.arg == "tagname" for kw in node.keywords)
        ]
        if creating:
            offenders.append(path.name)

    assert not offenders, (
        f"{offenders} pass tagname=, which creates the mapping when it is "
        f"absent and fabricates a block under the game's own name"
    )

    # And one place actually opens one, so the rule has somewhere to live.
    shared = (folder / "shared_memory.py").read_text(encoding="utf-8")
    assert "OpenFileMappingW" in shared, "should open an existing mapping only"


def test_status_says_not_connected_when_lmu_is_absent(lmu_absent):
    """The regression this replaced: it claimed to be connected to nothing."""
    plugin = plugins.LeMansUltimatePlugin()
    assert plugin.drivers() == []
    assert "not connected" in plugin.status()


# -- vocabulary ----------------------------------------------------------


class Announcer(SessionPlugin):
    """A plugin whose useful terms are not people."""

    id = "announcer"
    name = "Some Other Sim"

    def drivers(self):
        return ["Geoff Taylor"]

    def vocabulary(self):
        return ["Porsche 963", "Hypercar", "Eau Rouge"]


def test_vocabulary_defaults_to_the_driver_list():
    """The common case, so a plugin only has to implement drivers()."""
    registry = plugins.PluginRegistry((Working,))
    assert registry.vocabulary_for("fake") == ["Geoff Taylor", "Nick Tandy"]


def test_vocabulary_can_differ_from_drivers():
    """Another sim's useful terms might be cars or commentators, not names."""
    registry = plugins.PluginRegistry((Announcer,))
    assert registry.vocabulary_for("announcer") == [
        "Porsche 963", "Hypercar", "Eau Rouge"]
    assert registry.drivers_for("announcer") == ["Geoff Taylor"]


def test_a_plugin_that_raises_on_vocabulary_costs_only_its_terms():
    registry = plugins.PluginRegistry((Exploding,))
    assert registry.vocabulary_for("boom") == []


def test_vocabularies_lists_every_plugin_for_the_gui():
    """All of them, not just the active one: when a term is missing the
    question is usually which plugin should have supplied it."""
    rows = plugins.PluginRegistry((Working, Announcer)).vocabularies()
    names = [name for name, _terms, _status in rows]
    assert names == ["Fake Sim", "Some Other Sim"]


def test_vocabularies_survives_a_broken_plugin():
    rows = plugins.PluginRegistry((Exploding,)).vocabularies()
    assert rows[0][1] == []
    assert "error" in rows[0][2]


# -- plugin settings -----------------------------------------------------


class Configurable(SessionPlugin):
    id = "configurable"
    name = "Configurable Sim"
    settings = (
        plugins.PluginSetting(key="positions", label="Positions", default=True),
        plugins.PluginSetting(key="depth", label="Depth", kind="int", default=5),
    )


def test_defaults_come_from_the_plugin():
    """So a setting added later needs no rewrite of existing profiles."""
    assert Configurable().defaults() == {"positions": True, "depth": 5}


def test_stored_values_override_defaults():
    registry = plugins.PluginRegistry((Configurable,))
    merged = registry.settings_for("configurable", {"positions": False})
    assert merged == {"positions": False, "depth": 5}


def test_settings_for_an_unknown_plugin_is_empty():
    assert plugins.PluginRegistry((Configurable,)).settings_for("nope") == {}


def test_a_plugin_without_settings_has_none():
    assert plugins.PluginRegistry((Working,)).settings_for("fake") == {}


def test_profiles_store_plugin_settings_separately():
    """One plugin can serve two games and be configured differently for each."""
    cfg = config.Config.from_dict({"profiles": {
        "one.exe": {"plugin": "lmu", "plugin_settings": {"positions": True}},
        "two.exe": {"plugin": "lmu", "plugin_settings": {"positions": False}},
    }})
    assert cfg.profile_for("one.exe")[0].plugin_settings == {"positions": True}
    assert cfg.profile_for("two.exe")[0].plugin_settings == {"positions": False}


def test_plugin_settings_survive_a_round_trip(tmp_path):
    cfg = config.Config()
    cfg.profiles["game.exe"] = config.Profile(
        plugin="lmu", plugin_settings={"positions": False})

    path = tmp_path / "config.json"
    config.save(path, cfg)
    assert config.load(path).profiles["game.exe"].plugin_settings == {"positions": False}


def test_lmu_reads_standings(lmu_with_data):
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 2
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Geoff Taylor"
    lmu_with_data._data.scoring.vehScoringInfo[0].mPlace = 2
    lmu_with_data._data.scoring.vehScoringInfo[1].mDriverName = b"Nick Tandy"
    lmu_with_data._data.scoring.vehScoringInfo[1].mPlace = 1

    assert lmu_with_data.positions() == {1: "Nick Tandy", 2: "Geoff Taylor"}


def test_lmu_skips_unclassified_entries(lmu_with_data):
    """Place 0 means unclassified, not "leader"."""
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 1
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Geoff Taylor"
    lmu_with_data._data.scoring.vehScoringInfo[0].mPlace = 0

    assert lmu_with_data.positions() == {}


# -- multi-class standings -----------------------------------------------


def _grid(plugin, cars):
    """(name, overall place, class) for each car, into the scoring block."""
    plugin._data.scoring.scoringInfo.mNumVehicles = len(cars)
    for index, (name, place, vehicle_class) in enumerate(cars):
        entry = plugin._data.scoring.vehScoringInfo[index]
        entry.mDriverName = name
        entry.mPlace = place
        entry.mVehicleClass = vehicle_class
    return plugin


def test_lmu_derives_places_within_each_class(lmu_with_data):
    """The block carries an overall place and a class name and nothing that
    combines them, so class order is its members sorted on overall place."""
    standings = _grid(lmu_with_data, [
        (b"Max Verstappen", 1, b"Hypercar"),
        (b"Nyck de Vries", 2, b"LMP2"),
        (b"Geoff Taylor", 3, b"Hypercar"),
        (b"Nick Tandy", 4, b"LMGT3"),
        (b"Kamui Kobayashi", 5, b"LMGT3"),
    ]).standings()

    assert standings.by_class == {
        "Hypercar": {1: "Max Verstappen", 2: "Geoff Taylor"},
        "LMP2": {1: "Nyck de Vries"},
        "LMGT3": {1: "Nick Tandy", 2: "Kamui Kobayashi"},
    }


def test_lmu_still_reports_the_overall_order(lmu_with_data):
    """A bare "P3" means overall, which is the column the timing screen shows."""
    standings = _grid(lmu_with_data, [
        (b"Max Verstappen", 1, b"Hypercar"),
        (b"Nyck de Vries", 2, b"LMP2"),
        (b"Geoff Taylor", 3, b"Hypercar"),
    ]).standings()

    assert standings.overall == {
        1: "Max Verstappen", 2: "Nyck de Vries", 3: "Geoff Taylor"}


def test_class_order_does_not_depend_on_the_block_order(lmu_with_data):
    """Cars are listed by slot, not by position."""
    standings = _grid(lmu_with_data, [
        (b"Kamui Kobayashi", 5, b"LMGT3"),
        (b"Nick Tandy", 4, b"LMGT3"),
    ]).standings()

    assert standings.by_class["LMGT3"] == {1: "Nick Tandy", 2: "Kamui Kobayashi"}


def test_an_unclassified_car_is_in_no_class_either(lmu_with_data):
    standings = _grid(lmu_with_data, [
        (b"Nick Tandy", 1, b"LMGT3"),
        (b"Geoff Taylor", 0, b"LMGT3"),
    ]).standings()

    assert standings.by_class["LMGT3"] == {1: "Nick Tandy"}
    assert standings.overall == {1: "Nick Tandy"}


def test_a_car_with_no_class_is_still_in_the_overall_order(lmu_with_data):
    """A blank class must cost the class order, never the driver."""
    standings = _grid(lmu_with_data, [(b"Geoff Taylor", 1, b"")]).standings()

    assert standings.overall == {1: "Geoff Taylor"}
    assert standings.by_class == {}


def test_lmu_lists_the_classes_on_the_grid(lmu_with_data):
    assert _grid(lmu_with_data, [
        (b"Max Verstappen", 1, b"Hypercar"),
        (b"Geoff Taylor", 2, b"Hypercar"),
        (b"Nick Tandy", 3, b"LMGT3"),
    ]).classes() == ["Hypercar", "LMGT3"]


def test_lmu_vocabulary_leads_with_the_class_names(lmu_with_data):
    """The hint is capped, and a 60-car entry list would push the words that
    make "GT3 P3" work off the end of it."""
    assert _grid(lmu_with_data, [
        (b"Max Verstappen", 1, b"Hypercar"),
        (b"Nick Tandy", 2, b"LMGT3"),
    ]).vocabulary() == ["Hypercar", "LMGT3", "Max Verstappen", "Nick Tandy"]


def test_standings_are_empty_without_the_sim(lmu_absent):
    assert not plugins.LeMansUltimatePlugin().standings()


class OverallOnly(SessionPlugin):
    """A plugin written against the contract before classes existed."""

    id = "overall"
    name = "Single Class Sim"

    def positions(self):
        return {1: "Geoff Taylor"}


def test_a_positions_only_plugin_is_not_silently_dropped():
    """Its override would otherwise sit there looking correct while the worker
    read an empty `standings()` instead — a bug with no error message."""
    registry = plugins.PluginRegistry((OverallOnly,))
    standings = registry.standings_for("overall")

    assert standings.overall == {1: "Geoff Taylor"}
    assert standings.by_class == {}


# -- the session, for voice ----------------------------------------------


def test_lmu_reports_a_room_from_the_game_server(lmu_with_data):
    """Two clients on the same server agree on the key without talking."""
    info = lmu_with_data._data.scoring.scoringInfo
    info.mServerPublicIP = 3156777263
    info.mServerPort = 30852
    info.mTrackName = b"Daytona International Speedway Road Course"

    session = lmu_with_data.session()
    assert session.key == voice.session_key(3156777263, 30852)
    assert session.track == "Daytona International Speedway Road Course"
    assert session


def test_lmu_reports_no_room_offline(lmu_with_data):
    """Single player has no server, so there is nobody to be in a room with."""
    assert not lmu_with_data.session()


def test_lmu_reports_where_every_car_is(lmu_with_data):
    """Proximity is decided locally, which only works because the block carries
    every car's position and not just the player's."""
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 2
    first = lmu_with_data._data.scoring.vehScoringInfo[0]
    first.mDriverName = b"Nick Tandy"
    first.mPos.x, first.mPos.y, first.mPos.z = 10.0, 2.0, -30.0
    second = lmu_with_data._data.scoring.vehScoringInfo[1]
    second.mDriverName = b"Geoff Taylor"
    second.mIsPlayer = True

    cars = lmu_with_data.session().cars
    assert [car.driver for car in cars] == ["Nick Tandy", "Geoff Taylor"]
    assert cars[0].position == (10.0, 2.0, -30.0)
    assert lmu_with_data.session().player().driver == "Geoff Taylor"


def test_a_session_with_no_player_car_has_no_player(lmu_with_data):
    """Spectating, or the block not caught up yet. Must not raise."""
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 1
    lmu_with_data._data.scoring.vehScoringInfo[0].mDriverName = b"Nick Tandy"

    assert lmu_with_data.session().player() is None


def test_an_unclassified_car_still_has_a_position(lmu_with_data):
    """It is out of the standings, not off the track — and proximity does not
    care what place somebody is in."""
    lmu_with_data._data.scoring.scoringInfo.mNumVehicles = 1
    entry = lmu_with_data._data.scoring.vehScoringInfo[0]
    entry.mDriverName = b"Nick Tandy"
    entry.mPlace = 0
    entry.mPos.x = 42.0

    cars = lmu_with_data.session().cars
    assert len(cars) == 1
    assert cars[0].place == 0
    assert cars[0].position[0] == 42.0


def _own_car(plugin, *, control: int, others: int = 0):
    data = plugin._data
    data.scoring.scoringInfo.mNumVehicles = 1 + others
    mine = data.scoring.vehScoringInfo[0]
    mine.mDriverName = b"Geoff Taylor"
    mine.mID = 41
    mine.mIsPlayer = True
    mine.mControl = control
    mine.mPos.x = 0.0
    for index in range(others):
        other = data.scoring.vehScoringInfo[1 + index]
        other.mDriverName = f"Driver {index}".encode()
        other.mID = 100 + index
        other.mControl = 2
        other.mPos.x = 5000.0
    return plugin


def test_proximity_is_measured_from_your_own_car_while_driving(lmu_with_data):
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_PLAYER).session()

    assert session.driving() is True
    assert session.listener().driver == "Geoff Taylor"


def test_handing_over_to_the_ai_counts_as_spectating(lmu_with_data):
    """mControl is 0 for the local player and 1 once the AI has the car. It is
    the only signal the block gives that somebody has stopped driving."""
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_AI).session()

    assert session.driving() is False


def test_spectating_with_no_answer_from_the_game_cannot_place_the_listener(lmu_with_data):
    """The API unreachable, the game mid-load, the request timing out — all
    normal. None means audible: keeping the parked car as the reference would
    filter the session by somewhere nobody is looking, and the spectator would
    have no way to tell that from the feature being broken."""
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_AI, others=1).session()

    assert session.listener() is None
    assert voice.audible(
        voice.Speaker("Driver 0", (5000.0, 0.0, 0.0)),
        None, proximity_only=True, metres=200)


def test_the_car_the_camera_is_on_is_detected_not_asked_for(lmu_with_data, watching):
    """Somebody racing cannot reach a dropdown, and somebody spectating should
    not have to. LMU's own HTTP API says which car has focus."""
    watching(100)
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_AI, others=1).session()

    assert session.focus_slot == 100
    assert session.listener().driver == "Driver 0"
    assert session.listener().position[0] == 5000.0


def test_the_focused_car_is_where_the_listener_is(lmu_with_data):
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_AI, others=1).session()
    watching = replace(session, focus_slot=100)

    assert watching.listener().driver == "Driver 0"
    assert watching.listener().position[0] == 5000.0


# -- reading the focus -----------------------------------------------------


def test_focus_reads_the_slot_with_focus():
    assert plugins.lmu.focus_slot([
        {"driverName": "Geoff Taylor", "slotID": 48, "hasFocus": False, "player": True},
        {"driverName": "Eran Kaufman", "slotID": 46, "hasFocus": True},
    ]) == 46


def test_focus_is_not_the_player():
    """The distinction the whole thing rests on: while spectating, `player` is
    the parked car and `hasFocus` is the one on screen."""
    assert plugins.lmu.focus_slot([
        {"slotID": 48, "player": True, "hasFocus": False},
        {"slotID": 46, "player": False, "hasFocus": True},
    ]) == 46


def test_focus_accepts_either_flag():
    """`focus` sits beside `hasFocus` and has always agreed; neither is worth
    preferring on a guess."""
    assert plugins.lmu.focus_slot([{"slotID": 7, "focus": True}]) == 7


def test_slot_zero_is_a_real_slot():
    assert plugins.lmu.focus_slot([{"slotID": 0, "hasFocus": True}]) == 0


@pytest.mark.parametrize("standings", [
    [], None, {}, "nope", [None], ["nope"], [{}],
    [{"hasFocus": True}],                      # focused, but no slot
    [{"slotID": "46", "hasFocus": True}],      # a slot that is not a number
    [{"slotID": 46, "hasFocus": False}],       # nobody focused
])
def test_anything_unexpected_reads_as_no_focus(standings):
    """It is parsed on the trigger cycle. Nothing here may raise."""
    assert plugins.lmu.focus_slot(standings) is None


def test_the_focus_is_cached_rather_than_fetched_every_trigger(lmu_with_data, monkeypatch):
    """The response is ~16KB and the camera does not move between cars every
    frame. This runs while somebody is holding the trigger down."""
    calls = []

    def counted(_timeout):
        calls.append(1)
        return [{"slotID": 100, "hasFocus": True}]

    monkeypatch.setattr(plugins.lmu, "_fetch_standings", counted)
    _own_car(lmu_with_data, control=CONTROL_LOCAL_AI, others=1)

    for _ in range(5):
        assert lmu_with_data.session().focus_slot == 100
    assert len(calls) == 1


def test_a_game_that_is_not_answering_is_not_asked_every_trigger(lmu_with_data, monkeypatch):
    """A failure is cached too, or a closed game costs a timeout per press."""
    calls = []

    def failing(_timeout):
        calls.append(1)
        raise OSError("nothing listening")

    monkeypatch.setattr(plugins.lmu, "_fetch_standings", failing)
    _own_car(lmu_with_data, control=CONTROL_LOCAL_AI)

    for _ in range(5):
        assert lmu_with_data.session().focus_slot is None
    assert len(calls) == 1


def test_a_focus_on_a_car_that_has_left_falls_back_rather_than_misplacing(lmu_with_data):
    """They disconnect mid-session and the slot stops existing."""
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_AI, others=1).session()

    assert replace(session, focus_slot=999).listener() is None


def test_the_focused_car_wins_even_while_driving(lmu_with_data):
    """Watching somebody else from the pits with your own car parked."""
    session = _own_car(lmu_with_data, control=CONTROL_LOCAL_PLAYER, others=1).session()

    assert replace(session, focus_slot=100).listener().driver == "Driver 0"


def test_no_session_at_all_places_nobody():
    assert SessionInfo().listener() is None
    assert SessionInfo().driving() is False


def test_lmu_exposes_the_proximity_settings():
    lmu = plugins.PluginRegistry().by_id("lmu")
    assert [s.key for s in lmu.settings] == [
        "positions", "proximity_only", "proximity_metres", "spotter_swap_sides",
        "spotter_metres", "spotter_width_metres"]


def test_the_spotter_side_swap_is_off_until_it_is_needed():
    """It exists because which side is which could not be verified off a track.

    Off by default is the only defensible starting point: half the users would
    have to flip it whichever way round it shipped, and the ones who never
    touch the spotter should not be asked to care.
    """
    defaults = plugins.PluginRegistry().settings_for("lmu")
    assert defaults["spotter_swap_sides"] is False


def test_proximity_is_off_until_it_is_switched_on():
    """A dictation app that quietly opened the microphone to twenty strangers
    would be a betrayal; but silence nobody asked for is its own bug, so the
    filter starts off and the whole session is audible."""
    defaults = plugins.PluginRegistry().settings_for("lmu")
    assert defaults["proximity_only"] is False


def test_a_plugin_that_raises_on_standings_costs_only_the_standings():
    class Boom(SessionPlugin):
        id = "boom"

        def standings(self):
            raise RuntimeError("standings blew up")

    assert not plugins.PluginRegistry((Boom,)).standings_for("boom")


# -- resolving an unset choice -------------------------------------------


def test_an_unset_plugin_resolves_by_executable():
    """Config files are never overwritten on update.

    A profile written before plugins existed has no choice recorded, and
    treating that as "no plugin" left the whole feature silently doing nothing
    for everyone upgrading — which is exactly what happened.
    """
    registry = plugins.PluginRegistry((Working,))
    assert registry.resolve("", "fake.exe") == "fake"
    assert registry.resolve(None, "fake.exe") == "fake"


def test_an_explicit_choice_wins_over_the_executable():
    registry = plugins.PluginRegistry((Working, Announcer))
    assert registry.resolve("announcer", "fake.exe") == "announcer"


def test_an_unrecognised_executable_resolves_to_nothing():
    registry = plugins.PluginRegistry((Working,))
    assert registry.resolve("", "notepad.exe") == ""


def test_an_old_lmu_profile_still_gets_the_plugin():
    """The concrete upgrade case: a config predating the feature."""
    cfg = config.Config.from_dict({"profiles": {"le mans ultimate.exe": {
        "pre_keys": ["enter"], "post_keys": ["enter"],
    }}})
    profile, _ = cfg.profile_for("le mans ultimate.exe")
    assert profile.plugin == ""

    registry = plugins.PluginRegistry()
    assert registry.resolve(profile.plugin, "le mans ultimate.exe") == "lmu"
