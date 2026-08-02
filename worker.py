"""The trigger state machine.

Everything slow lives here, on one thread, so the hook callback can return
immediately. The ordering in `_on_down` is deliberate: recording starts *before*
the chat-box keys are sent, because `pre_delay_ms` is long enough that anything
said during it would otherwise be lost.

Stage timings are logged at each step. "The chat box wasn't open yet" and "the
transcription was slow" look identical from the driver's seat and completely
different in the log.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import hook as hook_mod
import inject
import speech
import state as state_mod
import winapi
from config import ConfigStore, Profile
from state import AppState, HistoryEntry

log = logging.getLogger(__name__)

EV_RESEND = "resend"
EV_STOP = "stop"


class Worker(threading.Thread):
    def __init__(
        self,
        store: ConfigStore,
        app_state: AppState,
        events: queue.Queue[tuple[str, object]],
        recorder: speech.Recorder,
        transcriber: speech.Transcriber,
    ):
        super().__init__(name="worker", daemon=True)
        self.store = store
        self.state = app_state
        self.events = events
        self.recorder = recorder
        self.transcriber = transcriber
        self.hook: hook_mod.KeyboardHook | None = None

        self._active: dict | None = None
        # Anything queued before this instant belongs to a cycle we already
        # handled and is stale by the time we get to it.
        self._cycle_ended = 0.0

    # -- public ----------------------------------------------------------

    def request_resend(self, text: str) -> None:
        self.events.put((EV_RESEND, text))

    def stop(self) -> None:
        self.events.put((EV_STOP, None))

    # -- thread body -----------------------------------------------------

    def run(self) -> None:
        while True:
            kind, payload = self.events.get()
            try:
                if kind == EV_STOP:
                    return
                if kind == hook_mod.TRIGGER_DOWN:
                    self._on_down(float(payload))
                elif kind == hook_mod.TRIGGER_UP:
                    self._on_up(float(payload))
                elif kind == EV_RESEND:
                    self._resend(str(payload))
            except Exception:
                # One bad cycle must not take the worker down; the trigger key
                # would keep being swallowed with nothing on the other end.
                log.exception("trigger cycle failed")
                self._abandon()

    # -- cycle -----------------------------------------------------------

    def _on_down(self, pressed_at: float) -> None:
        if pressed_at < self._cycle_ended:
            log.info("ignored a trigger pressed while the previous one was still running")
            return

        self._reload_if_changed()
        cfg = self.store.config

        exe = winapi.foreground_exe()
        profile, matched = cfg.profile_for(exe)
        log.info("trigger: exe=%s profile=%s", exe or "<unknown>", matched)
        self.state.set_context(exe, matched)
        self.state.set_status(state_mod.STATUS_RECORDING)

        started = time.perf_counter()
        speech.play_cue(cfg.cues, cfg.cues.start_hz)
        self.recorder.start(cfg.audio)

        inject.send_keys(profile.pre_keys, profile.key_hold_ms, profile.key_gap_ms)
        log.info("pre-keys sent (+%.0fms)", (time.perf_counter() - started) * 1000)
        _sleep_ms(profile.pre_delay_ms)

        self._active = {
            "started": started,
            "profile": profile,
            "exe": exe or "<unknown>",
            "matched": matched,
        }

    def _on_up(self, released_at: float) -> None:
        active = self._active
        self._active = None
        if active is None:
            return

        cfg = self.store.config
        profile: Profile = active["profile"]

        speech.play_cue(cfg.cues, cfg.cues.stop_hz)
        audio = self.recorder.stop()
        clip_seconds = self.recorder.duration(audio)

        if clip_seconds < cfg.audio.min_clip_seconds:
            log.info("clip was %.2fs, below min_clip_seconds; aborting", clip_seconds)
            self._abort(active, profile, "", 0.0)
            return

        self.state.set_status(state_mod.STATUS_TRANSCRIBING)
        transcribe_started = time.perf_counter()
        raw = self.transcriber.transcribe(audio, cfg.whisper)
        transcribe_seconds = time.perf_counter() - transcribe_started
        log.info(
            "transcribed %.2fs of audio in %.2fs: %r", clip_seconds, transcribe_seconds, raw
        )

        text = speech.sanitize(raw, profile.max_chars)
        if not text:
            log.info("nothing was said; firing abort keys instead of sending")
            self._abort(active, profile, "", transcribe_seconds)
            return

        self.state.set_status(state_mod.STATUS_TYPING)
        inject.type_text(text, profile.type_delay_ms, profile.text_mode)
        _sleep_ms(profile.post_delay_ms)
        inject.send_keys(profile.post_keys, profile.key_hold_ms, profile.key_gap_ms)

        total = time.perf_counter() - active["started"]
        log.info("sent %d chars in %.2fs total", len(text), total)

        self._finish(
            HistoryEntry(
                when=time.time(),
                exe=active["exe"],
                profile=active["matched"],
                text=text,
                typed=True,
                transcribe_seconds=transcribe_seconds,
                total_seconds=total,
            )
        )

    def _abort(self, active: dict, profile: Profile, text: str, transcribe_seconds: float) -> None:
        inject.send_keys(profile.abort_keys, profile.key_hold_ms, profile.key_gap_ms)
        self._finish(
            HistoryEntry(
                when=time.time(),
                exe=active["exe"],
                profile=active["matched"],
                text=text,
                typed=False,
                transcribe_seconds=transcribe_seconds,
                total_seconds=time.perf_counter() - active["started"],
            )
        )

    def _finish(self, entry: HistoryEntry) -> None:
        self.state.add_history(entry)
        self._cycle_ended = time.monotonic()
        self.state.set_status(
            state_mod.STATUS_IDLE if self.state.enabled else state_mod.STATUS_DISABLED
        )

    def _abandon(self) -> None:
        """Best-effort cleanup after an unexpected failure mid-cycle."""
        self._active = None
        try:
            if self.recorder.active:
                self.recorder.stop()
        except Exception:
            log.debug("recorder cleanup failed", exc_info=True)
        self._cycle_ended = time.monotonic()
        self.state.set_status(
            state_mod.STATUS_IDLE if self.state.enabled else state_mod.STATUS_DISABLED
        )

    # -- resend ----------------------------------------------------------

    def _resend(self, text: str) -> None:
        """Retype a past message into whatever is focused when the countdown ends.

        The countdown exists because the request comes from the GUI, which by
        definition has focus at that moment — without it the message would be
        typed straight back into this window.
        """
        cfg = self.store.config
        for remaining in (3, 2, 1):
            log.info("resending in %d...", remaining)
            time.sleep(1.0)

        exe = winapi.foreground_exe()
        profile, matched = cfg.profile_for(exe)
        log.info("resend: exe=%s profile=%s", exe or "<unknown>", matched)

        started = time.perf_counter()
        self.state.set_status(state_mod.STATUS_TYPING)
        inject.send_keys(profile.pre_keys, profile.key_hold_ms, profile.key_gap_ms)
        _sleep_ms(profile.pre_delay_ms)

        clipped = speech.sanitize(text, profile.max_chars)
        inject.type_text(clipped, profile.type_delay_ms, profile.text_mode)
        _sleep_ms(profile.post_delay_ms)
        inject.send_keys(profile.post_keys, profile.key_hold_ms, profile.key_gap_ms)

        self._finish(
            HistoryEntry(
                when=time.time(),
                exe=exe or "<unknown>",
                profile=matched,
                text=clipped,
                typed=True,
                transcribe_seconds=0.0,
                total_seconds=time.perf_counter() - started,
            )
        )

    # -- config ----------------------------------------------------------

    def _reload_if_changed(self) -> None:
        if not self.store.maybe_reload():
            return

        cfg = self.store.config
        log.info("config reloaded from %s", self.store.path)
        for problem in self.store.problems:
            log.warning("config: %s", problem)
        self.state.config_problems = list(self.store.problems)

        if self.hook is not None:
            try:
                import keys

                self.hook.set_trigger(keys.parse_key(cfg.trigger_key))
            except Exception as exc:
                log.error("keeping the previous trigger key: %s", exc)

        if self.transcriber.needs_reload(cfg.whisper):
            log.warning("whisper settings changed; reloading the model (this is slow)")
            self.state.set_status(state_mod.STATUS_LOADING)
            self.transcriber.load(cfg.whisper)


def _sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)
