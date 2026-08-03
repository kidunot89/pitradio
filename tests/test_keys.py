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


def test_trigger_accepts_modifiers():
    """A low-level hook reports one key at a time, so the hook checks modifier
    state separately via GetAsyncKeyState rather than expecting a chord event."""
    assert keys.parse_trigger("f13") == ([], 0x7C)
    assert keys.parse_trigger("ctrl+f12") == ([keys.VK["ctrl"]], 0x7B)
    # parse_key still answers "which key do I watch for".
    assert keys.parse_key("ctrl+f13") == 0x7C


@pytest.mark.parametrize(
    ("mods", "vk", "expected"),
    [
        ([], "f13", "f13"),
        (["ctrl"], "f12", "ctrl+f12"),
        (["shift", "ctrl"], "a", "ctrl+shift+a"),
        (["alt", "ctrl", "shift"], "enter", "ctrl+alt+shift+enter"),
    ],
)
def test_format_combo_is_order_stable(mods, vk, expected):
    """The same physical press must render identically however it was pressed."""
    codes = [keys.VK[m] for m in mods]
    assert keys.format_combo(codes, keys.VK[vk]) == expected


@pytest.mark.parametrize("spec", ["f13", "ctrl+f12", "shift+ctrl+a", "escape"])
def test_format_combo_round_trips_through_the_parser(spec):
    """Rendering a captured press and re-parsing it must describe the same keys.

    Modifier order is normalised, so compare as sets rather than lists.
    """
    mods, vk = keys.parse_trigger(spec)
    remods, revk = keys.parse_trigger(keys.format_combo(mods, vk))

    assert revk == vk
    assert {keys.generic_modifier(m) for m in remods} == {
        keys.generic_modifier(m) for m in mods
    }


@pytest.mark.parametrize(
    ("side_specific", "generic"),
    [(0xA0, 0x10), (0xA1, 0x10), (0xA2, 0x11), (0xA3, 0x11), (0xA4, 0x12), (0xA5, 0x12)],
)
def test_side_specific_modifiers_map_to_generic(side_specific, generic):
    """Either Ctrl should satisfy a "ctrl" trigger, so ask about the generic code."""
    assert keys.generic_modifier(side_specific) == generic


def test_non_modifiers_pass_through_generic_mapping():
    assert keys.generic_modifier(keys.VK["f13"]) == keys.VK["f13"]


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
