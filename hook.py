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

TRIGGER_DOWN = "down"
TRIGGER_UP = "up"


class KeyboardHook(threading.Thread):
    """Installs the hook, pumps messages, and posts trigger events to a queue."""

    def __init__(
        self,
        trigger_vk: int,
        events: queue.Queue[tuple[str, float]],
        is_enabled: Callable[[], bool],
    ):
        super().__init__(name="hook", daemon=True)
        self._trigger_vk = trigger_vk
        self._events = events
        self._is_enabled = is_enabled
        self._pressed = False
        self._hook = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        # Held in an attribute, not a local: if the CFUNCTYPE wrapper is
        # collected while Windows still holds the pointer, the next keypress
        # crashes the process.
        self._proc = HOOKPROC(self._callback)

    # -- public ----------------------------------------------------------

    def set_trigger(self, trigger_vk: int) -> None:
        """Config hot-reload can change the trigger key mid-session."""
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

        if info.vkCode != self._trigger_vk or not self._is_enabled():
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Auto-repeat is still swallowed either way, just not acted on: the
        # `_pressed` guard is what turns a stream of repeats into one event.
        if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) and not self._pressed:
            self._pressed = True
            self._post(TRIGGER_DOWN)
        elif w_param in (WM_KEYUP, WM_SYSKEYUP) and self._pressed:
            self._pressed = False
            self._post(TRIGGER_UP)

        return 1  # swallow: the game must never see the trigger key

    def _post(self, kind: str) -> None:
        try:
            self._events.put_nowait((kind, time.monotonic()))
        except queue.Full:
            log.warning("trigger queue full; dropped %s", kind)
