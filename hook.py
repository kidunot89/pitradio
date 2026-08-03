"""Low-level keyboard hook on its own thread.

WH_KEYBOARD_LL rather than RegisterHotKey for two reasons the app depends on:
it reports key-down and key-up separately, which is what makes hold-to-talk
possible, and returning 1 from the callback swallows the key so the trigger
never reaches the game and ends up as a stray character in the chat box.

The hook needs a message pump on the thread that installed it. Without one,
Windows decides the hook is unresponsive and silently unregisters it after
LowLevelHooksTimeout — the app keeps running and simply stops working.

The callback does nothing but push onto a queue. Anything slower risks the same
timeout, and the timeout is not reported anywhere.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

import keys
import winapi
from winapi import (
    HC_ACTION,
    HOOKPROC,
    KBDLLHOOKSTRUCT,
    WH_KEYBOARD_LL,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_QUIT,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    kernel32,
    user32,
)

log = logging.getLogger(__name__)

# Re-exported from state so the GUI can name them without importing winapi.
# Actions are momentary: one event on press, none on release, so the worker
# never has to pair them up.
from state import (  # noqa: E402,F401  (re-exported for callers)
    TRIGGER_CLEAR,
    TRIGGER_DOWN,
    TRIGGER_SEND,
    TRIGGER_UP,
)


class KeyboardHook(threading.Thread):
    """Installs the hook, pumps messages, and posts trigger events to a queue."""

    def __init__(
        self,
        trigger_vk: int,
        events: queue.Queue[tuple[str, float]],
        is_enabled: Callable[[], bool],
        modifiers: list[int] | None = None,
    ):
        super().__init__(name="hook", daemon=True)
        self._trigger_vk = trigger_vk
        self._modifiers = [keys.generic_modifier(m) for m in (modifiers or [])]
        self._events = events
        self._is_enabled = is_enabled
        self._pressed = False
        # kind -> (vk, modifiers) for the momentary send/clear keys, and
        # which of them are currently held so auto-repeat fires once.
        self._actions: dict[str, tuple[int, list[int]]] = {}
        self._action_held: set[str] = set()
        # Set while the GUI is asking the user to press a key. The callback
        # reports what was pressed and swallows it, so binding Enter or Escape
        # doesn't also actuate whatever is behind the dialog.
        self._capture: Callable[[list[int], int], None] | None = None
        self._held_modifiers: set[int] = set()
        self._hook = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        # Held in an attribute, not a local: if the CFUNCTYPE wrapper is
        # collected while Windows still holds the pointer, the next keypress
        # crashes the process.
        self._proc = HOOKPROC(self._callback)

    # -- public ----------------------------------------------------------

    def describe_binding(self) -> str:
        """What the hook is armed with *right now*.

        Read from the hook rather than from config on purpose: the whole point
        is to show what will actually fire, so a key that was saved but never
        applied is visible instead of silently doing nothing.
        """
        return keys.format_combo(self._modifiers, self._trigger_vk)

    def is_installed(self) -> bool:
        return self._hook is not None

    def start_capture(self, on_captured: Callable[[list[int], int], None]) -> None:
        """Report the next non-modifier keypress instead of acting on it."""
        self._capture = on_captured

    def cancel_capture(self) -> None:
        self._capture = None

    def set_actions(self, actions: dict[str, tuple[int, list[int]]]) -> None:
        """Bind the momentary keys, replacing whatever was bound before.

        Applied on save like the trigger is, not left to the worker's next
        reload: these exist to act on a message that is waiting *now*.
        """
        self._actions = {
            kind: (vk, [keys.generic_modifier(m) for m in mods])
            for kind, (vk, mods) in actions.items()
        }
        self._action_held.clear()

    def set_trigger(self, trigger_vk: int, modifiers: list[int] | None = None) -> None:
        """Config hot-reload can change the trigger key mid-session."""
        self._modifiers = [keys.generic_modifier(m) for m in (modifiers or [])]
        if trigger_vk != self._trigger_vk:
            self._trigger_vk = trigger_vk
            # Otherwise a key held across the change would never see its
            # matching release and the flag would stay stuck.
            self._pressed = False

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # -- thread body -----------------------------------------------------

    def run(self) -> None:
        self._thread_id = winapi.current_thread_id()
        module = kernel32.GetModuleHandleW(None)

        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, module, 0)
        if not self._hook:
            err = ctypes.get_last_error()
            log.error("SetWindowsHookExW failed (error %d); trigger key is dead", err)
            self._ready.set()
            return

        log.info("keyboard hook installed on thread %d", self._thread_id)
        self._ready.set()

        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            log.info("keyboard hook removed")

    # -- callback --------------------------------------------------------

    def _callback(self, n_code, w_param, l_param):
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

        # Our own injected keystrokes. Reacting to these would mean the Enter
        # we send to open the chat box immediately re-triggers the app.
        if info.dwExtraInfo == winapi.INJECT_TAG:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Track modifier state ourselves, before any decision to swallow.
        # Returning 1 from a low-level hook discards the event, and a discarded
        # keypress never updates the state GetAsyncKeyState reports — so during
        # capture, where everything is swallowed, Ctrl always read as "up" and
        # ctrl+f12 could never be captured.
        generic = keys.generic_modifier(info.vkCode)
        if generic in keys.MODIFIER_CODES:
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._held_modifiers.add(generic)
            elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                self._held_modifiers.discard(generic)

        if self._capture is not None:
            return self._handle_capture(info, n_code, w_param, l_param)

        if not self._is_enabled():
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Checked before the trigger so that binding the same key to both is a
        # visible config mistake rather than one silently shadowing the other.
        for kind, (vk, modifiers) in self._actions.items():
            if info.vkCode == vk:
                return self._handle_action(
                    kind, modifiers, n_code, w_param, l_param)

        if info.vkCode != self._trigger_vk:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # A low-level hook reports one key at a time and never a combination,
        # so a combo is recognised by checking modifier state when the main key
        # arrives. Only the main key is ever swallowed — swallowing Ctrl would
        # break it system-wide.
        if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) and not self._pressed:
            if not self._modifiers_held():
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            self._pressed = True
            self._post(TRIGGER_DOWN)
        elif w_param in (WM_KEYUP, WM_SYSKEYUP) and self._pressed:
            # Release fires regardless of modifier state: letting go of Ctrl
            # before the main key must not strand a recording.
            self._pressed = False
            self._post(TRIGGER_UP)
        elif not self._pressed:
            # A press we did not claim — pass it through untouched.
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Auto-repeat is swallowed too; `_pressed` is what turns a stream of
        # repeats into a single event.
        return 1  # the game must never see the trigger key

    def _handle_action(self, kind, modifiers, n_code, w_param, l_param):
        """A momentary key: one event on press, nothing on release."""
        if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if kind in self._action_held:
                return 1  # auto-repeat, already reported
            if not self._modifiers_held(modifiers):
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            self._action_held.add(kind)
            self._post(kind)
            return 1
        if w_param in (WM_KEYUP, WM_SYSKEYUP) and kind in self._action_held:
            self._action_held.discard(kind)
            return 1
        # A press we never claimed — pass it through untouched.
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _is_modifier_down(self, generic: int) -> bool:
        """Either source of truth will do.

        Our own tracking covers events we swallowed, which GetAsyncKeyState
        would miss; GetAsyncKeyState covers a modifier already held when the
        hook was installed, which our tracking would miss.
        """
        return generic in self._held_modifiers or winapi.is_key_down(generic)

    def _modifiers_held(self, modifiers: list[int] | None = None) -> bool:
        wanted = self._modifiers if modifiers is None else modifiers
        return all(self._is_modifier_down(mod) for mod in wanted)

    def _handle_capture(self, info, n_code, w_param, l_param):
        """Report the pressed key to the GUI and swallow it."""
        if keys.is_modifier(info.vkCode):
            # Wait for the real key; a modifier alone is not a binding. Passed
            # through rather than swallowed so the system's own key state stays
            # accurate — and Ctrl on its own does nothing to the window behind.
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        if w_param not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return 1

        held = [code for code in keys.MODIFIER_CODES if self._is_modifier_down(code)]
        callback, self._capture = self._capture, None
        try:
            callback(held, info.vkCode)
        except Exception:
            log.exception("key capture callback failed")
        return 1

    def _post(self, kind: str) -> None:
        try:
            self._events.put_nowait((kind, time.monotonic()))
        except queue.Full:
            log.warning("trigger queue full; dropped %s", kind)
