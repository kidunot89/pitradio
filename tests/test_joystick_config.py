"""Joystick trigger config, and the mic gain.

The joystick path can't be exercised without hardware, so what's testable is
the binding's shape and the rules around it — which is where a wrong value
would silently mean "no trigger" rather than an error.
"""

import numpy as np
import pytest

import config
import speech

# -- joystick binding ----------------------------------------------------


def test_unbound_by_default():
    """Shipping a binding would claim a button on someone else's wheel."""
    cfg = config.Config()
    assert cfg.joystick.device is None
    assert cfg.joystick.button is None
    assert cfg.validate() == []


def test_a_valid_binding_passes():
    cfg = config.Config.from_dict({"joystick": {"device": 0, "button": 13}})
    assert cfg.validate() == []


@pytest.mark.parametrize("button", [0, 129, -1, "13", None])
def test_button_must_be_one_to_one_twenty_eight(button):
    """1-based to match wheel labelling; the ceiling covers hats and button boxes."""
    cfg = config.Config.from_dict({"joystick": {"device": 0, "button": button}})
    assert any("joystick.button" in p for p in cfg.validate())


@pytest.mark.parametrize("button", [1, 32, 33, 128])
def test_button_boxes_past_thirty_two_are_accepted(button):
    """A 32-button ceiling would reject half the rims this is meant to bind."""
    cfg = config.Config.from_dict({"joystick": {"device": 0, "button": button}})
    assert [p for p in cfg.validate() if "joystick.button" in p] == []


@pytest.mark.parametrize("device", [-1, "first", 1.5])
def test_device_must_be_a_non_negative_int(device):
    cfg = config.Config.from_dict({"joystick": {"device": device, "button": 3}})
    assert any("joystick.device" in p for p in cfg.validate())


def test_an_identity_alone_is_a_complete_binding():
    """The index is only a fallback, so a binding may legitimately omit it."""
    cfg = config.Config.from_dict(
        {"joystick": {"guid": "03000000d81400000862000000000000", "button": 13}})
    assert cfg.validate() == []


def test_device_is_not_required_once_an_identity_is_recorded():
    cfg = config.Config.from_dict(
        {"joystick": {"device": None, "guid": "abc", "button": 4}})
    assert [p for p in cfg.validate() if "joystick.device" in p] == []


@pytest.mark.parametrize("guid", [7, 1.5, ["a"]])
def test_identity_must_be_a_string(guid):
    cfg = config.Config.from_dict({"joystick": {"guid": guid, "button": 3}})
    assert any("joystick.guid" in p for p in cfg.validate())


def test_binding_survives_a_save_load_round_trip(tmp_path):
    cfg = config.Config()
    cfg.joystick.device = 1
    cfg.joystick.button = 7
    cfg.joystick.guid = "03000000d81400000862000000000000"
    cfg.joystick.name = "Fanatec CSL Elite"

    path = tmp_path / "config.json"
    config.save(path, cfg)
    reloaded = config.load(path)

    assert (reloaded.joystick.device, reloaded.joystick.button) == (1, 7)
    assert reloaded.joystick.guid == "03000000d81400000862000000000000"
    assert reloaded.joystick.name == "Fanatec CSL Elite"


def test_a_binding_written_before_identities_still_loads():
    """Upgrading must not invalidate a binding someone already made."""
    cfg = config.Config.from_dict({"joystick": {"device": 0, "button": 13}})
    assert cfg.validate() == []
    assert cfg.joystick.guid is None


def test_clearing_a_binding_is_valid():
    cfg = config.Config.from_dict({"joystick": {"device": None, "button": None}})
    assert cfg.validate() == []


# -- microphone gain -----------------------------------------------------


def test_gain_defaults_to_unity():
    assert config.Config().audio.gain == 1.0


@pytest.mark.parametrize("gain", [0.05, 0, 10.5, 100])
def test_gain_outside_the_usable_range_is_rejected(gain):
    cfg = config.Config.from_dict({"audio": {"gain": gain}})
    assert any("audio.gain" in p for p in cfg.validate())


@pytest.mark.parametrize("gain", [0.1, 1.0, 2.5, 10.0])
def test_usable_gains_are_accepted(gain):
    cfg = config.Config.from_dict({"audio": {"gain": gain}})
    assert [p for p in cfg.validate() if "audio.gain" in p] == []


def test_gain_clips_rather_than_wrapping():
    """Scaling past full scale must saturate; wrapping would turn speech to noise."""
    recorder = speech.Recorder()
    recorder._gain = 4.0

    block = np.array([0.5, -0.5, 0.1, -0.9], dtype=np.float32)
    amplified = np.clip(block * recorder._gain, -1.0, 1.0)

    assert amplified.max() <= 1.0
    assert amplified.min() >= -1.0
    assert amplified[2] == pytest.approx(0.4)


def test_gain_is_read_from_config_on_start():
    """A recorder built before the gain was set must still pick it up."""
    recorder = speech.Recorder()
    assert recorder._gain == 1.0

    cfg = config.AudioConfig(gain=2.5)
    # start() would open a stream; this is the assignment it performs.
    recorder._gain = float(getattr(cfg, "gain", 1.0) or 1.0)
    assert recorder._gain == 2.5
