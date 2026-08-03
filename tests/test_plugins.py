"""The plugin registry and its contract.

Plugins are compiled into the build and registered statically — Nuitka cannot
follow an import it never sees, so runtime discovery would ship a binary with
no plugins and no indication why.

The rule that matters most: a plugin fault must cost session data, never the
message. These tests mostly check that misbehaving plugins are contained.
"""

import pytest

import config
import plugins
from plugins.base import SessionPlugin


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


def test_choices_offer_none_first():
    choices = plugins.PluginRegistry((Working,)).choices()
    assert choices[0] == ("", "(none)")
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
def lmu_with_data():
    """The real plugin against a zeroed struct, so field names are exercised.

    A wrong field name would otherwise be swallowed by the plugin's own error
    handling and show up as "no drivers, ever" on someone's machine.
    """
    lmu_data = pytest.importorskip(
        "pylmusharedmemory.lmu_data", reason="vendored reader unavailable")

    plugin = plugins.LeMansUltimatePlugin()
    plugin._data = lmu_data.LMUObjectOut()
    return plugin


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


def test_lmu_reports_not_connected_without_the_sim():
    plugin = plugins.LeMansUltimatePlugin()
    assert plugin.drivers() == []
    assert "not connected" in plugin.status()
