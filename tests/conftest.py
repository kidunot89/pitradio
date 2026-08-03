"""Shared fixtures.

The SDL3 ones drive a real SDL3 through its virtual joystick API — devices
created in process that behave like real ones to every other SDL call. The
library is real, the ctypes marshalling is real, and only the hardware is
synthetic, which is as little pretence as these paths allow.

`winapi` is stubbed only where a module imports it at load time and would
otherwise be unimportable off Windows. Nothing belonging to the app is faked.
"""

from __future__ import annotations

import ctypes
import sys
import types
from pathlib import Path

import pytest

import sdl3input

ROOT = Path(__file__).parent.parent


@pytest.fixture
def stub_winapi(monkeypatch):
    """The Win32 boundary, for modules that import it at module scope.

    Only the handful of functions the tests can reach; anything else missing
    would raise rather than quietly returning a wrong answer.
    """
    if sys.platform == "win32":
        return None

    stub = types.ModuleType("winapi")
    stub.joystick_count = lambda: 0
    stub.joystick_name = lambda index: None
    stub.joystick_buttons = lambda index: 0
    stub.joystick_button_mask = lambda index: None
    stub.INJECT_TAG = 0x50545244
    stub.foreground_exe = lambda: None
    stub.is_key_down = lambda vk: False
    monkeypatch.setitem(sys.modules, "winapi", stub)
    return stub


@pytest.fixture(scope="module")
def sdl():
    """A started SDL3 backend.

    Skipped only when SDL3 is genuinely not installed. If the library *is*
    there and `start()` still fails, that is a bug in the binding and this
    fails rather than skipping — otherwise breaking the binding would make the
    whole suite quietly disappear, which is the same silent failure the
    backend itself has and precisely what these tests exist to catch.
    """
    if sdl3input._load() is None:
        pytest.skip("SDL3 is not installed; see this module's docstring")

    backend = sdl3input.Sdl3Joysticks()
    assert backend.start(), (
        f"SDL3 is installed but the binding could not start it: "
        f"{backend.failure}"
    )
    yield backend
    backend.stop()


# -- the virtual joystick API, declared here because only tests use it ----


class SDL_VirtualJoystickDesc(ctypes.Structure):
    """Must match SDL_joystick.h exactly; SDL validates `version` against its
    own sizeof, so a wrong layout is rejected rather than silently misread."""

    _fields_ = [
        ("version", ctypes.c_uint32),
        ("type", ctypes.c_uint16),
        ("padding", ctypes.c_uint16),
        ("vendor_id", ctypes.c_uint16),
        ("product_id", ctypes.c_uint16),
        ("naxes", ctypes.c_uint16),
        ("nbuttons", ctypes.c_uint16),
        ("nballs", ctypes.c_uint16),
        ("nhats", ctypes.c_uint16),
        ("ntouchpads", ctypes.c_uint16),
        ("nsensors", ctypes.c_uint16),
        ("padding2", ctypes.c_uint16 * 2),
        ("button_mask", ctypes.c_uint32),
        ("axis_mask", ctypes.c_uint32),
        ("name", ctypes.c_char_p),
        ("touchpads", ctypes.c_void_p),
        ("sensors", ctypes.c_void_p),
        ("userdata", ctypes.c_void_p),
        ("Update", ctypes.c_void_p),
        ("SetPlayerIndex", ctypes.c_void_p),
        ("Rumble", ctypes.c_void_p),
        ("RumbleTriggers", ctypes.c_void_p),
        ("SetLED", ctypes.c_void_p),
        ("SendEffect", ctypes.c_void_p),
        ("SetSensorsEnabled", ctypes.c_void_p),
        ("Cleanup", ctypes.c_void_p),
    ]


class VirtualPad:
    """A synthetic device, controllable from the test."""

    def __init__(self, lib, instance_id, handle):
        self._lib = lib
        self.instance_id = instance_id
        self._handle = handle

    def press(self, button: int, down: bool = True) -> None:
        """`button` is 0-based, as SDL numbers them."""
        assert self._lib.SDL_SetJoystickVirtualButton(self._handle, button, down)

    def hat(self, index: int, value: int) -> None:
        assert self._lib.SDL_SetJoystickVirtualHat(self._handle, index, value)


@pytest.fixture
def pad(sdl):
    """Attaches a virtual pad and detaches it afterwards."""
    created = []

    def attach(name="Test Wheel", buttons=20, hats=0):
        lib = sdl._lib
        lib.SDL_AttachVirtualJoystick.argtypes = (
            ctypes.POINTER(SDL_VirtualJoystickDesc),)
        lib.SDL_AttachVirtualJoystick.restype = ctypes.c_uint32
        lib.SDL_DetachVirtualJoystick.argtypes = (ctypes.c_uint32,)
        lib.SDL_DetachVirtualJoystick.restype = ctypes.c_bool
        lib.SDL_SetJoystickVirtualButton.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_bool)
        lib.SDL_SetJoystickVirtualButton.restype = ctypes.c_bool
        lib.SDL_SetJoystickVirtualHat.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint8)
        lib.SDL_SetJoystickVirtualHat.restype = ctypes.c_bool

        desc = SDL_VirtualJoystickDesc()
        desc.version = ctypes.sizeof(SDL_VirtualJoystickDesc)
        desc.type = 0
        desc.naxes = 2
        desc.nbuttons = buttons
        desc.nhats = hats
        desc.name = name.encode()

        instance_id = lib.SDL_AttachVirtualJoystick(ctypes.byref(desc))
        assert instance_id, f"SDL_AttachVirtualJoystick failed: {sdl._error(lib)}"

        # Our own handle for driving it; SDL refcounts opens, so this does not
        # interfere with the one the backend keeps.
        handle = lib.SDL_OpenJoystick(instance_id)
        assert handle, f"SDL_OpenJoystick failed: {sdl._error(lib)}"
        created.append(instance_id)
        return VirtualPad(lib, instance_id, handle)

    yield attach

    for instance_id in created:
        sdl._lib.SDL_DetachVirtualJoystick(instance_id)
