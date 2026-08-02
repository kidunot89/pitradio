"""Synthetic keyboard input via SendInput.

Two regimes, and mixing them up is the difference between this working and
being unusable:

* **Keys** (chat open/send/abort) go out as hardware scan codes and are held
  for `key_hold_ms`, because games sample input once per frame — a press with
  no measurable duration is simply never observed.
* **Text** goes out as UTF-16 code units with KEYEVENTF_UNICODE. Those are
  delivered through the window message queue rather than polled, so they need
  no hold time. Applying the 40ms key hold per character would make a
  200-character message take eight seconds.

Every event carries INJECT_TAG in dwExtraInfo so the keyboard hook can tell our
own output apart from a real keypress.
"""

from __future__ import annotations

import ctypes
import logging
import time

import keys
import winapi
from winapi import (
    INPUT,
    INPUT_KEYBOARD,
    INPUTUNION,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    KEYEVENTF_UNICODE,
    MAPVK_VK_TO_VSC_EX,
    user32,
)

log = logging.getLogger(__name__)

ERROR_ACCESS_DENIED = 5

# Logged once per process. UIPI failures repeat on every single event, and a
# log full of identical lines buries the timings the log exists to show.
_reported_uipi = False


def _sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


def _send(inputs: list[INPUT]) -> bool:
    if not inputs:
        return True

    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent == len(inputs):
        return True

    global _reported_uipi
    err = ctypes.get_last_error()
    if err == ERROR_ACCESS_DENIED and not _reported_uipi:
        _reported_uipi = True
        log.error(
            "SendInput was blocked (ERROR_ACCESS_DENIED). The focused window "
            "belongs to a higher-integrity process, so UIPI is discarding our "
            "input. Run PitRadio as administrator."
        )
    elif err != ERROR_ACCESS_DENIED:
        log.error("SendInput sent %d/%d events, last error %d", sent, len(inputs), err)
    return False


def _key_event(vk: int, *, up: bool) -> INPUT:
    """One scan-code key event for a virtual-key code."""
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)
    # MAPVK_VK_TO_VSC_EX reports extended keys as 0xE0xx; the scan code itself
    # is the low byte and the prefix becomes a flag.
    extended = (scan >> 8) == 0xE0 or vk in keys.EXTENDED
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= KEYEVENTF_KEYUP

    return INPUT(
        type=INPUT_KEYBOARD,
        u=INPUTUNION(
            ki=KEYBDINPUT(
                wVk=0,
                wScan=scan & 0xFF,
                dwFlags=flags,
                time=0,
                dwExtraInfo=winapi.INJECT_TAG,
            )
        ),
    )


def _unicode_event(code_unit: int, *, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        u=INPUTUNION(
            ki=KEYBDINPUT(
                wVk=0,
                wScan=code_unit,
                dwFlags=flags,
                time=0,
                dwExtraInfo=winapi.INJECT_TAG,
            )
        ),
    )


def press_combo(spec: str, hold_ms: int) -> None:
    """Press one key spec, e.g. "enter" or "ctrl+enter"."""
    mods, vk = keys.parse_combo(spec)

    for mod in mods:
        _send([_key_event(mod, up=False)])
    _send([_key_event(vk, up=False)])
    _sleep_ms(hold_ms)
    _send([_key_event(vk, up=True)])
    for mod in reversed(mods):
        _send([_key_event(mod, up=True)])


def send_keys(specs: list[str], hold_ms: int, gap_ms: int) -> None:
    """Fire a sequence of key specs — the pre/post/abort key lists."""
    for index, spec in enumerate(specs):
        try:
            press_combo(spec, hold_ms)
        except keys.KeyNameError as exc:
            # Validation catches this at load time; if it still happens, skip
            # the bad key rather than abandoning the rest of the sequence.
            log.error("skipping unusable key %r: %s", spec, exc)
            continue
        if index < len(specs) - 1:
            _sleep_ms(gap_ms)


def type_text(text: str, delay_ms: int, mode: str = "unicode") -> None:
    if mode == "scancode":
        _type_scancode(text, delay_ms)
    else:
        _type_unicode(text, delay_ms)


def _type_unicode(text: str, delay_ms: int) -> None:
    """One UTF-16 code unit at a time.

    Encoding to UTF-16-LE rather than iterating characters handles astral
    characters for free: they arrive as their two surrogate units, which is
    exactly what KEYEVENTF_UNICODE expects.
    """
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        unit = units[i] | (units[i + 1] << 8)
        _send([_unicode_event(unit, up=False), _unicode_event(unit, up=True)])
        _sleep_ms(delay_ms)


def _type_scancode(text: str, delay_ms: int) -> None:
    """Fallback for games that ignore KEYEVENTF_UNICODE entirely.

    Maps each character to a keystroke on the active layout, so it can only
    produce what the layout can type — anything else is dropped with a log
    line rather than silently mangled.
    """
    for char in text:
        result = user32.VkKeyScanW(char)
        if result == -1:
            log.warning("scancode mode cannot type %r on this layout; skipped", char)
            continue

        vk = result & 0xFF
        shift_state = (result >> 8) & 0xFF
        mods = []
        if shift_state & 1:
            mods.append(keys.VK["shift"])
        if shift_state & 2:
            mods.append(keys.VK["ctrl"])
        if shift_state & 4:
            mods.append(keys.VK["alt"])

        for mod in mods:
            _send([_key_event(mod, up=False)])
        _send([_key_event(vk, up=False), _key_event(vk, up=True)])
        for mod in reversed(mods):
            _send([_key_event(mod, up=True)])
        _sleep_ms(delay_ms)
