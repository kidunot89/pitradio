"""The trigger state machine.

The heart of the app and, until now, untested — `worker.py` imports `winapi`
and `inject`, so it could not be imported off Windows at all. Stubbing that one
boundary makes the whole cycle testable, and it needs to be: v0.1.22 shipped a
call to a `_drop_pending` that was never written, on the error path of the
pending-message handler, where it would have killed the worker thread in
silence.

What is real here: `winapi` and `inject` themselves, the config store and its
file, hot-reload, the profile lookup, `speech.sanitize`, the mention matcher,
the gesture timing, and every branch of the worker.

Three things stand in, all of them at a hardware boundary. `inject.send_keys`
and `inject.type_text` are recorded rather than executed, so the tests can
assert *which* keys the worker decided to send. `winapi.foreground_exe`
answers which game is focused. The recorder and transcriber arrive through the
constructor — the seam the Worker was designed around — so real-shaped
stand-ins go in there rather than a patch.
"""

from __future__ import annotations

import json
import queue
import time

import numpy as np
import pytest

from pitradio import config as config_mod
from pitradio import state as state_mod


class FakeRecorder:
    """Implements speech.Recorder's interface; holds a canned clip."""

    def __init__(self, seconds=2.0):
        self.active = False
        self.seconds = seconds
        self.starts = 0
        self.stops = 0

    def start(self, audio_cfg):
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False
        self.stops += 1
        return np.zeros(int(16000 * self.seconds), dtype=np.float32)

    def duration(self, audio):
        return len(audio) / 16000.0


class FakeTranscriber:
    """Implements speech.Transcriber's interface; returns canned text."""

    def __init__(self, text="box this lap"):
        self.text = text
        self.prompts = []

    def transcribe(self, audio, whisper_cfg, extra_prompt=""):
        self.prompts.append(extra_prompt)
        return self.text

    def needs_reload(self, whisper_cfg):
        return False

    def load(self, whisper_cfg):
        pass


@pytest.fixture
def worker_setup(monkeypatch, foreground, tmp_path):
    """A Worker wired to a real config file, with `inject` recorded."""
    from pitradio import speech
    from pitradio import worker as worker_mod
    from pitradio.input import inject

    # Audio hardware, and the Win32 send path. Everything else is real.
    monkeypatch.setattr(speech, "play_cue", lambda *a, **k: None)

    sent: list[tuple] = []
    monkeypatch.setattr(inject, "send_keys",
                        lambda keys, hold, gap: sent.append(("keys", tuple(keys))))
    monkeypatch.setattr(inject, "type_text",
                        lambda text, delay, mode: sent.append(("text", text)))

    def build(raw, exe="le mans ultimate.exe", text="box this lap",
              clip_seconds=2.0):
        # Delays to zero so the cycle runs at test speed without patching time.
        raw.setdefault("default_profile", {}).update(
            pre_delay_ms=0, post_delay_ms=0, key_gap_ms=0, type_delay_ms=0)
        for profile in raw.get("profiles", {}).values():
            profile.update(pre_delay_ms=0, post_delay_ms=0)

        path = tmp_path / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = config_mod.ConfigStore(path)
        store.load()
        foreground(exe)

        app_state = state_mod.AppState()
        events: queue.Queue = queue.Queue()
        recorder = FakeRecorder(clip_seconds)
        transcriber = FakeTranscriber(text)
        worker = worker_mod.Worker(store, app_state, events, recorder, transcriber)
        return worker, store, app_state, sent, recorder, transcriber

    return build


def base_config():
    from pathlib import Path
    cfg = json.loads(
        (Path(__file__).parent.parent / "config.default.json").read_text(encoding="utf-8"))
    # The shipped default keeps recording for a second after the release, which
    # every cycle here would otherwise spend asleep for no benefit. The two
    # tests that care about it set their own value.
    cfg["audio"]["release_tail_ms"] = 0
    return cfg


def cycle(worker, at=None):
    """One press and release."""
    now = at if at is not None else time.monotonic()
    worker._on_down(now)
    worker._on_up(now + 1.0)


# -- the ordinary cycle ---------------------------------------------------


def test_a_full_cycle_opens_chat_types_and_sends(worker_setup):
    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)

    assert sent == [
        ("keys", ("enter",)),        # open the chat box
        ("text", "box this lap"),
        ("keys", ("enter",)),        # send it
    ]


def test_recording_starts_before_the_chat_keys(worker_setup):
    """pre_delay_ms is long enough that anything said during it would be lost."""
    worker, _store, _state, sent, recorder, _tr = worker_setup(base_config())

    worker._on_down(time.monotonic())
    assert recorder.starts == 1
    assert recorder.active
    # The chat-open keys went out after the recorder was already running.
    assert sent == [("keys", ("enter",))]


