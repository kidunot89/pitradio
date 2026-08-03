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

import config as config_mod
import paths
import state as state_mod
from state import AppState

__version__ = "0.1.3"

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

    # ctranslate2 and huggingface are chatty at INFO and drown out the stage
    # timings this log exists to surface.
    for noisy in ("urllib3", "huggingface_hub", "filelock"):
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
    import keys

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
    windows_only = {"winapi", "hook", "inject", "worker"}
    modules = [
        "tkinter", "tkinter.ttk",
        "numpy", "sounddevice", "PIL", "pystray",
        # The heavy native stack. faster_whisper pulls av in eagerly, and av's
        # extension modules import av.utils from inside compiled code.
        "av", "av.utils", "ctranslate2", "onnxruntime", "faster_whisper",
        "winapi", "hook", "inject", "worker",
        "speech", "updater", "gui", "gui_settings", "tray",
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

    # Build the real window, not just a bare Tk root. Importing tkinter proves
    # nothing about construction: App shells out to schtasks and enumerates
    # audio devices while building its tabs, and it was that shell-out — not any
    # import — which killed every shortcut launch through 0.1.2.
    try:
        import tkinter

        import gui

        root = tkinter.Tk()
        root.withdraw()
        store = config_mod.ConfigStore(paths.config_path())
        store.load()
        gui.App(root, store, AppState(), __version__, use_tray=False)
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
    import speech

    for kind in ("input", "output"):
        out(f"{kind} devices:")
        devices = speech.list_devices(kind)
        if not devices:
            out("  (none found)")
        for index, label in devices:
            out(f"  {index}: {label}")
        out()
    return 0


# -- GUI-only preview ----------------------------------------------------


def run_gui_only() -> int:
    """Launch the real window with no Win32, audio or model behind it.

    The point is to be able to check layout and the event pump on a machine
    that can't run the app for real.
    """
    import tkinter as tk

    import gui

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


def run(args) -> int:
    import tkinter as tk

    import gui
    import hook as hook_mod
    import keys
    import speech
    import updater
    import winapi
    import worker as worker_mod

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
        trigger_vk = keys.parse_key(cfg.trigger_key)
    except keys.KeyNameError as exc:
        log.error("%s; falling back to F13", exc)
        trigger_vk = keys.VK["f13"]

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
    hook = hook_mod.KeyboardHook(trigger_vk, events, lambda: app_state.enabled)
    worker = worker_mod.Worker(store, app_state, events, recorder, transcriber)
    worker.hook = hook

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
        transcriber=transcriber, hook=hook, use_tray=not args.no_tray,
    )

    if checker is not None:
        checker.on_ready = lambda installer: root.after(
            0, lambda: app._launch_installer(installer)
        )

    worker.start()
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
