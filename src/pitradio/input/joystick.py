"""Wheel and gamepad buttons as a trigger.

Two things here are copied from how sims handle controller bindings, because
both are the difference between a binding that keeps working and one that
appears to break for no reason.

**Bindings are stored against the device, not its position.** SDL device
indices are positional: unplug a pedal set, launch Steam, or plug in a headset
that enumerates as a controller, and every index after it shifts. A binding
recorded as "device 2, button 13" then quietly points at a different device.
Sims store a device identity — SDL calls it a GUID — and resolve it to whatever
index that device currently occupies. So does this. The index is still written
to the config as a fallback for bindings made before identities were recorded.

**Capture waits for a button to be pressed, not to be held.** Wheel rims and
button boxes are covered in toggle switches and rotary encoders that sit in a
permanently-on position, so "the first button that reads as down" captures a
switch nobody touched. Capture snapshots what is already held and waits for a
change, which is what makes it possible to bind a button on a rim that has a
latched switch on it.

Buttons are polled rather than delivered as events. That is also what makes the
trigger global: reading device state asks the driver and does not care which
window has focus, so a button on the wheel fires while the sim is in front.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pitradio.input import sdl3input, sdlinput, winapi

# Re-exported from state so every producer of these events agrees on the
# strings. Actions are momentary: one event on press, none on release.
from pitradio.state import (  # noqa: F401  (re-exported for callers)
    TRIGGER_CLEAR,
    TRIGGER_DOWN,
    TRIGGER_SEND,
    TRIGGER_UP,
)

log = logging.getLogger(__name__)

POLL_SECONDS = 0.012

# Capture settles for this long before it will accept anything, folding
# everything seen during the window into the ignore set. A Steam Controller
# reports its touchpads and grip sensors as buttons that sit active or chatter
# — buttons 20 and 22 on one — and a single-frame snapshot only catches the
# ones that happen to be active at that instant.
CAPTURE_SETTLE_POLLS = 25          # ~300ms

# And a press has to persist this long to count, so a one-frame blip from a
# noisy sensor cannot win the binding. A deliberate press lasts far longer.
CAPTURE_CONFIRM_POLLS = 3          # ~36ms
MAX_DEVICES = 16

# Backends in preference order. Every one of them sees hardware the others
# miss, so they are combined rather than chosen between:
#
#   SDL3    native drivers for devices SDL2 never covered, read over HIDAPI
#           without depending on any other software being present
#   SDL2    the widest coverage of wheels, pedals and button boxes
#   XInput  four fixed slots; catches pads that present as an Xbox controller,
#           including anything a wrapper re-presents as one
#   legacy  the Windows multimedia joystick API, as a floor
#
# A rig routinely spans more than one: a wheel on SDL2 and a pad on XInput is
# ordinary. Picking a single backend would silently drop half the hardware,
# which is what the earlier SDL2-only path did.
_BACKENDS: list = []
_STARTED = False


#: Set from config to force one backend, for when the automatic choice is
#: wrong. "auto" takes the first that starts.
_preferred = "auto"


#: Whether the SDL backends should take a Steam Controller away from Steam.
_take_over_steam = False


def prefer(backend_name: str, take_over_steam: bool = False) -> None:
    """Pin the backend, or "auto". Takes effect on the next start."""
    global _preferred, _take_over_steam
    if backend_name != _preferred or take_over_steam != _take_over_steam:
        _preferred = backend_name or "auto"
        _take_over_steam = take_over_steam
        stop_all()


def _candidates() -> list:
    sdl3 = sdl3input.Sdl3Joysticks()
    sdl2 = sdlinput.SdlJoysticks()
    # Read-only unless explicitly told otherwise: reading a button state does
    # not require owning the device, and taking it from whatever owns it breaks
    # that application.
    sdl3.steam_hidapi = sdl2.steam_hidapi = _take_over_steam
    order = [sdl3, sdl2]
    if sys.platform == "win32":
        # Imported here, not at module scope: ctypes.wintypes raises off
        # Windows, and this module has to stay importable for the tests.
        from pitradio.input import xinput

        order.append(xinput.XInputPads())
    order.append(LegacyPads())

    if _preferred != "auto":
        chosen = [b for b in order if b.version.lower() == _preferred.lower()]
        if chosen:
            return chosen
        log.warning("no backend called %r; choosing automatically", _preferred)
    return order


def _ensure_started() -> list:
    """Start backends in preference order and keep **one**.

    Not all of them. SDL2 and SDL3 enumerate the same hardware through the same
    drivers, so running both means two libraries opening the same HID device in
    one process — every controller appears twice, and one of the two copies
    reads a button state that never changes. A Fanatec wheel showed up under
    both and could not be bound at all.

    XInput and the legacy interface overlap SDL just as much. So the first
    backend that starts is the one used, and the rest are reported by
    `diagnose()` as available but idle. `prefer()` pins a specific one when the
    automatic choice is wrong, which is the only way to find that out.
    """
    global _STARTED
    if _STARTED:
        return _BACKENDS

    _STARTED = True
    for backend in _candidates():
        try:
            if backend.start():
                _BACKENDS.append(backend)
                log.info("joystick backend: %s", backend.version)
                return _BACKENDS
            log.info("%s unavailable: %s", backend.version, backend.failure)
        except Exception:
            # A backend that throws on load must cost only itself.
            log.exception("%s failed to start", backend.version)
    return _BACKENDS


def stop_all() -> None:
    """Release every backend, so a different one can be started."""
    global _STARTED
    for backend in _BACKENDS:
        try:
            backend.stop()
        except Exception:
            log.debug("%s teardown failed", backend.version, exc_info=True)
    _BACKENDS.clear()
    _STARTED = False


class LegacyPads:
    """The Windows multimedia joystick API, wrapped to match the others.

    Cannot see Steam Input devices at all and caps at 32 buttons, but it needs
    no library and works when everything else has failed to load.
    """

    version = "Windows legacy"

    def __init__(self) -> None:
        self._failure: str | None = None

    def start(self) -> bool:
        if sys.platform != "win32":
            self._failure = "not Windows"
            return False
        return True

    @property
    def failure(self) -> str | None:
        return self._failure

    def stop(self) -> None:
        pass

    def list_devices(self) -> list[tuple[int, str, int]]:
        found = []
        for index in range(min(winapi.joystick_count(), MAX_DEVICES)):
            name = winapi.joystick_name(index)
            if name is None:
                continue
            # Named but unreadable means present in the driver list and not
            # actually connected.
            if winapi.joystick_button_mask(index) is None:
                continue
            found.append((index, name, winapi.joystick_buttons(index) or 0))
        return found

    def button_mask(self, index: int) -> int | None:
        return winapi.joystick_button_mask(index)

    def guid(self, index: int) -> str | None:
        """No identity exists here, so the name stands in.

        Weaker than a GUID — two identical wheels are indistinguishable — but
        it still survives the reordering that breaks a bare index.
        """
        name = winapi.joystick_name(index)
        return f"legacy:{name}" if name else None

    def label(self, index: int, button: int) -> str:
        return f"button {button}"

    def name(self, index: int) -> str | None:
        return winapi.joystick_name(index)


def backends() -> list:
    return _ensure_started()


def backend_name() -> str:
    live = [b.version for b in backends()]
    return live[0] if live else "none"


@dataclass(frozen=True)
class Device:
    """One attached controller, as both the config and the GUI need it."""

    index: int          # position in the combined list; the legacy fallback
    name: str
    buttons: int
    guid: str
    api: str            # which backend found it
    native: int         # the id that backend addresses it by
    owner: object = None

    @property
    def key(self) -> str:
        """What a binding is matched on."""
        return self.guid or f"{self.api}:{self.native}"


def devices() -> list[Device]:
    """Every attached controller, across every backend that loaded.

    Deduplicated by identity, earlier backends winning: a pad visible to both
    SDL3 and XInput must appear once, and should be read through the backend
    that knows its real name and button count.
    """
    found: list[Device] = []
    seen: set[str] = set()

    for backend in backends():
        try:
            listed = backend.list_devices()
        except Exception:
            log.exception("%s enumeration failed", backend.version)
            continue

        for native, name, buttons in listed:
            try:
                guid = backend.guid(native) or ""
            except Exception:
                guid = ""
            identity = guid or f"{backend.version}:{native}"
            if identity in seen:
                continue
            # A device with nothing to press cannot be bound, and listing it
            # only invites someone to try.
            #
            # This is not a phantom: the Windows legacy interface reports real
            # hardware it cannot describe under the generic name "Microsoft
            # PC-joystick driver" with 0 usable inputs — a Fanatec wheel turns
            # up exactly that way, as a *third* view of a device SDL3 and SDL2
            # were already both holding open.
            if not buttons:
                continue
            seen.add(identity)
            found.append(Device(
                index=len(found), name=name, buttons=buttons, guid=guid,
                api=backend.version, native=native, owner=backend,
            ))
    return found


def _mask(device: Device) -> int | None:
    if device.owner is None:
        return None
    try:
        return device.owner.button_mask(device.native)
    except Exception:
        log.exception("%s button read failed", device.api)
        return None


def _still_attached(device: Device) -> bool:
    """Whether a cached device is still the one at that position."""
    if device.owner is None:
        return False
    try:
        return any(native == device.native
                   for native, _name, _count in device.owner.list_devices())
    except Exception:
        return False


def _find(guid: str, fallback: int | None) -> Device | None:
    """The device a binding refers to, or None if it is not attached."""
    listed = devices()
    if guid:
        for device in listed:
            if device.guid == guid:
                return device
        return None
    if fallback is None:
        return None
    for device in listed:
        if device.index == fallback:
            return device
    return None


def list_devices() -> list[tuple[int, str, int]]:
    """(device id, name, button count) for every attached joystick."""
    return [(d.index, d.name, d.buttons) for d in devices()]


def diagnose() -> list[str]:
    """What was found, and through which API.

    Which backend saw a device is the first thing worth knowing when one does
    not work: a pad that only XInput can see has no usable identity, and a
    device missing from every backend is a driver or Steam problem rather than
    anything this app can fix.
    """
    live = backends()
    lines = [f"backend in use: {backend_name()}"]
    if _preferred != "auto":
        lines.append(f"  pinned by config to {_preferred!r}")
    for backend in live:
        if backend.failure:
            lines.append(f"  {backend.version}: {backend.failure}")
    idle = [b.version for b in _candidates()
            if not any(b.version == used.version for used in live)]
    if idle:
        # Named so it is obvious another one could be tried, and how.
        lines.append(f"  not in use: {', '.join(idle)} "
                     f"(set joystick.backend in config.json to force one)")

    found = devices()
    for device in found:
        identity = device.guid or "no identity"
        lines.append(
            f"  [{device.api}] device {device.index}: {device.name!r} — "
            f"{device.buttons} inputs [{identity}]"
        )

    if not found:
        lines.append(
            "  no controllers detected by any backend. A device that Steam has "
            "captured, or one in desktop mode, presents as a keyboard and mouse "
            "rather than a controller and cannot be seen here."
        )
    return lines


def held_buttons(device: Device) -> list[int]:
    """Which of a device's buttons are down right now, 1-based.

    For the live readout. The trigger path cannot report this — it only ever
    asks about the one bit it is bound to, so when a binding is wrong it has
    nothing to say beyond "no".
    """
    mask = _mask(device)
    if not mask:
        return []
    return [bit + 1 for bit in range(device.buttons or 128) if mask & (1 << bit)]


def snapshot() -> list[tuple[Device, list[int]]]:
    """Every attached device with the buttons currently held on it."""
    return [(device, held_buttons(device)) for device in devices()]


def describe(index: int, button: int) -> str:
    """Human-readable binding, e.g. 'Fanatec CSL Elite - button 13 (SDL2)'.

    The API is part of the label because it changes what the binding can
    promise: an XInput device has no identity beyond its slot, so a binding
    against one is weaker than the same binding through SDL.
    """
    for device in devices():
        if device.index != index:
            continue
        label = f"button {button}"
        if device.owner is not None:
            try:
                label = device.owner.label(device.native, button)
            except Exception:
                log.debug("%s label failed", device.api, exc_info=True)
        return f"{device.name} - {label} ({device.api})"
    return f"joystick {index} - button {button}"


class JoystickWatcher(threading.Thread):
    """Polls one button and posts down/up onto the shared trigger queue.

    Publishes the same event kinds as the keyboard hook, so the worker cannot
    tell — and does not need to tell — which one fired.
    """

    def __init__(
        self,
        events: queue.Queue[tuple[str, float]],
        is_enabled: Callable[[], bool],
        device: int | None = None,
        button: int | None = None,
        guid: str | None = None,
        name: str | None = None,
    ):
        super().__init__(name="joystick", daemon=True)
        self._events = events
        self._is_enabled = is_enabled
        self._device = device
        self._button = button
        self._guid = guid or ""
        # Remembered so a binding can still be found when its identity changes.
        self._name = name or ""
        # binding key -> the device it resolved to. Re-enumerating every
        # backend on every poll would be wasteful with three bindings.
        self._resolved: dict[str, Device] = {}
        # kind -> (guid, index, button) for the momentary send/clear
        # bindings, and whether each is currently held.
        self._actions: dict[str, tuple[str, int | None, int]] = {}
        self._action_held: dict[str, bool] = {}
        self._pressed = False
        self._named_fallback_logged = False
        # Debug aid: log every button press on every device, whatever has
        # focus. See set_press_logging.
        self._log_presses = False
        self._press_state: dict[str, int] = {}
        self._capture: Callable[[Device, int], None] | None = None
        self._baseline: dict[str, int] | None = None
        self._settled = 0
        self._candidate: tuple[str, int] | None = None
        self._candidate_polls = 0
        self._stop = threading.Event()
        self._missing_logged = False

    # -- public ----------------------------------------------------------

    # The GUI holds a watcher, not the module, so enumeration has to be
    # reachable from the instance. Shipping these as module functions only is
    # what broke v0.1.4 on startup.
    def list_devices(self) -> list[tuple[int, str, int]]:
        return list_devices()

    def devices(self) -> list[Device]:
        return devices()

    def describe(self, device: int, button: int) -> str:
        return describe(device, button)

    def diagnose(self) -> list[str]:
        return diagnose()

    def snapshot(self) -> list[tuple[Device, list[int]]]:
        return snapshot()

    def binding_status(self) -> dict:
        """What the *bound* trigger sees right now.

        The whole reason this exists: capture and the trigger take different
        paths. Capture watches every device for an edge and does not care about
        identity; the trigger resolves a stored identity back to a device on
        every poll. A binding that captured fine and resolves to nothing is
        invisible from outside — the app simply does not react.
        """
        if self._button is None:
            return {"bound": False}

        device = self._resolve_device()
        mask = None if device is None else _mask(device)
        return {
            "bound": True,
            "button": self._button,
            "guid": self._guid,
            "device": device,
            "resolved": device is not None,
            "readable": mask is not None,
            "pressed": bool(mask and mask & (1 << (self._button - 1))),
        }

    def set_binding(
        self, device: int | None, button: int | None, guid: str | None = None,
        name: str | None = None,
    ) -> None:
        self._device, self._button = device, button
        self._guid = guid or ""
        self._name = name or ""
        self._resolved.clear()
        self._pressed = False
        self._missing_logged = False
        self._named_fallback_logged = False

    def set_actions(self, actions: dict[str, tuple[str, int | None, int]]) -> None:
        """Bind the momentary buttons: kind -> (guid, device index, button)."""
        self._actions = dict(actions)
        self._action_held.clear()
        self._resolved.clear()

    def set_press_logging(self, enabled: bool) -> None:
        """Log every button press on every device, to the ordinary app log.

        Binding a controller normally requires the window to have focus, and
        for anything Steam mediates that changes what the controller *is* —
        Steam Input applies its Desktop configuration to a non-Steam window and
        the game's configuration to the game, so the same physical button
        reports differently depending on what is focused. Capturing a binding
        in the GUI therefore records the desktop-mode button, and the one
        pressed while racing never matches.

        With this on, press the button with the *game* focused and read the
        number out of the log afterwards. It is the only way to learn what the
        app sees at the moment that matters.
        """
        self._log_presses = enabled
        self._press_state.clear()
        if enabled:
            log.info("controller press logging on — press a button with your "
                     "game focused, then look for 'controller press' here")

    def _poll_press_log(self) -> None:
        for device in devices():
            mask = _mask(device)
            if mask is None:
                continue
            previous = self._press_state.get(device.key, 0)
            for bit in range(device.buttons or 128):
                weight = 1 << bit
                if mask & weight and not previous & weight:
                    log.info("controller press: %s button %d  [%s] %s",
                             device.name, bit + 1, device.api,
                             device.guid or "no identity")
            self._press_state[device.key] = mask

    def start_capture(self, on_captured: Callable[[Device, int], None]) -> None:
        """Report the next button *pressed* on any device.

        Buttons already held when capture starts are ignored until they are
        released and pressed again — see the module docstring.
        """
        self._baseline = None
        self._settled = 0
        self._candidate = None
        self._capture = on_captured

    def cancel_capture(self) -> None:
        self._capture = None
        self._baseline = None
        self._settled = 0
        self._candidate = None

    def stop(self) -> None:
        self._stop.set()
        for backend in backends():
            try:
                backend.stop()
            except Exception:
                log.debug("%s teardown failed", backend.version, exc_info=True)

    # -- thread body -----------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._capture is not None:
                    self._poll_capture()
                else:
                    if self._button is not None:
                        self._poll_binding()
                    if self._actions:
                        self._poll_actions()
                    if self._log_presses:
                        self._poll_press_log()
            except Exception:
                # A disconnected wheel must not take the thread down; the
                # trigger would then be silently dead until a restart.
                log.exception("joystick poll failed")
            self._stop.wait(POLL_SECONDS)

    def _device_for(self, guid: str, fallback: int | None) -> Device | None:
        """The device a binding refers to, or None if it is not attached.

        Cached, because resolving three bindings against every backend on every
        12ms poll would mean re-enumerating hundreds of times a second.
        """
        cached = self._resolved.get(guid or f"index:{fallback}")
        if cached is not None and _still_attached(cached):
            return cached

        found = _find(guid, fallback)
        if found is None:
            self._resolved.pop(guid or f"index:{fallback}", None)
        else:
            self._resolved[guid or f"index:{fallback}"] = found
        return found

    def _resolve_device(self) -> Device | None:
        found = self._device_for(self._guid, self._device)
        if found is not None or not self._name:
            return found

        # The identity matched nothing attached. Before giving up, try the name
        # it was bound under: some devices — anything Steam mediates in
        # particular — report a different GUID from one session to the next,
        # which makes a binding capture perfectly and then resolve to nothing
        # on every poll, with no symptom beyond the trigger doing nothing.
        for device in devices():
            if device.name == self._name:
                if not self._named_fallback_logged:
                    self._named_fallback_logged = True
                    log.warning(
                        "the controller bound as %r no longer reports the same "
                        "identity (%s); matching it by name instead",
                        self._name, self._guid or "none")
                return device
        return None

    def _poll_actions(self) -> None:
        """Momentary bindings: one event on the press edge, none on release."""
        for kind, (guid, index, button) in self._actions.items():
            device = self._device_for(guid, index)
            mask = None if device is None else _mask(device)
            if mask is None:
                self._action_held[kind] = False
                continue
            down = bool(mask & (1 << (button - 1)))
            if down and not self._action_held.get(kind, False) and self._is_enabled():
                self._post(kind)
            self._action_held[kind] = down

    def _poll_binding(self) -> None:
        device = self._resolve_device()
        mask = None if device is None else _mask(device)
        if mask is None:
            if not self._missing_logged:
                self._missing_logged = True
                log.warning(
                    "the controller bound to the trigger is not responding; "
                    "its button trigger is inactive until it reconnects"
                )
            # A device that vanishes mid-press must not leave the cycle open.
            if self._pressed:
                self._pressed = False
                if self._is_enabled():
                    self._post(TRIGGER_UP)
            return

        if self._missing_logged:
            log.info("the controller bound to the trigger is back")
        self._missing_logged = False

        down = bool(mask & (1 << (self._button - 1)))
        if down and not self._pressed:
            self._pressed = True
            if self._is_enabled():
                self._post(TRIGGER_DOWN)
        elif not down and self._pressed:
            self._pressed = False
            if self._is_enabled():
                self._post(TRIGGER_UP)

    def _poll_capture(self) -> None:
        snapshot: dict[str, tuple[Device, int]] = {}
        for device in devices():
            mask = _mask(device)
            if mask is None:
                continue
            snapshot[device.key] = (device, mask)

        # Settling window. Everything seen active at any point during it is
        # folded into the ignore set — not just what was active on the first
        # frame. Rims and button boxes carry latched switches, and a Steam
        # Controller reports touchpads and grip sensors as buttons that sit on
        # or flicker. Any of them would otherwise win the binding before the
        # user has pressed anything.
        if self._baseline is None:
            self._baseline = {}
        if self._settled < CAPTURE_SETTLE_POLLS:
            for key, (_device, mask) in snapshot.items():
                self._baseline[key] = self._baseline.get(key, 0) | mask
            self._settled += 1
            return

        for key, (device, mask) in snapshot.items():
            fresh = mask & ~self._baseline.get(key, 0)
            if not fresh:
                continue
            # Lowest set bit wins if several arrive together, so the result is
            # stable rather than dependent on iteration order.
            button = (fresh & -fresh).bit_length()

            # Held across consecutive polls before it counts. A sensor that
            # blips for one frame cannot take the binding from a real press.
            if self._candidate != (key, button):
                self._candidate = (key, button)
                self._candidate_polls = 1
                return
            self._candidate_polls += 1
            if self._candidate_polls < CAPTURE_CONFIRM_POLLS:
                return

            callback, self._capture = self._capture, None
            self._baseline = None
            self._candidate = None
            try:
                callback(device, button)
            except Exception:
                log.exception("joystick capture callback failed")
            return

        # Nothing fresh this poll: a candidate that vanished was a blip.
        self._candidate = None

        # Released buttons stop counting as held, so a switch can still be
        # bound by flicking it off and on again.
        for key, (_device, mask) in snapshot.items():
            self._baseline[key] = self._baseline.get(key, 0) & mask

    def _post(self, kind: str) -> None:
        try:
            self._events.put_nowait((kind, time.monotonic()))
        except queue.Full:
            log.warning("trigger queue full; dropped joystick %s", kind)
