"""The engineer itself: one thread, watching the sim and deciding what to say.

Its own thread, and not one of the four that already exist, for the reasons
each of those cannot take it: the hook must return before Windows unregisters
it, the worker is holding somebody's trigger down, and the GUI is the only
thread allowed to touch a widget. Speaking is slower still and gets a thread of
its own below this one — see [speaking.py](speaking.py).

**Everything it does is a detail that is allowed to fail.** The engineer going
quiet must never cost a trigger, a transcription or a message in the chat box,
so every path here ends in a log line. That is not defensive coding for its own
sake: this reads a sim's shared memory several times a second while somebody
races, and a game update that moves a field must cost the commentary.

The one place it reaches into the rest of the app is `handle`, which the worker
calls with a fresh transcription. That returns whether the words were a command
— and when they were, the worker sends the chat box away instead of typing
them. It is a synchronous, pure match followed by queued work, because the
worker is waiting on it with a driver's finger still on the button.
"""

from __future__ import annotations

import logging
import threading
import time

from pitradio import i18n, paths
from pitradio.engineer import (
    coaching,
    lines,
    notifications,
    packs,
    personas,
    phrases,
    routines,
    sectors,
    speaking,
    tts,
)
from pitradio.plugins.base import SessionInfo, Standings

log = logging.getLogger(__name__)

#: How often the sim is read. Fast enough that a corner is not missed at
#: 300km/h — 10Hz is a sample every 8 metres — and slow enough to be free. The
#: scoring block itself only updates a few times a second, so asking faster
#: would return the same numbers.
POLL_SECONDS = 0.1

#: How long to keep quiet after a spotter call before making the same one
#: again. Without it a car sitting alongside through a long corner is announced
#: ten times a second.
SPOTTER_REPEAT = 3.0


