#!/usr/bin/env python3
"""PitRadio — push-to-talk dictation for sim racing.

Entry point. Sets up logging, loads config, starts the four threads and hands
the main thread to tkinter.

Thread layout:
    main    tkinter mainloop; the only thread allowed to touch widgets
    hook    WH_KEYBOARD_LL plus the GetMessageW pump it cannot live without
    worker  audio, transcription and key injection
    tray    pystray's own blocking loop

winapi is imported lazily rather than at module scope, so `--check-config` and
`--gui-only` still run on a non-Windows development machine.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import logging.handlers
import queue
import shutil
import sys
import threading
import time

from pitradio import __version__, paths
from pitradio import config as config_mod
from pitradio import state as state_mod
from pitradio.state import AppState

log = logging.getLogger("pitradio")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
GUI_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
TIME_FORMAT = "%H:%M:%S"


# -- setup ---------------------------------------------------------------


def setup_logging(app_state: AppState | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    # A GUI-subsystem build launched by double-click has no stdout at all, and
    # StreamHandler(None) would blow up on the first log record.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
        root.addHandler(console)

    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            paths.log_dir() / "pitradio.log",
            maxBytes=1_048_576, backupCount=3, encoding="utf-8",
        )
        rotating.setFormatter(logging.Formatter(LOG_FORMAT, TIME_FORMAT))
        root.addHandler(rotating)
    except OSError as exc:
        print(f"warning: file logging unavailable: {exc}", file=sys.stderr)

    if app_state is not None:
        gui_handler = state_mod.QueueLogHandler(app_state)
        gui_handler.setFormatter(logging.Formatter(GUI_LOG_FORMAT, TIME_FORMAT))
        root.addHandler(gui_handler)

    # The point of this log is the per-trigger stage timings — when the chat box
    # opened, how long transcription took, when the message went. The model
    # download alone emits about twenty httpx request lines, which buries them.
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_crash_logging()


def _install_crash_logging() -> None:
    """Make unhandled exceptions land in the log.

    A GUI-subsystem build launched from a shortcut has no console, so Python's
    default handler writes a traceback to a stderr that does not exist and the
    process simply disappears. Every startup crash through 0.1.2 was invisible
    for exactly that reason — the log stopped mid-startup with no indication
    why.
    """

    def on_exception(exc_type, exc_value, tb) -> None:
        log.critical("unhandled exception", exc_info=(exc_type, exc_value, tb))

    def on_thread_exception(args) -> None:
        name = args.thread.name if args.thread is not None else "?"
        log.critical(
            "unhandled exception in thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception


def seed_config() -> None:
    """First run of an installed build has no config yet; copy the shipped one."""
    target = paths.config_path()
    if target.exists():
        return
    source = paths.default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, target)
        log.info("created %s from the bundled defaults", target)
    except OSError as exc:
        log.warning("could not seed %s from %s: %s", target, source, exc)
        return

    _seed_language(target)


def _seed_language(path) -> None:
    """Point a brand-new config at the desktop's own language.

    Only on the run that creates the file. Deriving it every startup would
    overwrite whatever the user later chose in the Language tab, and a config
    that silently reverts is worse than one that guessed wrong once.

    English is the shipped default and needs no rewrite — it also spares an
    install on an English machine a pointless save.
    """
    from pitradio import languages as languages_mod

    try:
        code = languages_mod.system_language()
        if code == "en":
            return

        cfg = config_mod.load(path)
        size = cfg.whisper.languages.get(code, languages_mod.DEFAULT_SIZE)
        cfg.whisper.language = code
        cfg.whisper.languages = {code: size}
        cfg.whisper.model = languages_mod.model_name(code, size)
        config_mod.save(path, cfg)
        log.info("system language is %s; defaulting transcription to it (model %s)",
                 languages_mod.language_name(code), cfg.whisper.model)
    except Exception as exc:
        # A bad guess must not stop the app starting on its very first run.
        log.warning("could not apply the system language: %s", exc)


# -- command-line modes --------------------------------------------------


def out(line: str = "") -> None:
    """print(), except when there is no stdout to print to.

    The installed build is a GUI binary: launched from a terminal it has a
    console, launched by double-click it has none, and print() would raise.
    """
    if sys.stdout is not None:
        print(line)
    else:
        log.info("%s", line)


def cmd_check_config() -> int:
    """Validate the config without needing Windows, audio or a model."""
    from pitradio import keys

    store = config_mod.ConfigStore(paths.config_path())
    cfg = store.load()

    out(f"config: {store.path}")
    out(f"paths:  {paths.describe()}")
    out(f"trigger key: {cfg.trigger_key}")

    out("\nprofiles:")
    for name in ["default_profile", *sorted(cfg.profiles)]:
        profile = cfg.default_profile if name == "default_profile" else cfg.profiles[name]
        resolved = []
        for spec in [*profile.pre_keys, *profile.post_keys, *profile.abort_keys]:
            try:
                mods, vk = keys.parse_combo(spec)
                resolved.append(f"{spec}->{'+'.join(map(keys.name_for, [*mods, vk]))}")
            except keys.KeyNameError:
                resolved.append(f"{spec}->INVALID")
        out(f"  {name}")
        out(f"    keys: {', '.join(resolved)}")
        out(f"    pre_delay={profile.pre_delay_ms}ms hold={profile.key_hold_ms}ms "
              f"type_delay={profile.type_delay_ms}ms max_chars={profile.max_chars} "
              f"text_mode={profile.text_mode}")

    problems = store.problems
    if problems:
        out(f"\n{len(problems)} problem(s):")
        for problem in problems:
            out(f"  - {problem}")
        return 1

    out("\nno problems found")
    return 0


def cmd_self_test() -> int:
    """Import every module the running app needs, and open a real window.

    This exists because `--check-config` and `--list-devices` both return before
    touching tkinter, the keyboard hook or the speech stack — so a packaged
    build could pass both and still be unable to transcribe a single word. That
    is not hypothetical: v0.1.0 shipped missing `av.utils`, and neither of those
    commands would ever have noticed.

    Deliberately does not construct a WhisperModel; that would download 250MB.
    Importing is enough to catch a missing or unloadable dependency.
    """
    windows_only = {
        "pitradio.input.winapi", "pitradio.input.hook", "pitradio.input.inject",
        "pitradio.worker",
    }
    modules = [
        "tkinter", "tkinter.ttk",
        "numpy", "sounddevice", "PIL", "pystray",
        # The heavy native stack. faster_whisper pulls av in eagerly, and av's
        # extension modules import av.utils from inside compiled code.
        "av", "av.utils", "ctranslate2", "onnxruntime", "faster_whisper",
        # Voice chat's transport. Imported lazily by `net`, so a build that
        # dropped it would run perfectly until somebody pressed the trigger.
        "websocket",
        # The app itself. These were still listed under their pre-restructure
        # names — "winapi", "gui", "plugins" — so every one of them reported
        # ModuleNotFoundError and the real modules were never checked at all.
        "pitradio", "pitradio.config", "pitradio.paths", "pitradio.state",
        "pitradio.keys", "pitradio.languages", "pitradio.mentions",
        "pitradio.gestures", "pitradio.i18n", "pitradio.speech",
        "pitradio.updater", "pitradio.worker",
        "pitradio.input.winapi", "pitradio.input.hook", "pitradio.input.inject",
        "pitradio.ui.gui", "pitradio.ui.gui_settings", "pitradio.ui.gui_language",
        "pitradio.ui.theme", "pitradio.ui.logo", "pitradio.ui.tray",
        "pitradio.input", "pitradio.ui",
        "pitradio.plugins", "pitradio.plugins.base", "pitradio.plugins.lmu",
        # Voice. `net` imports websocket-client lazily so a build without it
        # still runs, which means nothing at module scope proves it was
        # bundled — exactly how av.utils went missing from v0.1.0.
        "pitradio.voice", "pitradio.net", "pitradio.endpoints",
        # The engineer. Its speech host is a subprocess rather than an import,
        # so nothing here proves it can talk — but a dropped module would take
        # the whole feature out silently, which is what this catches.
        "pitradio.engineer", "pitradio.engineer.service",
        "pitradio.engineer.routines", "pitradio.engineer.notifications",
        "pitradio.engineer.coaching", "pitradio.engineer.sectors",
        "pitradio.engineer.lines", "pitradio.engineer.phrases",
        "pitradio.engineer.packs", "pitradio.engineer.personas",
        "pitradio.engineer.speaking", "pitradio.engineer.spotter",
        "pitradio.engineer.tts",
    ]

    failures: list[tuple[str, str]] = []
    for name in modules:
        if name in windows_only and sys.platform != "win32":
            out(f"  skip    {name} (Windows only)")
            continue
        try:
            importlib.import_module(name)
            out(f"  ok      {name}")
        except Exception as exc:
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            out(f"  FAILED  {name}: {type(exc).__name__}: {exc}")

    # The catalogues are data files, so a build that dropped them imports
    # perfectly and simply renders English forever, saying nothing.
    try:
        from pitradio import i18n

        if i18n.LOCALE_DIR.is_dir():
            out(f"  ok      translations ({len(i18n.available())} language(s))")
        else:
            failures.append(("translations", f"{i18n.LOCALE_DIR} is missing"))
            out(f"  FAILED  translations: {i18n.LOCALE_DIR} is missing")
    except Exception as exc:
        failures.append(("translations", f"{type(exc).__name__}: {exc}"))
        out(f"  FAILED  translations: {type(exc).__name__}: {exc}")

    # Constructing the registry is what proves plugins were bundled: they are
    # registered statically, so a build that dropped the package would import
    # `plugins` fine and simply have none.
    try:
        from pitradio import plugins as plugins_mod

        registry = plugins_mod.PluginRegistry()
        if not registry.plugins:
            failures.append(("plugins", "no plugins registered"))
            out("  FAILED  plugins: none registered")
        else:
            names = ", ".join(p.name for p in registry.plugins)
            out(f"  ok      plugins ({names})")
    except Exception as exc:
        failures.append(("plugins", f"{type(exc).__name__}: {exc}"))
        out(f"  FAILED  plugins: {type(exc).__name__}: {exc}")

    # Build the real window, not just a bare Tk root. Importing tkinter proves
    # nothing about construction: App shells out to schtasks and enumerates
    # audio devices while building its tabs, and it was that shell-out — not any
    # import — which killed every shortcut launch through 0.1.2.
    try:
        import tkinter

        from pitradio.ui import gui

        root = tkinter.Tk()
        root.withdraw()
        store = config_mod.ConfigStore(paths.config_path())
        store.load()

        # Real collaborators, not None. Every one of them is optional, and the
        # None branches skip most of the attribute access — so building with
        # None proves far less than it appears to. v0.1.4 shipped a GUI that
        # called a method its backend did not have, and this test passed
        # anyway, because it had handed App a None.
        wiring = {}
        if sys.platform == "win32":
            from pitradio import keys as keys_mod
            from pitradio.input import hook as hook_mod

            events: queue.Queue = queue.Queue()
            # Constructing these does not install the hook or start polling;
            # only their run() does, and nothing starts a thread here.
            wiring["hook"] = hook_mod.KeyboardHook(
                keys_mod.VK["f13"], events, lambda: True)

        gui.App(root, store, AppState(), __version__, use_tray=False, **wiring)
        root.update()
        root.destroy()
        out("  ok      GUI construction")
    except Exception as exc:
        failures.append(("GUI construction", f"{type(exc).__name__}: {exc}"))
        out(f"  FAILED  GUI construction: {type(exc).__name__}: {exc}")

    if failures:
        out(f"\n{len(failures)} component(s) failed:")
        for name, detail in failures:
            out(f"  - {name}: {detail}")
        return 1

    out("\nall components loaded")
    return 0


def cmd_list_devices() -> int:
    from pitradio import speech

    for kind in ("input", "output"):
        out(f"{kind} devices:")
        devices = speech.list_devices(kind)
        if not devices:
            out("  (none found)")
        for index, label in devices:
            out(f"  {index}: {label}")
        out()
    return 0


def cmd_telemetry(seconds: float, interval: float) -> int:
    """Print what the connected sim is actually publishing.

    The engineer reads a dozen fields per car and every one of them is wrong in
    a way that produces no error: a lap distance that never moves, a speed
    stuck at zero, sector indices that never change, positions that are all the
    same point. Each of those makes a *behaviour* go quiet, and a quiet
    engineer looks identical whichever field is at fault.

    So this shows the numbers. Start the sim, get on track, run this, and it
    says what is arriving — which turns "the engineer does nothing" into a
    question with an answer.
    """
    import time

    from pitradio import plugins as plugins_mod
    from pitradio.plugins import base

    registry = plugins_mod.PluginRegistry()
    registry.start_all()

    out(f"plugins: {', '.join(p.name for p in registry.plugins) or '(none)'}")
    for plugin in registry.plugins:
        supplies = ", ".join(sorted(plugin.provides)) or "(nothing declared)"
        out(f"  {plugin.name}: {plugin.status()}")
        out(f"    provides: {supplies}")
    out()

    deadline = time.monotonic() + max(0.0, seconds)
    seen = False
    frames = 0
    #: What the last frame looked like. **Whether this changes is the whole
    #: check.** A block that is being published but never updated reads as a
    #: perfectly healthy session — every field plausible, the sim connected —
    #: and the engineer is silent because nothing ever moves. Printing five
    #: identical snapshots and leaving somebody to compare them by eye is not
    #: a diagnostic.
    previous: tuple | None = None
    moved = False

    while True:
        plugin_id, session = registry.any_telemetry()
        if session.has_data:
            seen = True
            frames += 1
            current = _signature(session)
            changed = previous is not None and current != previous
            moved = moved or changed
            # The first frame always, and after that only what is different.
            if previous is None or changed:
                _print_frame(plugin_id, session, registry)
            previous = current
        else:
            out("no sim publishing — is the game running and on track?")

        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.1, interval))

    registry.stop_all()
    if not seen:
        out("\nNothing was read. The engineer would be silent for the same reason.")
        return 1

    if frames > 1 and not moved:
        out(f"\nNothing changed across {frames} reads, including the sim's own "
            f"clock.\nThe block is published but is not being updated — the "
            f"game is paused, sitting\nin a menu, or the session has ended. "
            f"Get on track and run this again.\n"
            f"The engineer would be silent here, and this is why.")
        return 1

    out("\nLap distance, speed and the sim clock are all moving.")
    out("Watch the sector column change three times a lap and the engineer has "
        "everything it needs.")
    # Named so a reader knows which capability each column proves.
    out(f"positions -> spotter, laps -> lap times and the trainers, "
        f"sectors -> sector calls  ({base.PROVIDES_POSITIONS}/"
        f"{base.PROVIDES_LAPS}/{base.PROVIDES_SECTORS})")
    return 0


def _signature(session) -> tuple:
    """What has to change between reads for the sim to be live.

    The session clock is in here as well as the cars: a paused game keeps
    publishing positions that look entirely reasonable, and the clock is the
    field that gives it away.
    """
    return (
        round(session.elapsed, 3),
        tuple((car.driver, round(car.lap_dist, 2), round(car.speed, 2),
               car.laps, car.sector, round(car.position[0], 2))
              for car in session.cars),
    )


def _print_frame(plugin_id: str, session, registry) -> None:
    """One snapshot of every car, as the engineer sees it."""
    standings = registry.standings_for(plugin_id)
    out(f"[{plugin_id}] track={session.track!r} length={session.track_length:.0f}m "
        f"elapsed={session.elapsed:.1f}s cars={len(session.cars)} "
        f"key={'yes' if session.key else 'no (offline/single player)'}")
    out(f"  {'driver':<22}{'pos':>4}{'lapdist':>9}{'speed':>7}{'lap':>5}"
        f"{'sec':>4}{'last lap':>10}{'best lap':>10}  pits  world x/y/z")
    for car in session.cars:
        marker = "*" if car.is_player else " "
        x, y, z = car.position
        out(f" {marker}{car.driver[:21]:<22}{car.place:>4}{car.lap_dist:>9.1f}"
            f"{car.speed:>7.1f}{car.laps:>5}{car.sector:>4}"
            f"{car.last_lap:>10.3f}{car.best_lap:>10.3f}"
            f"{'  yes ' if car.in_pits else '   no '}"
            f"{x:>9.1f}{y:>7.1f}{z:>9.1f}")

    if standings.by_class:
        for name, order in standings.by_class.items():
            leaders = ", ".join(f"P{place} {driver}"
                                for place, driver in sorted(order.items())[:3])
            out(f"  class {name}: {leaders}")
    out()


# -- GUI-only preview ----------------------------------------------------


def run_gui_only() -> int:
    """Launch the real window with no Win32, audio or model behind it.

    The point is to be able to check layout and the event pump on a machine
    that can't run the app for real.
    """
    import tkinter as tk

    from pitradio.ui import gui

    app_state = AppState()
    setup_logging(app_state, verbose=False)
    seed_config()

    store = config_mod.ConfigStore(paths.config_path())
    store.load()
    app_state.config_problems = list(store.problems)

    root = tk.Tk()
    app = gui.App(root, store, app_state, __version__, use_tray=False)
    log.info("GUI preview mode: no keyboard hook, no audio, nothing will be typed")
    app.state.set_status(state_mod.STATUS_IDLE)
    root.mainloop()
    return 0


# -- the real thing ------------------------------------------------------


def _action_keys(cfg) -> dict:
    """Parsed send/clear keys, skipping any that no longer resolve.

    A bad name must cost that one binding, not startup — the config may have
    been hand-edited, and refusing to launch over an optional key would be a
    poor trade.
    """
    from pitradio import keys

    actions = {}
    for kind, spec in (
        (state_mod.TRIGGER_SEND, cfg.review.send_key),
        (state_mod.TRIGGER_CLEAR, cfg.review.clear_key),
    ):
        if not spec:
            continue
        try:
            mods, vk = keys.parse_trigger(spec)
        except keys.KeyNameError as exc:
            log.error("ignoring %s key %r: %s", kind, spec, exc)
            continue
        actions[kind] = (vk, mods)
        log.info("%s key: %s", kind, spec)
    return actions


def run(args) -> int:
    import tkinter as tk

    from pitradio import keys, speech, updater
    from pitradio import plugins as plugins_mod
    from pitradio import worker as worker_mod
    from pitradio.input import hook as hook_mod
    from pitradio.input import winapi
    from pitradio.ui import gui

    app_state = AppState()
    setup_logging(app_state, args.verbose)
    seed_config()
    paths.ensure_dirs()

    store = config_mod.ConfigStore(paths.config_path())
    cfg = store.load()
    app_state.config_problems = list(store.problems)
    app_state.enabled = cfg.enabled

    log.info("PitRadio %s starting", __version__)
    log.info("%s", paths.describe())
    for problem in store.problems:
        log.warning("config: %s", problem)

    app_state.elevated = winapi.is_elevated()
    if not app_state.elevated:
        log.warning(
            "not running as administrator — if a sim runs elevated, Windows will "
            "silently discard everything this app types"
        )

    try:
        trigger_mods, trigger_vk = keys.parse_trigger(cfg.trigger_key)
    except keys.KeyNameError as exc:
        log.error("%s; falling back to F13", exc)
        trigger_mods, trigger_vk = [], keys.VK["f13"]

    # Level updates arrive with every audio block; throttled here so they can't
    # crowd out log lines in the GUI's event queue.
    last_level = [0.0]

    def on_level(rms: float) -> None:
        now = time.monotonic()
        if now - last_level[0] >= 0.05:
            last_level[0] = now
            app_state.emit(state_mod.EV_LEVEL, rms)

    recorder = speech.Recorder(on_level=on_level)
    transcriber = speech.Transcriber(paths.model_dir())

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    hook = hook_mod.KeyboardHook(
        trigger_vk, events, lambda: app_state.enabled, trigger_mods)
    # Send/clear act on a message left waiting when a profile has auto-send
    # off. Armed here as well as on save, or they would only work after
    # visiting Settings once.
    hook.set_actions(_action_keys(cfg))

    registry = plugins_mod.PluginRegistry()
    registry.start_all()
    for name, status in registry.describe():
        log.info("plugin %s: %s", name, status)

    worker = worker_mod.Worker(store, app_state, events, recorder, transcriber)
    worker.hook = hook
    worker.plugins = registry

    # Built even when voice is switched off: the service reads the config on
    # every loop, so turning it on in Settings takes effect without a restart.
    # It is only ever a relay socket and a playback queue when there is a room.
    from pitradio import net as net_mod

    voice_service = net_mod.VoiceService(store, registry)
    worker.voice = voice_service

    # Built even when the engineer is switched off, for the same reason as
    # voice: the service reads the config on every poll, so turning it on in
    # the window works without a restart. Switched off it is one sleeping
    # thread.
    from pitradio.engineer import service as engineer_mod

    engineer = engineer_mod.EngineerService(store, registry)
    worker.engineer = engineer

    def is_busy() -> bool:
        """True when a sim is focused, so updates can wait."""
        try:
            _profile, matched = store.config.profile_for(winapi.foreground_exe())
        except Exception:
            return False
        return matched != "default"

    checker = None
    if not args.no_update_check:
        checker = updater.UpdateChecker(store, app_state, __version__, is_busy)

    root = tk.Tk()
    app = gui.App(
        root, store, app_state, __version__,
        worker=worker, checker=checker, recorder=recorder,
        transcriber=transcriber, hook=hook, plugins=registry,
        voice=voice_service, engineer=engineer, use_tray=not args.no_tray,
    )

    if checker is not None:
        checker.on_ready = lambda installer: root.after(
            0, lambda: app._launch_installer(installer)
        )

    worker.start()
    voice_service.start()
    engineer.start()
    hook.start()
    if not hook.wait_until_ready(5.0):
        log.error("the keyboard hook did not come up; the trigger key will do nothing")

    threading.Thread(
        target=_preload_model, args=(transcriber, store, app_state),
        name="model-preload", daemon=True,
    ).start()

    if checker is not None:
        checker.start()

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.quit()
    return 0


def _preload_model(transcriber, store, app_state: AppState) -> None:
    """Load Whisper up front so the first trigger of a session isn't slow.

    On a fresh install this also downloads ~250MB, which is why it says so
    rather than just appearing to hang.
    """
    app_state.set_status(state_mod.STATUS_LOADING)
    log.info(
        "loading the Whisper model — the first run downloads about 250MB into %s",
        paths.model_dir(),
    )
    try:
        transcriber.load(store.config.whisper)
    except Exception as exc:
        log.error("could not load the Whisper model: %s", exc)
        log.error("transcription will fail until this is resolved")
    app_state.set_status(
        state_mod.STATUS_IDLE if app_state.enabled else state_mod.STATUS_DISABLED
    )


# -- main ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pitradio",
        description="Push-to-talk dictation into sim racing chat boxes.",
    )
    parser.add_argument("--version", action="version", version=f"PitRadio {__version__}")
    parser.add_argument("--check-config", action="store_true",
                        help="validate config.json and resolve every key name, then exit")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio devices and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="load every component and exit; verifies a packaged build")
    parser.add_argument("--telemetry", nargs="?", const=10.0, type=float,
                        metavar="SECONDS",
                        help="print what the sim is publishing, for this many "
                             "seconds (default 10), then exit")
    parser.add_argument("--telemetry-interval", type=float, default=1.0,
                        metavar="SECONDS",
                        help="seconds between telemetry snapshots (default 1)")
    parser.add_argument("--gui-only", action="store_true",
                        help="open the window with no hook, audio or model (layout preview)")
    parser.add_argument("--no-tray", action="store_true",
                        help="skip the tray icon; closing the window then quits")
    parser.add_argument("--no-update-check", action="store_true",
                        help="never contact GitHub")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    if args.check_config:
        setup_logging(None, args.verbose)
        seed_config()
        return cmd_check_config()

    if args.list_devices:
        setup_logging(None, args.verbose)
        return cmd_list_devices()

    if args.telemetry is not None:
        setup_logging(None, args.verbose)
        return cmd_telemetry(args.telemetry, args.telemetry_interval)

    if args.self_test:
        setup_logging(None, args.verbose)
        return cmd_self_test()

    if args.gui_only:
        return run_gui_only()

    if sys.platform != "win32":
        print(
            "PitRadio drives the Windows input APIs and only runs on Windows.\n"
            "On another platform, --check-config, --list-devices and --gui-only "
            "still work.",
            file=sys.stderr,
        )
        return 2

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
