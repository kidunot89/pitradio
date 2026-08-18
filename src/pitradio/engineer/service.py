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
    phrases,
    queries,
    routines,
    sectors,
    speaking,
    spotter,
    tts,
)
from pitradio.engineer import (
    fuel as fuel_mod,
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
            lambda: self.store.config, host=self._host)

        self.book = coaching.LapBook()
        self.sectors = sectors.SectorBook()
        #: What the car burns a lap, learned from the tank. Fed on the same
        #: tick as the books, so a fuel answer and a lap answer can never come
        #: from two different moments of the session.
        self.fuel = fuel_mod.Usage()
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
        #: Set whenever the voice is resolved, so the short
        #: urgent phrases are rendered once per voice change.
        self._prime_urgent = True
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
        """The name it answers to."""
        return (self.config.name or "").strip() or "Chief"

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
        """Resolve the voice pack and the fallback voice, if either changed.

        Signature-guarded because resolving means listing a folder of
        thousands of clips and starting a PowerShell host, and doing that ten
        times a second would be absurd.
        """
        cfg = self.config
        language = self._language_for()
        signature = (cfg.voice_pack, cfg.fallback_voice, cfg.rate, cfg.terse,
                     language)
        if not force and signature == self._voice_signature:
            return
        self._voice_signature = signature

        if language != self._language:
            self._language = language
            self.catalogue = i18n.Catalogue.for_setting(language)
            log.info("engineer language: %s", self.catalogue.code)
        self.script = lines.Script(self.catalogue, terse=bool(cfg.terse))

        pack = None
        if cfg.voice_pack:
            pack = packs.find(paths.voice_pack_dir(), cfg.voice_pack)
            if pack is None:
                log.warning("no voice pack called %r in %s; every word will "
                            "come from the synthesiser",
                            cfg.voice_pack, paths.voice_pack_dir())
            else:
                log.info("engineer voice: %s (%d phrase(s) recorded)",
                         pack.name, len(pack.clips))
        else:
            log.info("engineer has no voice pack; using the Windows voice")

        rate = tts.DEFAULT_RATE if cfg.rate is None else int(cfg.rate)
        voice = (cfg.fallback_voice or "").strip()
        self._prime_urgent = True
        self.speaker.configure(speaking.VoiceSettings(voice, rate, pack))
        if self._prime_urgent:
            # The spotter's whole vocabulary is a handful of two-word calls, and
            # they are the ones that must not wait on a synthesiser. Rendered
            # here, on the thread that resolved the voice, rather than when a
            # car is already alongside.
            self._prime_urgent = False
            self.speaker.prime(self.script.urgent_phrases())

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

    def _query_entries(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(query id, the phrases that ask it), translated.

        Questions go into the same matcher as the routines, so one pass over
        the sentence decides everything and a routine can never be shadowed by
        a question or the other way round. The two are kept apart by id, which
        `_dispatch` checks before it looks for a routine.

        The argument-taking ones carry a `{argument}` placeholder, which is
        what confines them to the addressed path — an argument has no end, so
        an unaddressed "fastest sector three in GT3" would otherwise swallow
        any sentence starting with those words. See `phrases.MIN_BARE_WORDS`.

        **A question that is switched off contributes no phrases at all**, so
        it is not merely silent — the matcher never sees those words and they
        reach the chat box like any others. Silencing it downstream would leave
        "who has the fastest lap" being taken out of a message and then
        answered with nothing, which is the worst of both.
        """
        configured = self.config.questions or {}
        entries: list[tuple[str, tuple[str, ...]]] = []
        for query_id, spoken in queries.DEFAULT_PHRASES.items():
            settings = configured.get(query_id)
            if settings is not None and not settings.enabled:
                continue
            suffix = " {argument}" if query_id in queries.TAKES_ARGUMENT else ""
            entries.append((query_id, tuple(
                self.catalogue.translate(phrase) + suffix for phrase in spoken)))
        return tuple(entries)

    def _answer_fuel(self, command: phrases.Command, context) -> None:
        """What to fill the tank to for a stop this many laps away.

        The laps that matter are the ones *after* the stop — what is in the
        tank now covers the ones before it — so this never needs to read the
        current level, only what the car burns and how much race is left.
        """
        laps = queries.pit_in(command.argument)
        own = context.own_car()
        if laps is None or own is None:
            self.say(self.script.no_fuel_data_yet(), urgent=True)
            return

        session = context.session
        remaining = fuel_mod.laps_left(
            laps_done=own.laps, max_laps=session.max_laps,
            elapsed=session.elapsed, ends_at=session.ends_at,
            lap_time=self._reference_lap(own))
        need = fuel_mod.needed(
            remaining=remaining, pit_in=laps,
            per_lap=self.fuel.per_lap, capacity=own.fuel_capacity)
        if need is None:
            self.say(self.script.no_fuel_data_yet(), urgent=True)
            return
        if need.capped:
            self.say(self.script.fuel_will_not_reach(), urgent=True)
            return
        self.say(self.script.fuel_answer(need.percent, need.laps), urgent=True)

    def _reference_lap(self, own) -> float:
        """A lap time to divide the remaining clock by, in a timed race.

        The driver's own best, because the question is how many laps *they*
        will get through — not the leader, who may be in a faster class and
        would make the answer short by a lap on an endurance grid. Their last
        lap is the fallback while no best exists yet.
        """
        best = self.book.best_for(own.driver)
        if best is not None and best.lap_time > 0:
            return best.lap_time
        return float(getattr(own, "last_lap", 0.0) or 0.0)

    def _query_is_real(self, command: phrases.Command) -> bool:
        """Whether an unaddressed question was really one.

        Addressed, it certainly was: somebody who said the engineer's name was
        talking to it, and "who has the fastest lap in LMP1" at a race with no
        LMP1 entry deserves an answer saying so rather than being typed into
        the chat box.

        Unaddressed, the argument has to make sense on its own — see
        `queries.understood`.
        """
        if command.routine not in queries.DEFAULT_PHRASES or command.addressed:
            return True
        if command.routine == queries.FUEL_TO_FINISH:
            # Its argument is when the stop is, not a class — so "how much fuel
            # do I need to get through this stint on these tyres" is not a
            # question this can answer and belongs in the chat box.
            return queries.pit_in(command.argument) is not None
        classes = {car.vehicle_class for car in self._context().session.cars
                   if car.vehicle_class}
        return queries.understood(queries.parse(command.argument, classes))

    def _answer(self, command: phrases.Command) -> None:
        """One question, one answer, and nothing left running.

        Everything here is read out of the books the notifications already
        keep, so asking costs a lookup rather than a second pass over the
        session.
        """
        context = self._context()
        classes = {car.vehicle_class for car in context.session.cars
                   if car.vehicle_class}
        ask = queries.parse(command.argument, classes)
        if ask.unknown_class:
            self.say(self.script.no_such_class(), urgent=True)
            return

        # No class named means the driver's own, which is what they meant:
        # somebody asking "who has the fastest lap" from a GT3 car is asking
        # about the race they are in. `own_class_only` off means they have
        # already said they want the overall picture.
        wanted = ask.vehicle_class or context.my_class()

        if command.routine == queries.FUEL_TO_FINISH:
            self._answer_fuel(command, context)
            return

        if command.routine == queries.MY_BEST_LAP:
            own = context.own_car()
            best = context.book.best_for(own.driver) if own else None
            if best is None:
                self.say(self.script.no_time_yet(), urgent=True)
                return
            self.say(self.script.best_lap_answer(best.lap_time), urgent=True)
            return

        if command.routine == queries.FASTEST_SECTOR:
            sector = ask.sector
            if not sector:
                self.say(self.script.which_sector(), urgent=True)
                return
            held = context.sectors.fastest(sector, wanted)
            if held is None:
                self.say(self.script.no_time_yet(wanted), urgent=True)
                return
            driver, seconds = held
            self.say(self.script.fastest_sector_answer(
                driver, sector, seconds, vehicle_class=wanted), urgent=True)
            return

        fastest = context.book.fastest(wanted)
        if fastest is None or fastest.lap_time <= 0:
            self.say(self.script.no_time_yet(wanted), urgent=True)
            return
        self.say(self.script.fastest_lap_answer(
            fastest.driver, fastest.lap_time, vehicle_class=wanted), urgent=True)

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
                entries=self._entries() + self._query_entries(),
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

        if not self._query_is_real(command):
            # It began like a question and its argument was not one. Better in
            # the chat box than answered as though it had been asked.
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

        if command.routine in queries.DEFAULT_PHRASES:
            # A question, not a routine: it has an answer and when the answer
            # has been given there is nothing running.
            self._answer(command)
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
        return self._context_for(current, plugin_id, standings)

    def _context_for(self, session, plugin_id, standings, **extra) -> routines.Context:
        """One place that builds a Context, so every field is set the same way.

        There were two, and they had already drifted: the spotter's range and
        width were read in one and defaulted in the other, so the same car
        counted as alongside or not depending on which path built the tick.
        """
        settings = self._plugin_settings(plugin_id)
        # Every spotter distance from two numbers, so they cannot drift apart:
        # a car that is alongside, one that has gone clear, one on the same
        # line and one on another part of the circuit are all statements about
        # how big the cars are. See `spotter.ranges`.
        ranges = spotter.ranges(
            float(settings.get("spotter_car_length") or spotter.DEFAULT_CAR_LENGTH),
            float(settings.get("spotter_car_width") or spotter.DEFAULT_CAR_WIDTH))
        return routines.Context(
            script=self.script, book=self.book, sectors=self.sectors,
            session=session, standings=standings or Standings(),
            swap_sides=bool(settings.get("spotter_swap_sides")),
            alongside_metres=ranges["metres"],
            width_metres=ranges["width"],
            overlap_metres=ranges["overlap"],
            min_lateral_metres=ranges["min_lateral"],
            own_class_only=bool(self.config.own_class_only),
            threshold=float(self.config.coach_threshold),
            sector_threshold=float(self.config.sector_threshold),
            say=self.say, **extra)

    def _plugin_settings(self, plugin_id: str) -> dict:
        """This sim's plugin settings, with the profile's overrides applied.

        The engineer's per-sim knobs live here rather than in its own config
        because they describe the *game*, not the driver: which way round the
        axes are, and how long the cars are. Swallows everything — a sim that
        has just closed must cost the override, not the tick.
        """
        if not plugin_id or self.plugins is None:
            return {}
        try:
            return self.plugins.settings_for(
                plugin_id,
                self.store.config.profile_for_plugin(plugin_id).plugin_settings)
        except Exception:
            log.debug("could not read the plugin's engineer settings", exc_info=True)
            return {}

    def _provides(self, plugin_id: str) -> frozenset[str] | None:
        """What this sim can supply, or None for "nobody said".

        None rather than an empty set when there is no plugin at all: empty
        means "this sim has nothing", which would switch every behaviour off,
        and that is the wrong answer for a registry that simply has not been
        asked yet.
        """
        if not plugin_id or self.plugins is None:
            return None
        try:
            return self.plugins.provides_for(plugin_id)
        except Exception:
            log.debug("could not read what the plugin provides", exc_info=True)
            return None

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
        provided = self._provides(plugin_id)

        # The always-on behaviours, then whatever routine is running. Both go
        # through the same runner with the same repeat rules; the only
        # difference is where their settings come from.
        for call in self.behaviours.run(
            context, now, notifications.Settings.from_config(self.config), provided
        ):
            self.say(call.utterance, urgent=call.urgent)

        if self.active is None:
            return
        try:
            for call in self.active.tick(context, now, provided):
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
        own = session.player()
        if own is not None and own.fuel_capacity:
            self.fuel.observe(own.laps, own.fuel)
        for car in session.cars:
            lap = self.book.observe(car, session.elapsed)
            if lap is not None and car.is_player:
                finished_lap = lap
            split = self.sectors.observe(car)
            if split is not None:
                finished_sectors.append(split)

        standings = self.plugins.standings_for(plugin_id) if plugin_id else None
        return self._context_for(
            session, plugin_id, standings,
            finished_lap=finished_lap,
            finished_sectors=tuple(finished_sectors))

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
