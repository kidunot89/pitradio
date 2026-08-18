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

from pitradio import gestures as gestures_mod
from pitradio import mentions as mentions_mod
from pitradio import speech
from pitradio import state as state_mod
from pitradio.config import ConfigStore, Profile
from pitradio.input import hook as hook_mod
from pitradio.input import inject, winapi
from pitradio.state import AppState, HistoryEntry

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
        self.plugins = None
        #: Set by the app when voice is available. None everywhere else — in
        #: `--gui-only`, in tests, and in any build without the websocket
        #: package — and every use site copes with that.
        self.voice = None
        #: The race engineer, when there is one. Consulted after transcription
        #: and before anything is typed; None everywhere it is not built.
        self.engineer = None

        self._active: dict | None = None
        # A message typed but not sent, waiting on a gesture. Only ever set
        # when the matched profile has auto_send off.
        self._pending: dict | None = None
        self._gestures = gestures_mod.Gestures()
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
            # A lone tap only becomes a send once the double-tap window closes,
            # and nothing else will wake us to notice that — so when one is
            # outstanding, wait for it rather than blocking indefinitely.
            timeout = self._gestures.deadline(time.monotonic())
            try:
                kind, payload = self.events.get(timeout=timeout)
            except queue.Empty:
                try:
                    self._on_gesture_elapsed()
                except Exception:
                    log.exception("pending message gesture failed")
                    self._abandon()
                continue

            try:
                if kind == EV_STOP:
                    return
                if kind == hook_mod.TRIGGER_DOWN:
                    self._on_down(float(payload))
                elif kind == hook_mod.TRIGGER_UP:
                    self._on_up(float(payload))
                elif kind in (hook_mod.TRIGGER_SEND, hook_mod.TRIGGER_CLEAR):
                    self._on_action(kind)
                elif kind == EV_RESEND:
                    self._resend(str(payload))
            except Exception:
                # One bad cycle must not take the worker down; the trigger key
                # would keep being swallowed with nothing on the other end.
                log.exception("trigger cycle failed")
                self._abandon()

    # -- cycle -----------------------------------------------------------

    def _on_down(self, pressed_at: float) -> None:
        if self._pending is not None:
            self._gestures.press(pressed_at)
            # Start capturing straight away. If this turns out to be a hold it
            # is a re-record, and waiting to find out would swallow the first
            # words; if it turns out to be a tap the buffer is thrown away.
            if not self.recorder.active:
                self.recorder.start(self.store.config.audio)
            return

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
        speech.play_cue(cfg.cues, cfg.cues.start_hz, cfg.audio.output_device)
        self.recorder.start(cfg.audio)

        inject.send_keys(profile.pre_keys, profile.key_hold_ms, profile.key_gap_ms)
        log.info("pre-keys sent (+%.0fms)", (time.perf_counter() - started) * 1000)
        _sleep_ms(profile.pre_delay_ms)

        # Read the driver list now rather than after transcription: the
        # session can change while someone is talking, and the names that
        # matter are the ones from when they started.
        drivers: list[str] = []
        plugin_id = ""
        # Resolved whatever mentions are set to: voice needs the session even
        # when nobody's name is being marked up.
        if self.plugins is not None:
            plugin_id = self.plugins.resolve(profile.plugin, exe)
        if plugin_id and cfg.mentions.enabled:
            drivers = self.plugins.drivers_for(plugin_id)
            if drivers:
                # Named, not just counted: whether a mention works comes down to
                # what the sim actually calls someone, and "session has 1
                # driver" leaves you guessing at it.
                shown = ", ".join(drivers[:12])
                more = f" (+{len(drivers) - 12} more)" if len(drivers) > 12 else ""
                log.info("session drivers: %s%s", shown, more)

        self._active = {
            "started": started,
            "profile": profile,
            "exe": exe or "<unknown>",
            "matched": matched,
            "drivers": drivers,
            # Read alongside the drivers, from the same moment in the session.
            "vocabulary": (
                self.plugins.vocabulary_for(plugin_id)
                if drivers and cfg.mentions.add_names_to_vocabulary else []
            ),
            "standings": (
                self.plugins.standings_for(plugin_id)
                if drivers and self.plugins.settings_for(
                    plugin_id, profile.plugin_settings).get("positions")
                else None
            ),
            # Where this driver was when they started talking, so the clip can
            # say so. Read now for the same reason the drivers are.
            "session": (
                self.plugins.session_for(plugin_id)
                if plugin_id and cfg.voice.enabled else None
            ),
        }

    def _send_voice(self, active: dict, audio) -> None:
        """Hand the clip to the relay, if voice is on and there is a room.

        Wrapped whole, and deliberately: this is the newest and least proven
        thing in the trigger cycle, and it sits between somebody's recording
        and their message reaching the chat box. A relay problem must cost the
        audio and nothing else.
        """
        client = self.voice
        cfg = self.store.config
        session = active.get("session")
        if client is None or not cfg.voice.enabled or session is None or not session.key:
            return

        try:
            from pitradio import voice as voice_mod

            me = session.player()
            # Their own name from the sim, which is what everyone else already
            # sees on their timing screen.
            name = cfg.voice.display_name.strip() or (me.driver if me else "")
            if not name:
                log.info("voice: no name to send under; skipping the clip")
                return

            frame = voice_mod.encode_clip(
                voice_mod.Speaker(name, me.position if me else (0.0, 0.0, 0.0)),
                speech.to_pcm16(audio),
                sent_at=time.time(),
                rate=cfg.audio.samplerate,
            )
            client.send(frame)
            log.info("voice: sent %d bytes as %r", len(frame), name)
        except Exception:
            log.exception("sending the voice clip failed")

    def _on_up(self, released_at: float) -> None:
        if self._pending is not None:
            self._pending_up(released_at)
            return

        active = self._active
        self._active = None
        if active is None:
            return

        cfg = self.store.config
        profile: Profile = active["profile"]

        speech.play_cue(cfg.cues, cfg.cues.stop_hz, cfg.audio.output_device)
        audio = self.recorder.stop()
        clip_seconds = self.recorder.duration(audio)

        if clip_seconds < cfg.audio.min_clip_seconds:
            log.info("clip was %.2fs, below min_clip_seconds; aborting", clip_seconds)
            self._abort(active, profile, "", 0.0)
            return

        # Before transcription, not after: the clip is ready now, and Whisper
        # takes a second or more. Nothing about sending it depends on the text.
        self._send_voice(active, audio)

        self.state.set_status(state_mod.STATUS_TRANSCRIBING)
        transcribe_started = time.perf_counter()
        drivers = active.get("drivers") or []
        hint = mentions_mod.vocabulary_hint(
            active.get("vocabulary") or [], cfg.mentions.max_names)
        raw = self.transcriber.transcribe(audio, cfg.whisper, hint)
        transcribe_seconds = time.perf_counter() - transcribe_started
        log.info(
            "transcribed %.2fs of audio in %.2fs: %r", clip_seconds, transcribe_seconds, raw
        )

        text = speech.sanitize(raw, profile.max_chars)

        # Before mentions, before standings, before anything is typed: if that
        # was an instruction to the engineer it was never a message, and the
        # chat box has to be sent away rather than filled in. Deliberately on
        # the raw words — a command run through name matching would have its
        # own trigger phrase rewritten into somebody's driver name.
        if text and self._for_engineer(text):
            log.info("that was for the engineer; not sending it to the chat box")
            self._abort(active, profile, text, transcribe_seconds)
            return

        standings = active.get("standings")
        if text and standings and cfg.mentions.enabled:
            # Before name matching: "P3" is unambiguous, and resolving it first
            # means the resulting name is not re-matched.
            spoken = mentions_mod.apply_positions(
                text, standings.overall, classes=standings.by_class,
                prefix=cfg.mentions.prefix)
            if spoken != text:
                log.info("resolved standings: %r", spoken)
                text = spoken

        if text and drivers and cfg.mentions.enabled:
            marked = mentions_mod.apply_mentions(
                text, drivers,
                prefix=cfg.mentions.prefix,
                fuzzy=cfg.mentions.fuzzy,
                threshold=cfg.mentions.threshold,
                first_names=cfg.mentions.match_first_names,
            )
            if marked != text:
                log.info("marked up driver names: %r", marked)
                # Re-truncate: the prefixes made it longer, and max_chars is
                # the game's limit, not a suggestion.
                text = speech.sanitize(marked, profile.max_chars)

        if not text:
            log.info("nothing was said; firing abort keys instead of sending")
            self._abort(active, profile, "", transcribe_seconds)
            return

        self.state.set_status(state_mod.STATUS_TYPING)
        inject.type_text(text, profile.type_delay_ms, profile.text_mode)
        _sleep_ms(profile.post_delay_ms)

        if profile.auto_send:
            inject.send_keys(profile.post_keys, profile.key_hold_ms, profile.key_gap_ms)
        else:
            # Left in the chat box for the driver to read. Say what the trigger
            # now does, because otherwise a message that never arrives looks
            # exactly like a failure.
            self._pending = {"text": text, "profile": profile, "active": active}
            self._gestures = gestures_mod.Gestures(
                cfg.review.tap_ms, cfg.review.double_tap_ms)
            log.info(
                "auto-send is off: tap the trigger to send, tap twice to clear, "
                "hold to re-record"
            )

        total = time.perf_counter() - active["started"]
        log.info("%s %d chars in %.2fs total",
                 "sent" if profile.auto_send else "typed", len(text), total)

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

    def _for_engineer(self, text: str) -> bool:
        """Whether the engineer took those words as an instruction.

        Wrapped whole. This sits between a transcription and somebody's message
        reaching the chat box, and it is the one thing in the cycle that can
        *swallow* a message rather than merely fail to decorate it — so a fault
        in it must mean "not a command" and let the words through. Losing a
        message to a crash in an optional feature would be the worst bug this
        file could have.
        """
        if self.engineer is None:
            return False
        try:
            return bool(self.engineer.handle(text))
        except Exception:
            log.exception("the engineer failed on %r; sending it as a message", text)
            return False

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
        self._settle()

    def _settle(self) -> None:
        self._cycle_ended = time.monotonic()
        if self._pending is not None:
            # Not idle: the trigger means something different until this is
            # dealt with, and the status bar is where that is visible.
            self.state.set_status(state_mod.STATUS_REVIEW)
            return
        self.state.set_status(
            state_mod.STATUS_IDLE if self.state.enabled else state_mod.STATUS_DISABLED
        )

    def _abandon(self) -> None:
        """Best-effort cleanup after an unexpected failure mid-cycle."""
        self._active = None
        self._pending = None
        self._gestures.reset()
        self._discard_recording()
        self._settle()

    # -- a message waiting to be sent ------------------------------------

    def _pending_up(self, released_at: float) -> None:
        action = self._gestures.release(released_at)
        if action == gestures_mod.RETRY:
            self._retry(released_at)
        elif action == gestures_mod.CLEAR:
            self._clear_pending()
        else:
            # A lone tap so far. Nothing happens until the double-tap window
            # closes, so the speculative recording is of no further use.
            self._discard_recording()

    def _on_gesture_elapsed(self) -> None:
        """The double-tap window closed on a lone tap, so it was a send."""
        if self._gestures.elapsed(time.monotonic()) != gestures_mod.SEND:
            return
        self._send_pending()

    def _on_action(self, kind: str) -> None:
        """A dedicated send or clear binding fired.

        Acts immediately — there is no window to wait out, which is the whole
        reason to bind a button for it rather than counting taps.
        """
        if self._pending is None:
            log.info("%s pressed, but no message is waiting to be sent", kind)
            return
        self._gestures.reset()
        self._discard_recording()
        if kind == hook_mod.TRIGGER_SEND:
            self._send_pending()
        else:
            self._clear_pending()

    def _send_pending(self) -> None:
        if self._pending is None:
            return
        pending, self._pending = self._pending, None
        profile: Profile = pending["profile"]
        inject.send_keys(profile.post_keys, profile.key_hold_ms, profile.key_gap_ms)
        log.info("sent the waiting message (%d chars)", len(pending["text"]))
        self._settle()

    def _clear_pending(self) -> None:
        pending, self._pending = self._pending, None
        self._gestures.reset()
        self._discard_recording()
        profile: Profile = pending["profile"]
        inject.send_keys(profile.abort_keys, profile.key_hold_ms, profile.key_gap_ms)
        log.info("cleared the waiting message without sending it")
        self._settle()

    def _retry(self, released_at: float) -> None:
        """Throw the waiting message away and use what was just recorded.

        The recording has been running since the button went down, so nothing
        said during the hold is lost. The chat box is cleared and reopened
        before the replacement is typed into it.
        """
        pending, self._pending = self._pending, None
        self._gestures.reset()

        profile: Profile = pending["profile"]
        log.info("re-recording: clearing the waiting message first")
        inject.send_keys(profile.abort_keys, profile.key_hold_ms, profile.key_gap_ms)
        inject.send_keys(profile.pre_keys, profile.key_hold_ms, profile.key_gap_ms)
        _sleep_ms(profile.pre_delay_ms)

        # Same session context as the message being replaced — the drivers in
        # the session have not changed in the seconds since.
        active = dict(pending["active"])
        active["started"] = time.perf_counter()
        self._active = active
        self._on_up(released_at)

    def _discard_recording(self) -> None:
        try:
            if self.recorder.active:
                self.recorder.stop()
        except Exception:
            log.debug("recorder cleanup failed", exc_info=True)

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
        if profile.auto_send:
            inject.send_keys(profile.post_keys, profile.key_hold_ms, profile.key_gap_ms)
        else:
            log.info("auto-send is off; the message is waiting in the chat box")

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
                from pitradio import keys

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