class EngineerService:
    """A named voice with opinions, and the thread that gives it something to say.

    Built even when the engineer is switched off: the config is read on every
    loop, so turning it on in the window takes effect without a restart, and
    while it is off the loop does nothing but sleep.
    """

    def __init__(self, store, plugins, *, speaker=None, host=None,
                 clock=time.monotonic) -> None:
        self.store = store
        self.plugins = plugins
        # Injected, because every repeat interval in the engineer is measured
        # against it. A test that had to sleep three seconds to prove the
        # spotter repeats would be a test nobody runs.
        self._clock = clock
        self._host = host if host is not None else tts.SapiHost()
        self.speaker = speaker if speaker is not None else speaking.Speaker(
            lambda: self.store.config.engineer, host=self._host)

        self.book = coaching.LapBook()
        self.sectors = sectors.SectorBook()
        self.routines = {routine.id: routine for routine in routines.build()}
        self.active: routines.Routine | None = None
        #: The always-on behaviours. A routine brings its own runner, so the
        #: two never share a repeat history and stopping a routine cannot make
        #: a behaviour go quiet.
        self.behaviours = notifications.Runner(notifications.build())

        self.catalogue = i18n.Catalogue()
        self.script = lines.Script(self.catalogue)
        #: What the voice was last resolved from, so Windows is only asked what
        #: it has installed when something relevant actually changed.
        self._voice_signature: tuple | None = None
        self._language = ""
        self._name = ""

        self._track = ""
        self._laps: dict[str, int] = {}
        self._previous_position: tuple[float, float, float] | None = None
        self._spotter_call: str | None = None
        self._spotter_at = 0.0

        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self.speaker.start()
        self._thread = threading.Thread(target=self._run, name="engineer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self.speaker.stop()
        except Exception:
            log.debug("stopping the engineer's speech failed", exc_info=True)

    # -- what it is called, and what it sounds like ------------------------

    @property
    def config(self):
        return self.store.config.engineer

    def display_name(self) -> str:
        """The name it answers to: the configured one, or the persona's."""
        configured = (self.config.name or "").strip()
        return configured or personas.by_id(self.config.persona).name

    def _language_for(self) -> str:
        """Which language it speaks.

        Follows the transcription language unless pinned, because the commands
        arrive through Whisper: an engineer listening for English phrases while
        Whisper produces Spanish would never hear a single one, and nothing
        about that failure points at the language setting.
        """
        pinned = (self.config.language or "").strip()
        return pinned or (self.store.config.whisper.language or "").strip() or "en"

    def refresh_voice(self, *, force: bool = False) -> None:
        """Resolve the persona, the voice and the pack, if anything changed.

        Signature-guarded because resolving means starting a PowerShell host to
        ask Windows what is installed, and doing that ten times a second would
        be absurd.
        """
        cfg = self.config
        language = self._language_for()
        persona = personas.by_id(cfg.persona)
        signature = (cfg.persona, cfg.voice, cfg.voice_pack, cfg.rate, language)
        if not force and signature == self._voice_signature:
            return
        self._voice_signature = signature

        if language != self._language:
            self._language = language
            self.catalogue = i18n.Catalogue.for_setting(language)
            self.script = lines.Script(self.catalogue, terse=persona.terse)
            log.info("engineer language: %s", self.catalogue.code)
        else:
            self.script = lines.Script(self.catalogue, terse=persona.terse)

        pack = None
        if cfg.voice_pack:
            pack = packs.find(paths.voice_pack_dir(), cfg.voice_pack)
            if pack is None:
                log.warning("no voice pack called %r in %s; using the synthesiser",
                            cfg.voice_pack, paths.voice_pack_dir())
            else:
                log.info("engineer voice pack: %s (%d phrase(s))",
                         pack.name, len(pack.clips))

        voice = cfg.voice.strip()
        if not voice:
            voice = personas.pick_voice(persona, self._host.voices(), language)
            if voice:
                log.info("engineer voice: %s (%s)", voice, persona.name)

        rate = persona.rate if cfg.rate is None else int(cfg.rate)
        self.speaker.configure(speaking.VoiceSettings(voice, rate, pack))

    def say(self, utterance: list[str], *, urgent: bool = False) -> None:
        self.speaker.say(utterance, urgent=urgent)

    def say_test(self) -> None:
        """Speak a sample line. What the window's Test button calls.

        Forces a re-resolve first, so pressing Test after changing the voice
        demonstrates the new one rather than whatever was resolved last.
        """
        try:
            self.refresh_voice(force=True)
            name = self.display_name()
            self.say([name, self.script.t("radio check")])
        except Exception:
            log.exception("the engineer's test line failed")

    # -- commands ---------------------------------------------------------

    def _entries(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(routine id, trigger phrases) for everything switched on.

        A routine's configured phrases win outright over its defaults rather
        than adding to them. Someone who has typed their own phrase in has
        decided what starts it, and leaving the shipped ones live would mean a
        phrase they did not choose still fires.
        """
        return self._phrase_entries("phrases", "phrases")

    def _end_entries(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(routine id, the phrases that stand it down)."""
        return self._phrase_entries("end_phrases", "end_phrases")

    def _phrase_entries(
        self, configured_field: str, routine_field: str
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        configured = self.config.routines or {}
        entries: list[tuple[str, tuple[str, ...]]] = []
        for routine in self.routines.values():
            settings = configured.get(routine.id)
            if settings is not None and not settings.enabled:
                continue
            chosen = tuple(
                phrase for phrase
                in (getattr(settings, configured_field, None) or [])
                if phrase.strip())
            if not chosen:
                chosen = phrases.default_phrases(
                    self.catalogue,
                    ((routine.id, getattr(routine, routine_field)),))[0][1]
            if chosen:
                entries.append((routine.id, chosen))
        return tuple(entries)

    def handle(self, text: str) -> bool:
        """Was that for the engineer? Called by the worker before it types.

        Returns True when the words were a command and have been dealt with,
        which is the worker's signal to close the chat box rather than send
        them. False means it was an ordinary message and nothing here has
        touched it.
        """
        if not self.config.enabled:
            return False
        try:
            command = phrases.match_command(
                text,
                name=self.display_name(),
                entries=self._entries(),
                end_entries=self._end_entries(),
                stop_phrases=tuple(
                    self.catalogue.translate(phrase)
                    for phrase in phrases.STOP_PHRASES),
            )
        except Exception:
            log.exception("matching %r against the engineer failed", text)
            return False

        if command is None:
            return False

        log.info("engineer: %r -> %s%s", text, command.routine,
                 f" {command.argument!r}" if command.argument else "")
        try:
            self._dispatch(command)
        except Exception:
            log.exception("running the %s command failed", command.routine)
        return True

    def _dispatch(self, command: phrases.Command) -> None:
        self.refresh_voice()

        if command.routine == phrases.ACKNOWLEDGE:
            self.say(self.script.acknowledge(), urgent=True)
            return

        if command.routine == phrases.STOP:
            self._stop_active()
            return

        routine = self.routines.get(command.routine)
        if routine is None:
            self.say(self.script.not_understood())
            return

        if command.ending:
            # A routine's own end phrase. Only stops it if it is the one
            # running: "end sector trainer" while the hot lap trainer is going
            # is somebody misremembering which they started, and silently
            # stopping the other one would be worse than saying nothing.
            if self.active is routine:
                self._stop_active()
            else:
                self.say(self.script.stopped(), urgent=True)
            return

        # Starting one stops whatever was running. Two routines commenting on
        # the same lap is the thing that makes people switch this off.
        if self.active is not None and self.active is not routine:
            self._stop_active(quiet=True)

        self.speaker.clear()
        # Set before start(), so a routine that speaks during it is already the
        # active one — and cleared again if it declines, so a failed start does
        # not leave a routine that never began looking like it is running.
        self.active = routine
        try:
            started = routine.start(self._context(), command.argument)
        except Exception:
            log.exception("starting %s failed", routine.id)
            started = False
        if not started:
            self.active = None

    def _stop_active(self, *, quiet: bool = False) -> None:
        routine, self.active = self.active, None
        # Cleared whether or not anything was running: the stop phrase is what
        # a driver says when the engineer will not shut up, and the queue is
        # most of what it is still going to say.
        self.speaker.clear()
        if routine is None:
            return
        try:
            routine.stop(self._context())
        except Exception:
            log.exception("stopping %s failed", routine.id)
        if not quiet:
            self.say(self.script.stopped(), urgent=True)

    def _context(self, session: SessionInfo | None = None) -> routines.Context:
        current = session if session is not None else SessionInfo()
        plugin_id = ""
        if session is None and self.plugins is not None:
            plugin_id, current = self.plugins.any_telemetry()
        standings = (self.plugins.standings_for(plugin_id)
                     if self.plugins is not None and plugin_id else None)
        context = routines.Context(
            script=self.script, book=self.book, sectors=self.sectors,
            session=current, standings=standings or Standings(),
            swap_sides=self._swap_sides(plugin_id),
            threshold=float(self.config.coach_threshold),
            sector_threshold=float(self.config.sector_threshold),
            say=self.say)
        return context

    def _swap_sides(self, plugin_id: str) -> bool:
        """Whether this sim's axes need the spotter's sides flipped.

        Per-sim, so it lives on the plugin's settings rather than in the
        engineer's config — see `engineer/spotter.py` for why it is a setting
        at all.
        """
        if not plugin_id or self.plugins is None:
            return False
        try:
            settings = self.plugins.settings_for(
                plugin_id,
                self.store.config.profile_for_plugin(plugin_id).plugin_settings)
        except Exception:
            log.debug("could not read the plugin's spotter setting", exc_info=True)
            return False
        return bool(settings.get("spotter_swap_sides"))

    # -- the loop ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._tick()
            except Exception:
                # One bad read must not end the thread; the sim may be mid-load
                # and perfectly fine a tenth of a second later.
                log.exception("the engineer's poll failed")
            self._stopping.wait(POLL_SECONDS)

    def _tick(self) -> None:
        if not self.config.enabled or self.plugins is None:
            return

        self.refresh_voice()
        plugin_id, session = self.plugins.any_telemetry()
        if not session.has_data:
            return

        self._maybe_new_session(session)
        context = self._observe(session, plugin_id)
        now = self._clock()

        # The always-on behaviours, then whatever routine is running. Both go
        # through the same runner with the same repeat rules; the only
        # difference is where their settings come from.
        for call in self.behaviours.run(
            context, now, notifications.Settings.from_config(self.config)
        ):
            self.say(call.utterance, urgent=call.urgent)

        if self.active is None:
            return
        try:
            for call in self.active.tick(context, now):
                self.say(call.utterance, urgent=call.urgent)
            if self.active.finished():
                self._stop_active()
        except Exception:
            log.exception("routine %s failed; standing it down", self.active.id)
            self._stop_active(quiet=True)

    def _maybe_new_session(self, session: SessionInfo) -> None:
        """Throw everything away when the track changes.

        Lap traces are distances into a particular circuit, and so are sector
        boundaries. Carrying them from one to the next would compare Le Mans
        against Sebring with a straight face, and nothing about the result
        would look wrong.
        """
        track = session.track or ""
        if track == self._track:
            return
        if self._track:
            log.info("engineer: track changed to %r; clearing lap data", track)
        self._track = track
        self.book.reset(session.track_length)
        self.sectors.reset()
        self.behaviours.reset()
        if self.active is not None:
            self._stop_active(quiet=True)

    def _observe(self, session: SessionInfo, plugin_id: str) -> routines.Context:
        """Feed the sim's frame into the books and build this tick's context.

        Every car goes in, not just the player's: the fastest lap and the
        fastest sector are somebody else's most of the time, and a target's
        reference lap only exists because their laps were recorded too.
        """
        if session.track_length and not self.book.track_length:
            self.book.track_length = session.track_length

        finished_lap = None
        finished_sectors = []
        for car in session.cars:
            lap = self.book.observe(car, session.elapsed)
            if lap is not None and car.is_player:
                finished_lap = lap
            split = self.sectors.observe(car)
            if split is not None:
                finished_sectors.append(split)

        standings = self.plugins.standings_for(plugin_id) if plugin_id else None
        return routines.Context(
            script=self.script, book=self.book, sectors=self.sectors,
            session=session, standings=standings or Standings(),
            finished_lap=finished_lap,
            finished_sectors=tuple(finished_sectors),
            swap_sides=self._swap_sides(plugin_id),
            threshold=float(self.config.coach_threshold),
            sector_threshold=float(self.config.sector_threshold),
            say=self.say)

    # -- for the window ----------------------------------------------------

    def status(self) -> str:
        """A line for the Engineer tab, so it is visibly doing something."""
        if not self.config.enabled:
            return "off"
        if self.active is None:
            return f"listening as {self.display_name()}"
        state = self.active.running_state()
        return f"{self.active.name}: {state}" if state else f"{self.active.name} running"

    def known_drivers(self) -> list[str]:
        """Who has a lap on record, for the tab's target list."""
        return sorted(self.book.best.keys())
