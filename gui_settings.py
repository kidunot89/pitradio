"""The editing tabs: Settings, Profiles, Vocabulary, Audio, History, Updates.

Saving writes config.json and nothing else. The worker re-reads that file on its
next trigger, so editing here and editing the file by hand take exactly the same
path back into the running app — there is no second, in-memory route that could
drift from what's on disk.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import paths
import speech
import state as state_mod

log = logging.getLogger(__name__)

TASK_NAME = "PitRadio"


# -- small helpers -------------------------------------------------------


def _row(parent, row: int, label: str, widget, hint: str = "") -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
    widget.grid(row=row, column=1, sticky="we", pady=3, padx=(8, 0))
    if hint:
        ttk.Label(parent, text=hint, foreground="#777").grid(
            row=row, column=2, sticky="w", padx=(8, 0))
    parent.columnconfigure(1, weight=1)


def _entry(parent, var, width: int = 24) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=var, width=width)


def _as_int(var: tk.StringVar, fallback: int) -> int:
    try:
        return max(0, int(str(var.get()).strip()))
    except (TypeError, ValueError):
        return fallback


def _as_float(var: tk.StringVar, fallback: float) -> float:
    try:
        return float(str(var.get()).strip())
    except (TypeError, ValueError):
        return fallback


def _keys_to_text(specs: list[str]) -> str:
    return ", ".join(specs)


def _text_to_keys(text: str) -> list[str]:
    return [part.strip() for part in str(text).split(",") if part.strip()]


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log.error("could not open %s: %s", path, exc)


def open_log_folder() -> None:
    open_folder(paths.log_dir())


# -- Settings ------------------------------------------------------------


def build_settings_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="Settings")
    cfg = app.store.config

    trigger = ttk.LabelFrame(frame, text="Trigger", padding=10)
    trigger.pack(fill="x")
    app.v_trigger = tk.StringVar(value=cfg.trigger_key)
    _row(trigger, 0, "Trigger key", _entry(trigger, app.v_trigger),
         "F13 is unbound in every sim; map a wheel button to it externally")

    defaults = ttk.LabelFrame(frame, text="Default profile", padding=10)
    defaults.pack(fill="x", pady=(10, 0))
    app.v_default = _profile_vars(defaults, cfg.default_profile)

    cues = ttk.LabelFrame(frame, text="Audio cues", padding=10)
    cues.pack(fill="x", pady=(10, 0))
    app.v_cues_enabled = tk.BooleanVar(value=cfg.cues.enabled)
    app.v_cue_start = tk.StringVar(value=str(cfg.cues.start_hz))
    app.v_cue_stop = tk.StringVar(value=str(cfg.cues.stop_hz))
    app.v_cue_ms = tk.StringVar(value=str(cfg.cues.duration_ms))
    app.v_cue_vol = tk.StringVar(value=str(cfg.cues.volume))
    ttk.Checkbutton(cues, text="Beep on record start and stop",
                    variable=app.v_cues_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
    _row(cues, 1, "Start tone (Hz)", _entry(cues, app.v_cue_start, 10))
    _row(cues, 2, "Stop tone (Hz)", _entry(cues, app.v_cue_stop, 10))
    _row(cues, 3, "Duration (ms)", _entry(cues, app.v_cue_ms, 10))
    _row(cues, 4, "Volume (0-1)", _entry(cues, app.v_cue_vol, 10))

    startup = ttk.LabelFrame(frame, text="Startup", padding=10)
    startup.pack(fill="x", pady=(10, 0))
    app.v_start_min = tk.BooleanVar(value=cfg.gui.start_minimized)
    ttk.Checkbutton(startup, text="Start minimised to tray",
                    variable=app.v_start_min).pack(anchor="w")

    app.v_run_logon = tk.BooleanVar(value=_task_exists())
    logon = ttk.Checkbutton(
        startup, text="Start with Windows (as administrator)",
        variable=app.v_run_logon, command=lambda: _apply_run_at_logon(app))
    logon.pack(anchor="w")
    if not paths.is_frozen():
        logon.state(["disabled"])
        ttk.Label(
            startup,
            text="Available in the installed build only — it registers a scheduled task.",
            foreground="#777",
        ).pack(anchor="w")

    ttk.Button(frame, text="Save", command=lambda: _save_settings(app)).pack(
        anchor="e", pady=(12, 0))


def _profile_vars(parent, profile) -> dict:
    """Build the editors for one profile and return the vars keyed by field."""
    v = {
        "pre_keys": tk.StringVar(value=_keys_to_text(profile.pre_keys)),
        "post_keys": tk.StringVar(value=_keys_to_text(profile.post_keys)),
        "abort_keys": tk.StringVar(value=_keys_to_text(profile.abort_keys)),
        "pre_delay_ms": tk.StringVar(value=str(profile.pre_delay_ms)),
        "post_delay_ms": tk.StringVar(value=str(profile.post_delay_ms)),
        "key_hold_ms": tk.StringVar(value=str(profile.key_hold_ms)),
        "key_gap_ms": tk.StringVar(value=str(profile.key_gap_ms)),
        "type_delay_ms": tk.StringVar(value=str(profile.type_delay_ms)),
        "max_chars": tk.StringVar(value=str(profile.max_chars)),
        "text_mode": tk.StringVar(value=profile.text_mode),
    }

    _row(parent, 0, "Keys to open chat", _entry(parent, v["pre_keys"]),
         "comma separated, e.g. enter")
    _row(parent, 1, "Keys to send", _entry(parent, v["post_keys"]))
    _row(parent, 2, "Keys to abort", _entry(parent, v["abort_keys"]),
         "used when nothing was said")
    _row(parent, 3, "Delay after opening chat (ms)", _entry(parent, v["pre_delay_ms"], 10),
         "raise this if the first characters go missing")
    _row(parent, 4, "Delay before sending (ms)", _entry(parent, v["post_delay_ms"], 10))
    _row(parent, 5, "Key hold (ms)", _entry(parent, v["key_hold_ms"], 10),
         "below ~20ms games miss the press entirely")
    _row(parent, 6, "Gap between keys (ms)", _entry(parent, v["key_gap_ms"], 10))
    _row(parent, 7, "Delay per character (ms)", _entry(parent, v["type_delay_ms"], 10))
    _row(parent, 8, "Max characters", _entry(parent, v["max_chars"], 10))

    mode = ttk.Combobox(parent, textvariable=v["text_mode"], width=12,
                        values=("unicode", "scancode"), state="readonly")
    _row(parent, 9, "Text injection", mode,
         "switch to scancode if the game ignores typed text")
    return v


def _read_profile_vars(v: dict, profile) -> None:
    profile.pre_keys = _text_to_keys(v["pre_keys"].get())
    profile.post_keys = _text_to_keys(v["post_keys"].get())
    profile.abort_keys = _text_to_keys(v["abort_keys"].get())
    profile.pre_delay_ms = _as_int(v["pre_delay_ms"], profile.pre_delay_ms)
    profile.post_delay_ms = _as_int(v["post_delay_ms"], profile.post_delay_ms)
    profile.key_hold_ms = _as_int(v["key_hold_ms"], profile.key_hold_ms)
    profile.key_gap_ms = _as_int(v["key_gap_ms"], profile.key_gap_ms)
    profile.type_delay_ms = _as_int(v["type_delay_ms"], profile.type_delay_ms)
    profile.max_chars = _as_int(v["max_chars"], profile.max_chars)
    profile.text_mode = v["text_mode"].get() or "unicode"


def _save_settings(app) -> None:
    cfg = app.store.config
    cfg.trigger_key = app.v_trigger.get().strip() or cfg.trigger_key
    _read_profile_vars(app.v_default, cfg.default_profile)

    cfg.cues.enabled = app.v_cues_enabled.get()
    cfg.cues.start_hz = _as_int(app.v_cue_start, cfg.cues.start_hz)
    cfg.cues.stop_hz = _as_int(app.v_cue_stop, cfg.cues.stop_hz)
    cfg.cues.duration_ms = _as_int(app.v_cue_ms, cfg.cues.duration_ms)
    cfg.cues.volume = min(1.0, max(0.0, _as_float(app.v_cue_vol, cfg.cues.volume)))

    cfg.gui.start_minimized = app.v_start_min.get()
    app.save_config()


# -- run at logon --------------------------------------------------------


def _task_exists() -> bool:
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _apply_run_at_logon(app) -> None:
    """Scheduled task, not a Run key.

    A registry Run entry can't launch anything elevated, and this app needs
    elevation to type into a sim that runs elevated — so the Run key would
    produce an app that starts and silently does nothing.
    """
    if sys.platform != "win32":
        return

    want = app.v_run_logon.get()
    try:
        if want:
            subprocess.run(
                ["schtasks", "/create", "/f", "/tn", TASK_NAME,
                 "/tr", f'"{sys.executable}"', "/sc", "ONLOGON", "/rl", "HIGHEST"],
                capture_output=True, text=True, check=True,
            )
            log.info("registered scheduled task %s", TASK_NAME)
        else:
            subprocess.run(
                ["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                capture_output=True, text=True, check=True,
            )
            log.info("removed scheduled task %s", TASK_NAME)
    except subprocess.CalledProcessError as exc:
        app.v_run_logon.set(not want)
        messagebox.showerror(
            "PitRadio",
            f"Could not change the startup task:\n{exc.stderr or exc}",
        )


# -- Profiles ------------------------------------------------------------


def build_profiles_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="Profiles")

    ttk.Label(
        frame,
        text=("One profile per sim, keyed on its executable name. The Status tab "
              "shows the name of whatever is focused — that is the key to use."),
        foreground="#666", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 8))

    body = ttk.Frame(frame)
    body.pack(fill="both", expand=True)

    left = ttk.Frame(body)
    left.pack(side="left", fill="y")
    app.profile_list = tk.Listbox(left, width=28, exportselection=False)
    app.profile_list.pack(fill="y", expand=True)
    app.profile_list.bind("<<ListboxSelect>>", lambda _e: _load_profile(app))

    buttons = ttk.Frame(left)
    buttons.pack(fill="x", pady=6)
    ttk.Button(buttons, text="Add", command=lambda: _add_profile(app)).pack(side="left")
    ttk.Button(buttons, text="Remove", command=lambda: _remove_profile(app)).pack(
        side="left", padx=4)

    right = ttk.LabelFrame(body, text="Profile settings", padding=10)
    right.pack(side="left", fill="both", expand=True, padx=(10, 0))

    import config as config_mod

    app.v_profile = _profile_vars(right, config_mod.Profile())
    ttk.Button(right, text="Save profile", command=lambda: _save_profile(app)).grid(
        row=10, column=1, sticky="e", pady=(10, 0))

    _refresh_profile_list(app)


def _refresh_profile_list(app, select: str | None = None) -> None:
    app.profile_list.delete(0, "end")
    for name in sorted(app.store.config.profiles):
        app.profile_list.insert("end", name)
    if select is not None:
        names = sorted(app.store.config.profiles)
        if select in names:
            app.profile_list.selection_set(names.index(select))
            _load_profile(app)


def _selected_profile(app) -> str | None:
    selection = app.profile_list.curselection()
    if not selection:
        return None
    return app.profile_list.get(selection[0])


def _load_profile(app) -> None:
    name = _selected_profile(app)
    if name is None:
        return
    profile = app.store.config.profiles[name]
    v = app.v_profile
    v["pre_keys"].set(_keys_to_text(profile.pre_keys))
    v["post_keys"].set(_keys_to_text(profile.post_keys))
    v["abort_keys"].set(_keys_to_text(profile.abort_keys))
    v["pre_delay_ms"].set(str(profile.pre_delay_ms))
    v["post_delay_ms"].set(str(profile.post_delay_ms))
    v["key_hold_ms"].set(str(profile.key_hold_ms))
    v["key_gap_ms"].set(str(profile.key_gap_ms))
    v["type_delay_ms"].set(str(profile.type_delay_ms))
    v["max_chars"].set(str(profile.max_chars))
    v["text_mode"].set(profile.text_mode)


def _add_profile(app) -> None:
    from tkinter import simpledialog

    suggested = app.state.last_exe or ""
    name = simpledialog.askstring(
        "Add profile",
        "Executable name (the Status tab shows the focused one):",
        initialvalue=suggested, parent=app.root,
    )
    if not name:
        return

    import copy

    key = name.strip().lower()
    app.store.config.profiles[key] = copy.deepcopy(app.store.config.default_profile)
    app.save_config()
    _refresh_profile_list(app, select=key)


def _remove_profile(app) -> None:
    name = _selected_profile(app)
    if name is None:
        return
    if not messagebox.askyesno("PitRadio", f"Remove the profile for {name}?"):
        return
    app.store.config.profiles.pop(name, None)
    app.save_config()
    _refresh_profile_list(app)


def _save_profile(app) -> None:
    name = _selected_profile(app)
    if name is None:
        messagebox.showinfo("PitRadio", "Select a profile first, or add one.")
        return
    _read_profile_vars(app.v_profile, app.store.config.profiles[name])
    app.save_config()


# -- Vocabulary ----------------------------------------------------------


def build_vocabulary_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="Vocabulary")

    ttk.Label(
        frame,
        text=("Words Whisper should expect. Corner names, car and series terms, "
              "team mates' names — this measurably improves proper nouns. Applies "
              "on the next trigger; no model reload."),
        foreground="#666", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 8))

    app.vocab_text = tk.Text(frame, wrap="word", height=16)
    app.vocab_text.insert("1.0", app.store.config.whisper.initial_prompt)
    app.vocab_text.pack(fill="both", expand=True)

    ttk.Button(frame, text="Save", command=lambda: _save_vocab(app)).pack(
        anchor="e", pady=(10, 0))


def _save_vocab(app) -> None:
    app.store.config.whisper.initial_prompt = app.vocab_text.get("1.0", "end").strip()
    app.save_config()


# -- Audio ---------------------------------------------------------------


def build_audio_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="Audio")
    cfg = app.store.config

    inputs = ttk.LabelFrame(frame, text="Microphone", padding=10)
    inputs.pack(fill="x")

    app.input_devices = speech.list_devices("input")
    app.v_input = tk.StringVar(value=_device_label(app.input_devices, cfg.audio.input_device))
    combo = ttk.Combobox(inputs, textvariable=app.v_input, state="readonly",
                         values=["(system default)"] + [label for _i, label in app.input_devices])
    _row(inputs, 0, "Input device", combo)

    app.level = ttk.Progressbar(inputs, maximum=100)
    _row(inputs, 1, "Level", app.level)

    app.v_test_result = tk.StringVar(value="")
    test_row = ttk.Frame(inputs)
    test_row.grid(row=2, column=0, columnspan=3, sticky="we", pady=(8, 0))
    app.test_button = ttk.Button(test_row, text="Record 4s and transcribe",
                                 command=lambda: _run_mic_test(app))
    app.test_button.pack(side="left")
    ttk.Label(test_row, textvariable=app.v_test_result, foreground="#333",
              wraplength=560).pack(side="left", padx=10)
    ttk.Label(inputs, text="Nothing is typed anywhere during a test.",
              foreground="#777").grid(row=3, column=0, columnspan=3, sticky="w")

    outputs = ttk.LabelFrame(frame, text="Cue output", padding=10)
    outputs.pack(fill="x", pady=(10, 0))
    app.output_devices = speech.list_devices("output")
    app.v_output = tk.StringVar(
        value=_device_label(app.output_devices, cfg.cues.output_device))
    _row(outputs, 0, "Output device",
         ttk.Combobox(outputs, textvariable=app.v_output, state="readonly",
                      values=["(system default)"] + [label for _i, label in app.output_devices]))
    ttk.Label(outputs,
              text="Pick something other than your sim's output so the beep doesn't "
                   "end up in the recording.",
              foreground="#777").grid(row=1, column=0, columnspan=3, sticky="w")
    ttk.Button(outputs, text="Play test cue",
               command=lambda: speech.play_cue(cfg.cues, cfg.cues.start_hz)).grid(
        row=2, column=1, sticky="w", pady=(8, 0))

    ttk.Button(frame, text="Save", command=lambda: _save_audio(app)).pack(
        anchor="e", pady=(12, 0))


def _device_label(devices, spec) -> str:
    if spec is None or spec == "":
        return "(system default)"
    for index, label in devices:
        if spec == index or (isinstance(spec, str) and spec.lower() in label.lower()):
            return label
    return "(system default)"


def _device_from_label(devices, label: str):
    for index, name in devices:
        if name == label:
            return index
    return None


def _save_audio(app) -> None:
    cfg = app.store.config
    cfg.audio.input_device = _device_from_label(app.input_devices, app.v_input.get())
    cfg.cues.output_device = _device_from_label(app.output_devices, app.v_output.get())
    app.save_config()


def set_level(app, rms: float) -> None:
    # RMS on speech peaks well below 1.0; this scaling keeps the bar readable
    # rather than accurate, which is all it needs to be.
    app.level["value"] = min(100.0, rms * 400.0)


def _run_mic_test(app) -> None:
    if app.recorder is None or app.transcriber is None:
        messagebox.showinfo(
            "PitRadio", "Audio isn't available in this run (GUI preview mode).")
        return
    if app.state.status not in (state_mod.STATUS_IDLE, state_mod.STATUS_DISABLED):
        messagebox.showinfo("PitRadio", "Busy — try again in a moment.")
        return

    app.test_button.state(["disabled"])
    app.v_test_result.set("Recording…")

    def work():
        import time

        try:
            app.recorder.start(app.store.config.audio)
            time.sleep(4.0)
            audio = app.recorder.stop()
            app.root.after(0, lambda: app.v_test_result.set("Transcribing…"))
            raw = app.transcriber.transcribe(audio, app.store.config.whisper)
            text = speech.sanitize(raw, app.store.config.default_profile.max_chars)
            result = text or "(nothing recognised)"
        except Exception as exc:
            log.error("mic test failed: %s", exc)
            result = f"Test failed: {exc}"

        def done():
            app.v_test_result.set(result)
            app.test_button.state(["!disabled"])
            app.level["value"] = 0

        app.root.after(0, done)

    threading.Thread(target=work, name="mic-test", daemon=True).start()


# -- History -------------------------------------------------------------


def build_history_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="History")

    columns = ("time", "app", "sent", "text")
    app.history_tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
    for column, heading, width in (
        ("time", "Time", 80), ("app", "App", 160), ("sent", "Sent", 60),
        ("text", "Transcription", 520),
    ):
        app.history_tree.heading(column, text=heading)
        app.history_tree.column(column, width=width, anchor="w")
    app.history_tree.pack(fill="both", expand=True)

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(8, 0))
    ttk.Button(buttons, text="Copy", command=lambda: _copy_history(app)).pack(side="left")
    ttk.Button(buttons, text="Re-send", command=lambda: _resend_history(app)).pack(
        side="left", padx=6)
    ttk.Label(buttons,
              text="Re-send waits 3 seconds so you can focus the game first.",
              foreground="#777").pack(side="left", padx=6)

    for entry in reversed(app.state.history):
        add_history_row(app, entry)


def add_history_row(app, entry) -> None:
    app.history_tree.insert(
        "", 0,
        values=(entry.clock, entry.exe, "yes" if entry.typed else "no",
                entry.text or "(nothing said)"),
    )


def _selected_text(app) -> str | None:
    selection = app.history_tree.selection()
    if not selection:
        return None
    return app.history_tree.item(selection[0], "values")[3]


def _copy_history(app) -> None:
    text = _selected_text(app)
    if not text:
        return
    app.root.clipboard_clear()
    app.root.clipboard_append(text)


def _resend_history(app) -> None:
    text = _selected_text(app)
    if not text or text == "(nothing said)":
        return
    if app.worker is None:
        messagebox.showinfo("PitRadio", "Re-send isn't available in this run.")
        return
    log.info("re-sending in 3 seconds — focus the game now")
    app.worker.request_resend(text)


# -- Updates -------------------------------------------------------------


def build_updates_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text="Updates")

    ttk.Label(frame, text=f"Installed version: {app.version}").pack(anchor="w")

    app.v_update_status = tk.StringVar(value="No update pending.")
    ttk.Label(frame, textvariable=app.v_update_status).pack(anchor="w", pady=(6, 0))

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(8, 0))
    ttk.Button(row, text="Check now", command=app.check_for_updates).pack(side="left")

    app.v_auto_update = tk.BooleanVar(value=app.store.config.updates.auto_install)
    ttk.Checkbutton(row, text="Install updates automatically",
                    variable=app.v_auto_update,
                    command=lambda: _save_auto_update(app)).pack(side="left", padx=12)

    ttk.Label(
        frame,
        text=("Updates are downloaded from GitHub and checked against the release's "
              "SHA256SUMS before anything is run. Builds are not code-signed, so that "
              "checksum proves the download is intact — not who produced it. "
              "Automatic installs are off by default for that reason, and are always "
              "deferred while a sim is in focus."),
        foreground="#666", wraplength=880, justify="left",
    ).pack(fill="x", pady=(12, 6))

    notes = ttk.LabelFrame(frame, text="Release notes", padding=6)
    notes.pack(fill="both", expand=True)
    app.notes_text = tk.Text(notes, wrap="word", height=12, state="disabled")
    app.notes_text.pack(fill="both", expand=True)


def _save_auto_update(app) -> None:
    app.store.config.updates.auto_install = app.v_auto_update.get()
    app.save_config()


def set_update_details(app, info) -> None:
    app.v_update_status.set(f"Version {info.version} is available.")
    app.notes_text.configure(state="normal")
    app.notes_text.delete("1.0", "end")
    app.notes_text.insert("1.0", info.notes or "(no release notes)")
    app.notes_text.configure(state="disabled")
