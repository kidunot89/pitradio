import pytest

import keys


def test_plain_key():
    assert keys.parse_combo("enter") == ([], 0x0D)
    assert keys.parse_combo("f13") == ([], 0x7C)
    assert keys.parse_combo("escape") == ([], 0x1B)


def test_names_are_case_and_space_insensitive():
    assert keys.parse_combo("  ENTER ") == keys.parse_combo("enter")


def test_aliases_agree():
    assert keys.parse_combo("esc") == keys.parse_combo("escape")
    assert keys.parse_combo("return") == keys.parse_combo("enter")


def test_modifiers():
    mods, vk = keys.parse_combo("ctrl+enter")
    assert mods == [0xA2]
    assert vk == 0x0D

    mods, vk = keys.parse_combo("ctrl+shift+t")
    assert mods == [0xA2, 0xA0]
    assert vk == ord("T")


def test_unknown_key_and_modifier_are_rejected():
    with pytest.raises(keys.KeyNameError, match="unknown key"):
        keys.parse_combo("wat")
    with pytest.raises(keys.KeyNameError, match="unknown modifier"):
        keys.parse_combo("hyper+enter")
    with pytest.raises(keys.KeyNameError):
        keys.parse_combo("")


def test_trigger_key_rejects_modifiers():
    """The low-level hook sees one key at a time, so a chord can never match."""
    assert keys.parse_key("f13") == 0x7C
    with pytest.raises(keys.KeyNameError, match="cannot have modifiers"):
        keys.parse_key("ctrl+f13")


@pytest.mark.parametrize(
    "name",
    ["left", "up", "right", "down", "home", "end", "pageup", "pagedown",
     "insert", "delete", "rctrl", "ralt"],
)
def test_extended_keys_are_flagged(name):
    """Missing the extended flag turns these into their numpad twins, silently."""
    assert keys.VK[name] in keys.EXTENDED


@pytest.mark.parametrize("name", ["enter", "escape", "a", "f13", "space"])
def test_ordinary_keys_are_not_extended(name):
    assert keys.VK[name] not in keys.EXTENDED


def test_function_keys_cover_f1_to_f24():
    assert keys.VK["f1"] == 0x70
    assert keys.VK["f13"] == 0x7C
    assert keys.VK["f24"] == 0x87


def test_name_for_round_trips():
    assert keys.name_for(0x0D) in ("enter", "return")
    assert keys.name_for(0x7C) == "f13"
    assert keys.name_for(0x9999).startswith("vk_")