def _tail_before_stop(worker_setup, monkeypatch, tail_ms):
    """What the worker waited for, immediately before stopping the recorder."""
    from pitradio import worker as worker_mod

    cfg = base_config()
    cfg.setdefault("audio", {})["release_tail_ms"] = tail_ms
    worker, _store, _state, _sent, recorder, _tr = worker_setup(cfg)

    slept = []
    monkeypatch.setattr(worker_mod, "_sleep_ms", lambda ms: slept.append(ms))

    seen = {}
    stop = recorder.stop

    def watched_stop():
        seen["slept"] = list(slept)
        return stop()

    recorder.stop = watched_stop
    cycle(worker)
    return seen["slept"]


def test_recording_continues_past_the_release(worker_setup, monkeypatch):
    """People stop pressing before they stop speaking.

    The last word lands after the thumb comes off, so the clip ends
    mid-syllable — and Whisper transcribes the truncated sound as whatever it
    resembles, which makes the message wrong rather than merely short.
    """
    slept = _tail_before_stop(worker_setup, monkeypatch, 900)

    assert slept[-1] == 900, "the recorder was stopped without waiting"


def test_the_tail_can_be_turned_off(worker_setup, monkeypatch):
    """It is paid before every transcription, so it has to be optional."""
    assert _tail_before_stop(worker_setup, monkeypatch, 0)[-1] == 0


def test_the_matched_profile_is_reported(worker_setup):
    worker, _store, state, _sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)

    snapshot = state.snapshot()
    assert snapshot["exe"] == "le mans ultimate.exe"
    assert snapshot["profile"] == "le mans ultimate.exe"


def test_an_unknown_executable_uses_the_default_profile(worker_setup):
    worker, _store, state, _sent, _rec, _tr = worker_setup(
        base_config(), exe="notepad.exe")
    cycle(worker)

    assert state.snapshot()["profile"] == "default"


def test_the_transcription_lands_in_history(worker_setup):
    worker, _store, state, _sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)

    history = list(state.history)
    assert len(history) == 1
    assert history[0].text == "box this lap"
    assert history[0].typed is True


def test_text_is_truncated_to_the_game_limit(worker_setup):
    raw = base_config()
    raw["profiles"]["le mans ultimate.exe"]["max_chars"] = 10
    worker, _store, _state, sent, _rec, _tr = worker_setup(
        raw, text="a much longer message than ten characters")
    cycle(worker)

    typed = next(payload for kind, payload in sent if kind == "text")
    assert len(typed) <= 10


def test_the_status_returns_to_idle(worker_setup):
    worker, _store, state, _sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)
    assert state.snapshot()["status"] == state_mod.STATUS_IDLE


# -- nothing was said -----------------------------------------------------


def test_a_clip_below_the_minimum_aborts_without_typing(worker_setup):
    """Holding the key by accident must not put anything in the chat box."""
    worker, _store, _state, sent, _rec, _tr = worker_setup(
        base_config(), clip_seconds=0.1)
    cycle(worker)

    assert sent == [("keys", ("enter",)), ("keys", ("escape",))]
    assert not any(kind == "text" for kind, _ in sent)


def test_an_empty_transcription_aborts(worker_setup):
    worker, _store, state, sent, _rec, _tr = worker_setup(base_config(), text="   ")
    cycle(worker)

    assert ("keys", ("escape",)) in sent
    assert not any(kind == "text" for kind, _ in sent)
    assert next(iter(state.history)).typed is False


# -- a trigger arriving mid-cycle ----------------------------------------


def test_a_trigger_from_before_the_last_cycle_ended_is_ignored(worker_setup):
    """A stale message firing into the game a beat later is worse than none."""
    worker, _store, _state, sent, recorder, _tr = worker_setup(base_config())
    cycle(worker)
    before = len(sent)

    worker._on_down(0.0)                 # a timestamp from before the cycle ended
    assert len(sent) == before
    assert recorder.starts == 1


def test_a_release_with_no_press_does_nothing(worker_setup):
    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())
    worker._on_up(time.monotonic())
    assert sent == []


# -- auto_send off: the message waits ------------------------------------


def review_config():
    raw = base_config()
    raw["profiles"]["le mans ultimate.exe"]["auto_send"] = False
    return raw


def test_with_auto_send_off_the_message_is_typed_but_not_sent(worker_setup):
    worker, _store, state, sent, _rec, _tr = worker_setup(review_config())
    cycle(worker)

    assert sent == [("keys", ("enter",)), ("text", "box this lap")]
    assert state.snapshot()["status"] == state_mod.STATUS_REVIEW


def test_a_tap_sends_the_waiting_message(worker_setup):
    worker, _store, state, sent, _rec, _tr = worker_setup(review_config())
    cycle(worker)
    sent.clear()

    now = time.monotonic()
    worker._on_down(now)
    worker._on_up(now + 0.05)            # a tap
    assert sent == []                    # still inside the double-tap window

    worker._gestures._tapped_at = now - 10      # let the window elapse
    worker._on_gesture_elapsed()

    assert sent == [("keys", ("enter",))]
    assert state.snapshot()["status"] == state_mod.STATUS_IDLE


def test_two_taps_clear_the_waiting_message(worker_setup):
    worker, _store, state, sent, _rec, _tr = worker_setup(review_config())
    cycle(worker)
    sent.clear()

    now = time.monotonic()
    worker._on_down(now)
    worker._on_up(now + 0.05)
    worker._on_down(now + 0.10)
    worker._on_up(now + 0.15)

    assert sent == [("keys", ("escape",))]
    assert state.snapshot()["status"] == state_mod.STATUS_IDLE


