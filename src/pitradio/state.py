"""Shared state between the worker, the hook and the GUI.

tkinter may only be touched from the thread that created it, so nothing here
calls into the GUI. Instead everything publishes onto a queue that the GUI
drains on a timer. One mechanism for status, log lines, history and update
notices — a second would just be another thing to get out of sync.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# Event kinds placed on AppState.events.
EV_STATUS = "status"
EV_LOG = "log"
EV_HISTORY = "history"
EV_LEVEL = "level"
EV_UPDATE = "update"
EV_MODEL = "model"

# Trigger event kinds, shared by the keyboard hook and the worker. They live
# here rather than in hook.py because the GUI needs to name them too, and
# gui.py must never import anything that reaches winapi.
TRIGGER_DOWN = "down"
TRIGGER_UP = "up"
TRIGGER_SEND = "send"
TRIGGER_CLEAR = "clear"

STATUS_STARTING = "starting"
STATUS_LOADING = "loading model"
STATUS_IDLE = "idle"
STATUS_RECORDING = "recording"
STATUS_TRANSCRIBING = "transcribing"
STATUS_TYPING = "typing"
# Typed into the chat box and waiting for the driver to send or clear it,
# which only happens when a profile has auto_send off.
STATUS_REVIEW = "waiting to send"
STATUS_DISABLED = "disabled"


@dataclass
class HistoryEntry:
    when: float
    exe: str
    profile: str
    text: str
    typed: bool
    transcribe_seconds: float
    total_seconds: float

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.when))


@dataclass
class UpdateInfo:
    version: str
    notes: str
    asset_url: str
    checksum_url: str
    asset_name: str


class AppState:
    """Thread-safe scratchpad plus a fan-out queue for the GUI."""

    def __init__(self, max_history: int = 100):
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()

        # Read on every keystroke from inside the hook callback, so it stays a
        # plain attribute — no lock on the hot path.
        self.enabled = True

        self.status = STATUS_STARTING
        self.elevated: bool | None = None
        self.last_exe: str | None = None
        self.last_profile: str = "default"
        self.history: deque[HistoryEntry] = deque(maxlen=max_history)
        self.pending_update: UpdateInfo | None = None
        self.config_problems: list[str] = []

    # -- publishing ------------------------------------------------------

    def emit(self, kind: str, payload: Any = None) -> None:
        self.events.put((kind, payload))

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status
        self.emit(EV_STATUS, status)

    def set_context(self, exe: str | None, profile: str) -> None:
        with self._lock:
            self.last_exe = exe
            self.last_profile = profile
        self.emit(EV_STATUS, self.status)

    def set_enabled(self, value: bool) -> None:
        self.enabled = value
        self.set_status(STATUS_IDLE if value else STATUS_DISABLED)

    def add_history(self, entry: HistoryEntry) -> None:
        with self._lock:
            self.history.appendleft(entry)
        self.emit(EV_HISTORY, entry)

    def set_pending_update(self, info: UpdateInfo | None) -> None:
        with self._lock:
            self.pending_update = info
        self.emit(EV_UPDATE, info)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "enabled": self.enabled,
                "elevated": self.elevated,
                "exe": self.last_exe,
                "profile": self.last_profile,
                "pending_update": self.pending_update,
                "problems": list(self.config_problems),
            }


class QueueLogHandler(logging.Handler):
    """Routes log records to the GUI through the same queue as everything else."""

    def __init__(self, state: AppState):
        super().__init__()
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.emit(EV_LOG, self.format(record))
        except Exception:
            self.handleError(record)
