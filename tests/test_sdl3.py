"""The SDL3 binding, against a real SDL3.

Nothing here is mocked. SDL3 ships a virtual joystick API — devices created in
process that behave like real ones to every other SDL call — which is exactly
what this needs: the library is real, the ctypes marshalling is real, and only
the hardware is synthetic.

That matters because the failures this binding can have are all invisible from
Python. A wrong `restype` on `SDL_Init` reads success as failure. SDL3 returns
`bool` where SDL2 returned `int`, and `SDL_GetJoysticks` hands back a malloc'd
array the caller has to free. None of that is checkable by reading the code,
and all of it fails as "no controllers detected" rather than as an error.

Skipped when SDL3 is not installed. `packaging/fetch_sdl3.py` provides it on
Windows; `brew install sdl3` or `apt install libsdl3-0` elsewhere.
"""

from __future__ import annotations

from pitradio.input import sdl3input

# -- the library actually loads ------------------------------------------


def test_sdl3_initialises(sdl):
    """SDL3's SDL_Init returns bool where SDL2's returned 0 on success.

    Reading that backwards makes the backend report itself unavailable and drop
    silently to SDL2 — the exact state SDL3 was added to escape.
    """
    assert sdl.available
    assert sdl.failure is None


def test_enumeration_with_nothing_attached_is_empty_not_broken(sdl):
    """SDL_GetJoysticks returns a valid pointer and a count of zero."""
    assert sdl.list_devices() == []


# -- enumeration ----------------------------------------------------------


def test_a_device_is_found_with_its_name(sdl, pad):
    pad(name="Fanatec CSL Elite", buttons=20)
    devices = sdl.list_devices()

    assert len(devices) == 1
    _instance, name, _count = devices[0]
    assert name == "Fanatec CSL Elite"


def test_several_devices_are_all_found(sdl, pad):
    """A wheel base, a rim and pedals enumerate separately on a real rig."""
    pad(name="Wheel Base", buttons=8)
    pad(name="Button Box", buttons=32)
    pad(name="Pedals", buttons=2)

    names = sorted(name for _id, name, _count in sdl.list_devices())
    assert names == ["Button Box", "Pedals", "Wheel Base"]


def test_a_detached_device_disappears(sdl, pad):
    first = pad(name="Wheel", buttons=8)
    assert len(sdl.list_devices()) == 1

    sdl._lib.SDL_DetachVirtualJoystick(first.instance_id)
    assert sdl.list_devices() == []


def test_hats_are_counted_as_bindable_inputs(sdl, pad):
    """A wheel rim's D-pad is a hat, and it has to be bindable."""
    pad(name="Rim", buttons=10, hats=1)
    _instance, _name, count = sdl.list_devices()[0]

    assert count == 10 + 4


# -- button state ---------------------------------------------------------


def test_a_pressed_button_appears_in_the_mask(sdl, pad):
    device = pad(buttons=20)
    instance_id = sdl.list_devices()[0][0]

    assert sdl.button_mask(instance_id) == 0
    device.press(12)                       # SDL's 0-based button 12
    assert sdl.button_mask(instance_id) == 1 << 12


def test_releasing_clears_it(sdl, pad):
    device = pad(buttons=20)
    instance_id = sdl.list_devices()[0][0]

    device.press(3)
    assert sdl.button_mask(instance_id) & (1 << 3)
    device.press(3, down=False)
    assert not sdl.button_mask(instance_id) & (1 << 3)


def test_several_buttons_at_once(sdl, pad):
    device = pad(buttons=20)
    instance_id = sdl.list_devices()[0][0]

    device.press(0)
    device.press(5)
    device.press(19)
    assert sdl.button_mask(instance_id) == (1 << 0) | (1 << 5) | (1 << 19)


def test_a_button_box_past_thirty_two_buttons(sdl, pad):
    """The old 32-button ceiling would have truncated this."""
    device = pad(name="Button Box", buttons=64)
    instance_id = sdl.list_devices()[0][0]

    device.press(63)
    assert sdl.button_mask(instance_id) == 1 << 63


def test_the_mask_of_a_missing_device_is_none(sdl):
    """Not zero: "unplugged" and "nothing pressed" must be distinguishable."""
    assert sdl.button_mask(999999) is None


# -- hats -----------------------------------------------------------------


def test_hat_directions_sit_above_the_buttons(sdl, pad):
    """Folded in so everything above this layer deals in one flat number."""
    device = pad(name="Rim", buttons=10, hats=1)
    instance_id = sdl.list_devices()[0][0]

    device.hat(0, sdl3input.SDL_HAT_UP)
    assert sdl.button_mask(instance_id) == 1 << 10     # first bit after buttons

    device.hat(0, sdl3input.SDL_HAT_LEFT)
    assert sdl.button_mask(instance_id) == 1 << 13     # up, right, down, left


def test_a_hat_and_a_button_together(sdl, pad):
    device = pad(name="Rim", buttons=10, hats=1)
    instance_id = sdl.list_devices()[0][0]

    device.press(2)
    device.hat(0, sdl3input.SDL_HAT_DOWN)
    assert sdl.button_mask(instance_id) == (1 << 2) | (1 << 12)


# -- identity and labels --------------------------------------------------


def test_a_device_has_a_stable_identity(sdl, pad):
    """The whole binding model depends on this being a real value."""
    pad(name="Wheel", buttons=8)
    instance_id = sdl.list_devices()[0][0]

    guid = sdl.guid(instance_id)
    assert guid
    assert len(guid) == 32
    assert guid.strip("0")          # not the all-zero "no identity" GUID


def test_the_same_device_keeps_its_identity(sdl, pad):
    pad(name="Wheel", buttons=8)
    instance_id = sdl.list_devices()[0][0]

    assert sdl.guid(instance_id) == sdl.guid(instance_id)


def test_buttons_are_labelled_one_based(sdl, pad):
    """1-based to match the numbering printed on a wheel."""
    pad(name="Wheel", buttons=20)
    instance_id = sdl.list_devices()[0][0]

    assert sdl.label(instance_id, 13) == "button 13"


def test_hats_are_labelled_as_pov_directions(sdl, pad):
    """'button 14' would tell nobody which way the D-pad was pushed."""
    pad(name="Rim", buttons=10, hats=1)
    instance_id = sdl.list_devices()[0][0]

    assert sdl.label(instance_id, 11) == "POV up"
    assert sdl.label(instance_id, 12) == "POV right"
    assert sdl.label(instance_id, 14) == "POV left"


def test_the_name_is_reported_for_a_known_device(sdl, pad):
    pad(name="Fanatec CSL Elite", buttons=8)
    instance_id = sdl.list_devices()[0][0]

    assert sdl.name(instance_id) == "Fanatec CSL Elite"


def test_an_unknown_device_has_no_name(sdl):
    assert sdl.name(999999) is None