def test_a_hold_clears_and_records_a_replacement(worker_setup):
    worker, _store, _state, sent, recorder, _tr = worker_setup(review_config())
    cycle(worker)
    sent.clear()
    starts_before = recorder.starts

    now = time.monotonic()
    worker._on_down(now)
    assert recorder.starts == starts_before + 1   # captures from the press
    worker._on_up(now + 1.0)                      # a hold

    assert sent == [
        ("keys", ("escape",)),           # clear what was waiting
        ("keys", ("enter",)),            # reopen the chat box
        ("text", "box this lap"),        # the replacement
    ]


def test_a_dedicated_send_binding_acts_at_once(worker_setup):
    """No double-tap window to wait out; that is the point of binding a button."""
    from pitradio.input import hook as hook_mod

    worker, _store, state, sent, _rec, _tr = worker_setup(review_config())
    cycle(worker)
    sent.clear()

    worker._on_action(hook_mod.TRIGGER_SEND)

    assert sent == [("keys", ("enter",))]
    assert state.snapshot()["status"] == state_mod.STATUS_IDLE


def test_a_dedicated_clear_binding_acts_at_once(worker_setup):
    from pitradio.input import hook as hook_mod

    worker, _store, _state, sent, _rec, _tr = worker_setup(review_config())
    cycle(worker)
    sent.clear()

    worker._on_action(hook_mod.TRIGGER_CLEAR)
    assert sent == [("keys", ("escape",))]


def test_send_with_nothing_waiting_does_nothing(worker_setup):
    from pitradio.input import hook as hook_mod

    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())
    worker._on_action(hook_mod.TRIGGER_SEND)
    assert sent == []


def test_auto_send_on_leaves_nothing_pending(worker_setup):
    worker, _store, _state, _sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)
    assert worker._pending is None


# -- error handling -------------------------------------------------------


def test_a_failure_mid_cycle_does_not_kill_the_worker(worker_setup, monkeypatch):
    """The trigger key keeps being swallowed with nothing on the other end."""
    worker, _store, state, _sent, _rec, transcriber = worker_setup(base_config())

    def explode(*a, **k):
        raise RuntimeError("transcription blew up")

    monkeypatch.setattr(transcriber, "transcribe", explode)

    worker._on_down(time.monotonic())
    with pytest.raises(RuntimeError):
        worker._on_up(time.monotonic() + 1.0)

    # run() catches it and calls _abandon; verify that leaves a usable state.
    worker._abandon()
    assert worker._active is None
    assert worker._pending is None
    assert state.snapshot()["status"] == state_mod.STATUS_IDLE


def test_abandon_is_what_the_gesture_error_path_calls(worker_setup):
    """v0.1.22 called a `_drop_pending` that did not exist, inside an except
    block — an AttributeError that would have killed the thread in silence."""
    worker, _store, _state, _sent, _rec, _tr = worker_setup(base_config())
    assert hasattr(worker, "_abandon")
    assert not hasattr(worker, "_drop_pending")


# -- hot reload -----------------------------------------------------------


def test_config_changes_take_effect_on_the_next_trigger(worker_setup):
    """No watcher thread; the worker stats the file at the start of a cycle."""
    import os

    worker, store, _state, sent, _rec, _tr = worker_setup(base_config())
    cycle(worker)
    assert ("keys", ("enter",)) in sent
    sent.clear()

    raw = base_config()
    raw["profiles"]["le mans ultimate.exe"]["pre_keys"] = ["t"]
    raw["profiles"]["le mans ultimate.exe"]["pre_delay_ms"] = 0
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    os.utime(store.path, (0, 0))

    cycle(worker, at=time.monotonic() + 10)
    assert sent[0] == ("keys", ("t",))


# -- resend from the history ---------------------------------------------


def test_resend_retypes_the_message(worker_setup, monkeypatch):
    from pitradio import worker as worker_mod

    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())
    # The countdown before a resend is real seconds, so it is skipped here
    # rather than waited out; the test below covers that it exists.
    monkeypatch.setattr(worker_mod.time, "sleep", lambda seconds: None)

    worker._resend("box this lap")

    assert sent == [
        ("keys", ("enter",)),
        ("text", "box this lap"),
        ("keys", ("enter",)),
    ]


def test_resend_counts_down_before_typing(worker_setup, monkeypatch):
    """The request comes from the GUI, which has focus at that moment.

    Without the delay the message is typed straight back into PitRadio's own
    window instead of the game.
    """
    from pitradio import worker as worker_mod

    worker, _store, _state, sent, _rec, _tr = worker_setup(base_config())

    slept: list[float] = []
    monkeypatch.setattr(worker_mod.time, "sleep", slept.append)

    worker._resend("box this lap")

    assert sum(slept) >= 3.0, "a resend must give you time to focus the game"
    # And the countdown happens before anything is typed.
    assert sent and sent[0][0] == "keys"
