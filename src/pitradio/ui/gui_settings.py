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

from pitradio import i18n, paths, speech
from pitradio import state as state_mod
from pitradio.i18n import t
from pitradio.ui import theme

log = logging.getLogger(__name__)

TASK_NAME = "PitRadio"

# Capture arms the global hook. Leaving it armed indefinitely because someone
# clicked the button and wandered off is worse than making them click again.
CAPTURE_TIMEOUT_MS = 5000


# -- small helpers -------------------------------------------------------


def _row(parent, row: int, label: str, widget, hint: str = "") -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
    widget.grid(row=row, column=1, sticky="we", pady=3, padx=(8, 0))
    if hint:
        # Wrapped, not stretched: an unwrapped hint pushes the row wider than
        # the window and is simply cut off at the edge.
        ttk.Label(parent, text=hint, style="Muted.TLabel",
                  wraplength=240, justify="left").grid(
            row=row, column=2, sticky="w", padx=(8, 0))
    parent.columnconfigure(1, weight=1)


def _entry(parent, var, width: int = 24) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=var, width=width)


def _field_grid(parent, row: int, *fields, columns: int = 2) -> int:
    """Lay short fields out across the width instead of one per row.

    A millisecond value needs about four characters and was being given the
    whole window, so the profile editor ran to six rows of almost nothing and
    pushed everything below it off the bottom. Each entry is (label, widget,
    hint); a hint spans the remaining columns on its own line so it stays
    readable without stretching the row.

    Returns the next free row.
    """
    for index, (label, widget, hint) in enumerate(fields):
        column = (index % columns) * 2
        # Two grid rows per line: one for the controls, one for their hints.
        # Without reserving the second, a hint drew straight over the next
        # line's labels — "Key hold (ms)" with "raise this if the first
        # characters go missing" on top of it, which reads as corruption
        # rather than as a layout mistake. A screenshot found it; nothing else
        # would have.
        line = row + (index // columns) * 2
        ttk.Label(parent, text=label).grid(
            row=line, column=column, sticky="w", pady=(3, 0), padx=(0, 8))
        widget.grid(row=line, column=column + 1, sticky="w", pady=(3, 0),
                    padx=(0, 24))
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel",
                      wraplength=240, justify="left").grid(
                row=line + 1, column=column, columnspan=2, sticky="w",
                pady=(0, 2))
    lines = (len(fields) + columns - 1) // columns
    return row + lines * 2


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
            subprocess.Popen(["open", str(path)], stdin=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(path)], stdin=subprocess.DEVNULL)
    except Exception as exc:
        log.error("could not open %s: %s", path, exc)


def open_log_folder() -> None:
    open_folder(paths.log_dir())


def scrolling_tab(app, title: str) -> tuple[ttk.Frame, ttk.Frame]:
    """A tab whose content scrolls, with a footer that does not.

    Returns `(body, footer)`. Put the fields in `body` and the Save button in
    `footer`, which stays pinned to the bottom of the tab.

    Tabs grew past the height of a small window, and tkinter simply clips what
    does not fit — so Save was off the bottom with nothing to indicate it
    existed. Sizing the window larger only moves the problem, since the content
    of the Profiles tab depends on which plugin is assigned.
    """
    outer = ttk.Frame(app.notebook)
    app.notebook.add(outer, text=title)
    return scrolling_pane(outer)


def _palette(widget):
    """The palette the window was themed with.

    `scrolling_pane` is handed a container rather than the App, so it walks up
    to the toplevel for it. Falls back to light rather than failing — a pane
    that is the wrong shade still works.
    """
    return getattr(widget.winfo_toplevel(), "_pitradio_palette", theme.LIGHT_PALETTE)


def scrolling_pane(outer, padding: int = 12) -> tuple[ttk.Frame, ttk.Frame]:
    """The scroll-plus-sticky-footer machinery, for a tab or a pane inside one."""
    # Packed before the canvas so it keeps its height when space runs short.
    footer = ttk.Frame(outer, padding=(padding, 8))
    footer.pack(side="bottom", fill="x")
    ttk.Separator(outer, orient="horizontal").pack(side="bottom", fill="x")

    canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0,
                       background=_palette(outer).window)
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)

    body = ttk.Frame(canvas, padding=padding)
    window = canvas.create_window((0, 0), window=body, anchor="nw")

    def resized(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))
        # Only show the scrollbar when there is something to scroll: a
        # permanent one on a short tab reads as content being cut off.
        needed = body.winfo_reqheight() > canvas.winfo_height()
        if needed and not bar.winfo_ismapped():
            bar.pack(side="right", fill="y")
        elif not needed and bar.winfo_ismapped():
            bar.pack_forget()
            canvas.yview_moveto(0)

    body.bind("<Configure>", resized)
    canvas.bind(
        "<Configure>",
        lambda event: (canvas.itemconfigure(window, width=event.width), resized()),
    )
    _bind_mousewheel(canvas, body)
    return body, footer


def _bind_mousewheel(canvas, body) -> None:
    """Scroll on hover only.

    A global binding would hijack the wheel over the log pane and the history
    list, which have their own scrolling.
    """

    def scroll(event) -> None:
        if not canvas.winfo_exists():
            return
        if body.winfo_reqheight() <= canvas.winfo_height():
            return
        # Windows reports multiples of 120; macOS reports small counts; X11
        # sends button 4/5 with no delta at all.
        if getattr(event, "num", None) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif abs(event.delta) >= 120:
            step = -int(event.delta / 120)
        else:
            step = -int(event.delta) or (-1 if event.delta > 0 else 1)
        canvas.yview_scroll(step, "units")

    def bind(_event=None) -> None:
        canvas.bind_all("<MouseWheel>", scroll)
        canvas.bind_all("<Button-4>", scroll)
        canvas.bind_all("<Button-5>", scroll)

    def unbind(_event=None) -> None:
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    canvas.bind("<Enter>", bind)
    canvas.bind("<Leave>", unbind)


# -- Settings ------------------------------------------------------------


def build_settings_tab(app) -> None:
    frame, footer = scrolling_tab(app, t("Settings"))
    cfg = app.store.config

    trigger = ttk.LabelFrame(frame, text=t("Trigger"), padding=10)
    trigger.pack(fill="x")
    # Named so packaging/screenshots.py can crop to it.
    app.trigger_frame = trigger

    app.v_trigger = tk.StringVar(value=cfg.trigger_key)
    key_row = ttk.Frame(trigger)
    _entry(key_row, app.v_trigger, 20).pack(side="left")
    app.capture_key_button = ttk.Button(key_row, text="Press a key…")
    app.capture_key_button.pack(side="left", padx=6)
    app.trigger_capture = KeyCapture(
        app, app.v_trigger, app.capture_key_button,
        append=False, label="Press a key…")
    app.capture_key_button.configure(command=app.trigger_capture.start)
    _row(trigger, 0, t("Trigger key"), key_row,
         t("hold it to talk; it never reaches the game"))

    # Send and clear act on a message left waiting when a profile has
    # auto-send off. The tap/double-tap gestures on the talk trigger do the
    # same job; these exist for anyone who would rather not count taps.
    app.v_send_key = tk.StringVar(value=cfg.review.send_key)
    app.v_clear_key = tk.StringVar(value=cfg.review.clear_key)
    _row(trigger, 1, t("Send waiting message"),
         _key_binding_row(app, trigger, app.v_send_key),
         t("optional; same as tapping the trigger once"))
    _row(trigger, 2, t("Clear waiting message"),
         _key_binding_row(app, trigger, app.v_clear_key),
         t("optional; same as tapping the trigger twice"))

    # Wheels and gamepads are not read directly. Four input libraries were
    # tried — SDL3, SDL2, XInput and the legacy multimedia API — and between
    # them they still could not read a Fanatec rim or a Steam Controller
    # reliably, because both are claimed by software that does not share.
    # JoyToKey maps a button to a keyboard key, which the hook already sees.
    ttk.Label(
        trigger,
        text=t("Using a wheel or gamepad button? Map it to a keyboard key "
               "with JoyToKey, then set that key above."),
        style="Muted.TLabel", wraplength=640, justify="left",
    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    defaults = ttk.LabelFrame(frame, text=t("Default profile"), padding=10)
    defaults.pack(fill="x", pady=(10, 0))
    app.v_default = _profile_vars(app, defaults, cfg.default_profile, show_plugin=False)

    cues = ttk.LabelFrame(frame, text=t("Audio cues"), padding=10)
    cues.pack(fill="x", pady=(10, 0))
    app.v_cues_enabled = tk.BooleanVar(value=cfg.cues.enabled)
    app.v_cue_start = tk.StringVar(value=str(cfg.cues.start_hz))
    app.v_cue_stop = tk.StringVar(value=str(cfg.cues.stop_hz))
    app.v_cue_ms = tk.StringVar(value=str(cfg.cues.duration_ms))
    app.v_cue_vol = tk.StringVar(value=str(cfg.cues.volume))
    ttk.Checkbutton(cues, text=t("Beep on record start and stop"),
                    variable=app.v_cues_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
    _row(cues, 1, t("Start tone (Hz)"), _entry(cues, app.v_cue_start, 10))
    _row(cues, 2, t("Stop tone (Hz)"), _entry(cues, app.v_cue_stop, 10))
    _row(cues, 3, t("Duration (ms)"), _entry(cues, app.v_cue_ms, 10))
    _row(cues, 4, t("Volume (0-1)"), _entry(cues, app.v_cue_vol, 10))

    appearance = ttk.LabelFrame(frame, text=t("Appearance"), padding=10)
    appearance.pack(fill="x", pady=(10, 0))
    app.appearance_frame = appearance
    app.v_theme = tk.StringVar(value=_theme_label(cfg.gui.theme))
    _row(appearance, 0, t("Theme"),
         ttk.Combobox(appearance, textvariable=app.v_theme, width=18,
                      values=[label for _mode, label in THEME_CHOICES],
                      state="readonly"),
         t("takes effect next time PitRadio starts"))

    # Separate from the transcription language: a Spanish window with English
    # chat is a perfectly ordinary thing to want.
    app.v_language = tk.StringVar(value=_language_label(cfg.gui.language))
    _row(appearance, 1, t("Interface language"),
         ttk.Combobox(appearance, textvariable=app.v_language, width=18,
                      values=[label for _code, label in _language_choices()],
                      state="readonly"),
         t("English until someone contributes a translation"))

    startup = ttk.LabelFrame(frame, text=t("Startup"), padding=10)
    startup.pack(fill="x", pady=(10, 0))
    app.v_start_min = tk.BooleanVar(value=cfg.gui.start_minimized)
    ttk.Checkbutton(startup, text=t("Start minimised to tray"),
                    variable=app.v_start_min).pack(anchor="w")

    app.v_run_logon = tk.BooleanVar(value=_task_exists())
    logon = ttk.Checkbutton(
        startup, text=t("Start with Windows (as administrator)"),
        variable=app.v_run_logon, command=lambda: _apply_run_at_logon(app))
    logon.pack(anchor="w")
    if not paths.is_frozen():
        logon.state(["disabled"])
        ttk.Label(
            startup,
            text="Available in the installed build only — it registers a scheduled task.",
            style="Muted.TLabel",
        ).pack(anchor="w")

    ttk.Button(footer, text=t("Save"), command=lambda: _save_settings(app)).pack(anchor="e")


THEME_CHOICES = (
    ("system", "Match the system"),
    ("light", "Light"),
    ("dark", "Dark"),
)


def _language_choices() -> list[tuple[str, str]]:
    """(code, label) for every catalogue that ships, plus "follow the system"."""
    from pitradio import languages as languages_mod

    choices = [("system", t("Match the system"))]
    for code in i18n.available():
        choices.append((code, languages_mod.language_name(code)))
    return choices


def _language_label(code: str) -> str:
    return dict(_language_choices()).get(code, _language_choices()[0][1])


def _language_code(label: str) -> str:
    for code, text in _language_choices():
        if text == label:
            return code
    return "system"


def _theme_label(mode: str) -> str:
    return dict(THEME_CHOICES).get(mode, THEME_CHOICES[0][1])


def _theme_mode(label: str) -> str:
    for mode, text in THEME_CHOICES:
        if text == label:
            return mode
    return "system"


class KeyCapture:
    """Binds the next key pressed into a text field.

    Capture runs through the low-level hook rather than a tkinter key binding,
    for two reasons: the hook reports raw virtual-key codes, which map directly
    onto the config's key names — including F13-F24, which no keyboard has and
    tkinter reports inconsistently — and it swallows the press, so binding Enter
    or Escape doesn't also actuate the window behind the prompt.

    One instance per field. The trigger key replaces; the profile's key lists
    append, because those are sequences and you usually want to add to one.
    """

    def __init__(self, app, var, button, *, append: bool, label: str = "Set…"):
        self.app = app
        self.var = var
        self.button = button
        self.append = append
        self.label = label
        self._remaining = 0
        self._timer = None

    def start(self) -> None:
        if self.app.hook is None:
            messagebox.showinfo(
                t("PitRadio"),
                "Key capture needs the keyboard hook, which isn't running in "
                "this mode.")
            return

        self.button.state(["disabled"])
        self._remaining = CAPTURE_TIMEOUT_MS
        self._tick()
        self.app.hook.start_capture(self._captured)

    # -- countdown -------------------------------------------------------

    def _tick(self) -> None:
        """Count down visibly, so a capture about to lapse says so."""
        if self._remaining <= 0:
            log.info("key capture timed out; nothing was pressed")
            self.finish()
            return
        self.button.configure(text=f"Press a key… {self._remaining // 1000}")
        self._remaining -= 1000
        self._timer = self.app.root.after(1000, self._tick)

    def finish(self) -> None:
        if self._timer is not None:
            self.app.root.after_cancel(self._timer)
            self._timer = None
        self._remaining = 0
        if self.app.hook is not None:
            self.app.hook.cancel_capture()
        self.button.state(["!disabled"])
        self.button.configure(text=self.label)

    # -- result ----------------------------------------------------------

    def _captured(self, modifiers, vk) -> None:
        """Called on the hook thread; marshals back to Tk before touching it."""
        from pitradio import keys

        spec = keys.format_combo(modifiers, vk)
        self.app.root.after(0, lambda: self._apply(spec))

    def _apply(self, spec: str) -> None:
        self.finish()
        if self.append:
            existing = _text_to_keys(self.var.get())
            existing.append(spec)
            self.var.set(_keys_to_text(existing))
        else:
            self.var.set(spec)
        log.info("captured %s (save to apply)", spec)


def _capture_key(app) -> None:
    """The trigger key's own capture, kept as a named entry point."""
    app.trigger_capture.start()


def _key_binding_row(app, parent, key_var, key_label=None):
    """An entry and a capture button, for one optional key binding."""
    key_label = key_label or t("Key…")
    row = ttk.Frame(parent)
    _entry(row, key_var, 12).pack(side="left")
    button = ttk.Button(row, text=key_label, width=8)
    button.pack(side="left", padx=(4, 0))
    capture = KeyCapture(app, key_var, button, append=False, label=key_label)
    button.configure(command=capture.start)
    ttk.Button(row, text=t("Clear"), width=6,
               command=lambda: key_var.set("")).pack(side="left", padx=(6, 0))
    return row


def _profile_vars(app, parent, profile, *, show_plugin: bool = True) -> dict:
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
        "auto_send": tk.BooleanVar(value=profile.auto_send),
    }

    v["_captures"] = []
    for row, (field, label, hint) in enumerate((
        ("pre_keys", "Keys to open chat", "comma separated; modifiers like ctrl+enter work"),
        ("post_keys", "Keys to send", ""),
        ("abort_keys", "Keys to abort", "used when nothing was said"),
    )):
        _row(parent, row, label, _key_list_row(app, parent, v, field), hint)
    # Six short numbers, two per row. One per row ran the editor off the
    # bottom of the window for the sake of fields four characters wide.
    next_row = _field_grid(
        parent, 3,
        (t("Chat open delay (ms)"), _entry(parent, v["pre_delay_ms"], 8),
         t("raise this if the first characters go missing")),
        (t("Send delay (ms)"), _entry(parent, v["post_delay_ms"], 8), ""),
        (t("Key hold (ms)"), _entry(parent, v["key_hold_ms"], 8),
         t("below ~20ms games miss the press entirely")),
        (t("Gap between keys (ms)"), _entry(parent, v["key_gap_ms"], 8), ""),
        (t("Per character (ms)"), _entry(parent, v["type_delay_ms"], 8), ""),
        (t("Max characters"), _entry(parent, v["max_chars"], 8), ""),
    )

    mode = ttk.Combobox(parent, textvariable=v["text_mode"], width=12,
                        values=("unicode", "scancode"), state="readonly")
    _row(parent, next_row, t("Text injection"), mode,
         t("switch to scancode if the game ignores typed text"))

    # Off leaves the message in the chat box to be read before it goes out.
    # Whisper does mishear things, and in a public session a mistake is
    # everyone's problem.
    ttk.Checkbutton(
        parent, text=t("Send automatically"), variable=v["auto_send"],
    ).grid(row=next_row + 1, column=1, sticky="w", pady=3, padx=(8, 0))
    ttk.Label(parent, text=t("off types the message and leaves it for you to send"),
              style="Muted.TLabel", wraplength=240, justify="left").grid(
        row=next_row + 1, column=2, sticky="w", padx=(8, 0))

    # Hidden on the default profile. A session plugin reads one specific game,
    # and the default profile is what applies to games that have none — so a
    # choice there can never take effect. Offering it anyway meant setting it
    # in the obvious-looking place and having nothing happen.
    if not show_plugin:
        v["_plugin_choices"] = []
        v["plugin"] = tk.StringVar(value=profile.plugin)
        v["_settings_vars"] = {}
        v["_plugin_settings"] = dict(getattr(profile, "plugin_settings", {}) or {})
        return v

    # The plugin lives on the profile rather than the plugin declaring which
    # games it serves, so one plugin can be assigned to several games.
    choices = app.plugins.choices() if app.plugins is not None else [("", "(automatic)")]
    v["_plugin_choices"] = choices
    v["plugin"] = tk.StringVar(value=_plugin_label(choices, profile.plugin))
    picker = ttk.Combobox(parent, textvariable=v["plugin"], width=24,
                          values=[name for _id, name in choices], state="readonly")
    _row(parent, next_row + 2, t("Session plugin"), picker,
         t("reads who is in the session; automatic picks by executable name"))

    # The assigned plugin's own options, rebuilt whenever the choice changes so
    # only the relevant ones are ever on screen.
    v["_plugin_settings"] = dict(getattr(profile, "plugin_settings", {}) or {})
    v["_settings_vars"] = {}
    v["_settings_frame"] = ttk.Frame(parent)
    v["_settings_frame"].grid(row=next_row + 3, column=0, columnspan=3, sticky="we", pady=(4, 0))
    _rebuild_plugin_settings(app, v)
    picker.bind("<<ComboboxSelected>>",
                lambda _e: _rebuild_plugin_settings(app, v))
    return v


def _rebuild_plugin_settings(app, v: dict) -> None:
    """Render the options of whichever plugin is currently selected."""
    frame = v["_settings_frame"]
    for child in frame.winfo_children():
        child.destroy()
    v["_settings_vars"] = {}

    if app.plugins is None:
        return
    plugin = app.plugins.by_id(_plugin_id(v["_plugin_choices"], v["plugin"].get()))
    if plugin is None or not plugin.settings:
        return

    ttk.Label(frame, text=f"{plugin.name} options", style="Heading.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(4, 2))

    stored = v["_plugin_settings"]
    for row, setting in enumerate(plugin.settings, start=1):
        current = stored.get(setting.key, setting.default)
        if setting.kind == "bool":
            var = tk.BooleanVar(value=bool(current))
            ttk.Checkbutton(frame, text=setting.label, variable=var).grid(
                row=row, column=0, sticky="w")
        else:
            var = tk.StringVar(value=str(current))
            ttk.Label(frame, text=setting.label).grid(row=row, column=0, sticky="w")
            ttk.Entry(frame, textvariable=var, width=12).grid(
                row=row, column=1, sticky="w", padx=(8, 0))
        v["_settings_vars"][setting.key] = (var, setting)

        if setting.help:
            ttk.Label(frame, text=setting.help, style="Hint.TLabel",
                      wraplength=560, justify="left").grid(
                row=row, column=2, sticky="w", padx=(8, 0))


def _read_plugin_settings(v: dict) -> dict:
    values = {}
    for key, (var, setting) in v.get("_settings_vars", {}).items():
        raw = var.get()
        if setting.kind == "int":
            try:
                raw = int(str(raw).strip())
            except (TypeError, ValueError):
                raw = setting.default
        values[key] = raw
    return values


def _key_list_row(app, parent, v, field: str):
    """An entry for a key sequence, with capture and clear beside it."""
    frame = ttk.Frame(parent)
    _entry(frame, v[field], 26).pack(side="left")

    button = ttk.Button(frame, text="Add key…")
    button.pack(side="left", padx=6)
    capture = KeyCapture(app, v[field], button, append=True, label="Add key…")
    button.configure(command=capture.start)
    # Held so the capture object outlives this function; a garbage-collected
    # one would leave its countdown running against a dead button.
    v["_captures"].append(capture)

    ttk.Button(frame, text=t("Clear"), width=6,
               command=lambda var=v[field]: var.set("")).pack(side="left")
    return frame


def _plugin_label(choices, plugin_id: str) -> str:
    for identifier, name in choices:
        if identifier == (plugin_id or ""):
            return name
    # An id from a plugin that no longer ships. Show it rather than silently
    # resetting to none, so the config isn't quietly rewritten on save.
    return plugin_id or "(none)"


def _plugin_id(choices, label: str) -> str:
    for identifier, name in choices:
        if name == label:
            return identifier
    return ""


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
    profile.auto_send = bool(v["auto_send"].get())
    if v.get("_plugin_choices"):
        profile.plugin = _plugin_id(v["_plugin_choices"], v["plugin"].get())
        profile.plugin_settings = _read_plugin_settings(v)


def _save_settings(app) -> None:
    cfg = app.store.config
    cfg.trigger_key = app.v_trigger.get().strip() or cfg.trigger_key

    cfg.review.send_key = app.v_send_key.get().strip()
    cfg.review.clear_key = app.v_clear_key.get().strip()
    cfg.gui.theme = _theme_mode(app.v_theme.get())
    cfg.gui.language = _language_code(app.v_language.get())

    _read_profile_vars(app.v_default, cfg.default_profile)

    cfg.cues.enabled = app.v_cues_enabled.get()
    cfg.cues.start_hz = _as_int(app.v_cue_start, cfg.cues.start_hz)
    cfg.cues.stop_hz = _as_int(app.v_cue_stop, cfg.cues.stop_hz)
    cfg.cues.duration_ms = _as_int(app.v_cue_ms, cfg.cues.duration_ms)
    cfg.cues.volume = min(1.0, max(0.0, _as_float(app.v_cue_vol, cfg.cues.volume)))

    cfg.gui.start_minimized = app.v_start_min.get()
    app.save_config()


# -- run at logon --------------------------------------------------------


# Launched from a shortcut, this app has no console, and its std handles are
# invalid. subprocess must then be told to redirect *all three* streams:
# capture_output covers stdout and stderr but leaves stdin alone, and Windows
# fails process creation with [WinError 6] The handle is invalid. That killed
# the app during GUI construction in every release up to 0.1.2 — silently,
# because there was no console for the traceback to reach.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_SUBPROCESS_KWARGS = {
    "stdin": subprocess.DEVNULL,
    "capture_output": True,
    "text": True,
    "creationflags": _NO_WINDOW,
}


def _task_exists() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME], **_SUBPROCESS_KWARGS
        )
    except OSError as exc:
        # Whether a scheduled task exists is a checkbox's initial state. It is
        # never worth taking the window down for.
        log.warning("could not query the scheduled task: %s", exc)
        return False
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
                check=True, **_SUBPROCESS_KWARGS,
            )
            log.info("registered scheduled task %s", TASK_NAME)
        else:
            subprocess.run(
                ["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                check=True, **_SUBPROCESS_KWARGS,
            )
            log.info("removed scheduled task %s", TASK_NAME)
    except (subprocess.CalledProcessError, OSError) as exc:
        app.v_run_logon.set(not want)
        messagebox.showerror(
            t("PitRadio"),
            f"Could not change the startup task:\n{exc.stderr or exc}",
        )


# -- Profiles ------------------------------------------------------------


def build_profiles_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text=t("Profiles"))

    ttk.Label(
        frame,
        text=("One profile per sim, keyed on its executable name. The Status tab "
              "shows the name of whatever is focused — that is the key to use."),
        style="Hint.TLabel", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 8))

    body = ttk.Frame(frame)
    body.pack(fill="both", expand=True)

    left = ttk.Frame(body)
    left.pack(side="left", fill="y")
    app.profile_list = tk.Listbox(left, width=28, exportselection=False,
                                  **theme.listbox_options(app.palette))
    app.profile_list.pack(fill="y", expand=True)
    app.profile_list.bind("<<ListboxSelect>>", lambda _e: _load_profile(app))

    buttons = ttk.Frame(left)
    buttons.pack(fill="x", pady=6)
    ttk.Button(buttons, text=t("Add"), command=lambda: _add_profile(app)).pack(side="left")
    ttk.Button(buttons, text=t("Remove"), command=lambda: _remove_profile(app)).pack(
        side="left", padx=4)

    right = ttk.LabelFrame(body, text=t("Profile settings"), padding=0)
    right.pack(side="left", fill="both", expand=True, padx=(10, 0))
    fields, profile_footer = scrolling_pane(right, padding=10)

    from pitradio import config as config_mod

    app.v_profile = _profile_vars(app, fields, config_mod.Profile())
    ttk.Button(profile_footer, text=t("Save profile"),
               command=lambda: _save_profile(app)).pack(anchor="e")

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
    v["auto_send"].set(profile.auto_send)
    v["plugin"].set(_plugin_label(v["_plugin_choices"], profile.plugin))
    v["_plugin_settings"] = dict(getattr(profile, "plugin_settings", {}) or {})
    # Redraw, or the controls would still show the previously selected
    # profile's values while claiming to describe this one.
    _rebuild_plugin_settings(app, v)


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
    if not messagebox.askyesno(t("PitRadio"), f"Remove the profile for {name}?"):
        return
    app.store.config.profiles.pop(name, None)
    app.save_config()
    _refresh_profile_list(app)


def _save_profile(app) -> None:
    name = _selected_profile(app)
    if name is None:
        messagebox.showinfo(t("PitRadio"), t("Select a profile first, or add one."))
        return
    _read_profile_vars(app.v_profile, app.store.config.profiles[name])
    app.save_config()


# -- Vocabulary ----------------------------------------------------------


def build_vocabulary_tab(app) -> None:
    frame, footer = scrolling_tab(app, t("Vocabulary"))

    ttk.Label(
        frame,
        text=("Words Whisper should expect. Corner names, car and series terms, "
              "team mates' names — this measurably improves proper nouns. Applies "
              "on the next trigger; no model reload."),
        style="Hint.TLabel", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 8))

    app.vocab_text = tk.Text(frame, wrap="word", height=10,
                             **theme.text_options(app.palette))
    app.vocab_text.insert("1.0", app.store.config.whisper.initial_prompt)
    app.vocab_text.pack(fill="both", expand=True)

    ttk.Button(footer, text=t("Save"), command=lambda: _save_vocab(app)).pack(anchor="e")

    session = ttk.LabelFrame(frame, text=t("From the session (read-only)"), padding=8)
    session.pack(fill="both", expand=True, pady=(12, 0))

    ttk.Label(
        session,
        text=("Supplied by plugins at the moment you trigger, and prepended to "
              "the text above. Today that means driver names; another sim's "
              "plugin might contribute car names, teams or commentators. Shown "
              "here because a name Whisper keeps mangling is usually a name it "
              "was never told about."),
        style="Hint.TLabel", wraplength=860, justify="left",
    ).pack(fill="x", pady=(0, 6))

    app.runtime_vocab_text = tk.Text(session, wrap="word", height=8,
                                     **theme.text_options(app.palette),
                                     state="disabled", font=("Consolas", 9))
    app.runtime_vocab_text.pack(fill="both", expand=True)

    ttk.Button(session, text=t("Refresh"),
               command=lambda: refresh_runtime_vocabulary(app)).pack(
        anchor="w", pady=(6, 0))

    refresh_runtime_vocabulary(app)


def refresh_runtime_vocabulary(app) -> None:
    """Show what plugins currently supply, and the prompt Whisper would get."""
    from pitradio import mentions as mentions_mod

    lines: list[str] = []
    if app.plugins is None:
        lines.append("Plugins are unavailable in this run.")
    else:
        for name, terms, status in app.plugins.vocabularies():
            lines.append(f"{name}: {status}")
            if terms:
                lines.append(f"  {len(terms)} term(s): " + ", ".join(terms[:40]))
                if len(terms) > 40:
                    lines.append(f"  (+{len(terms) - 40} more)")
            lines.append("")

        cfg = app.store.config
        # The exact string handed to Whisper, truncation and ordering included,
        # so what is shown is what it receives rather than an approximation.
        active = next(
            (terms for _n, terms, _s in app.plugins.vocabularies() if terms), [])
        hint = mentions_mod.vocabulary_hint(active, cfg.mentions.max_names)
        from pitradio import speech as speech_mod

        effective = speech_mod._join_prompt(cfg.whisper.initial_prompt, hint) or ""
        lines.append("-- prompt Whisper receives --")
        lines.append(effective)

    app.runtime_vocab_text.configure(state="normal")
    app.runtime_vocab_text.delete("1.0", "end")
    app.runtime_vocab_text.insert("1.0", "\n".join(lines))
    app.runtime_vocab_text.configure(state="disabled")


def _save_vocab(app) -> None:
    app.store.config.whisper.initial_prompt = app.vocab_text.get("1.0", "end").strip()
    app.save_config()


# -- Audio ---------------------------------------------------------------


def build_audio_tab(app) -> None:
    frame, footer = scrolling_tab(app, t("Audio"))
    cfg = app.store.config

    inputs = ttk.LabelFrame(frame, text=t("Microphone"), padding=10)
    inputs.pack(fill="x")

    app.input_devices = speech.list_devices("input")
    app.v_input = tk.StringVar(value=_device_label(app.input_devices, cfg.audio.input_device))
    combo = ttk.Combobox(inputs, textvariable=app.v_input, state="readonly",
                         values=["(system default)"] + [label for _i, label in app.input_devices])
    _row(inputs, 0, t("Input device"), combo)

    app.v_gain = tk.DoubleVar(value=cfg.audio.gain)
    app.v_gain_label = tk.StringVar(value=_gain_text(cfg.audio.gain))
    gain_row = ttk.Frame(inputs)
    ttk.Scale(gain_row, from_=0.1, to=10.0, orient="horizontal", length=260,
              variable=app.v_gain,
              command=lambda _v: app.v_gain_label.set(_gain_text(app.v_gain.get()))
              ).pack(side="left")
    ttk.Label(gain_row, textvariable=app.v_gain_label, width=8).pack(side="left", padx=6)
    ttk.Button(gain_row, text=t("Reset"),
               command=lambda: _reset_gain(app)).pack(side="left")
    _row(inputs, 1, t("Microphone gain"), gain_row,
         t("raise if the level bar barely moves when you speak"))

    app.level = ttk.Progressbar(inputs, maximum=100)
    _row(inputs, 2, t("Level"), app.level)
    ttk.Label(inputs,
              text="The level bar shows the signal after gain — what Whisper "
                   "actually receives. Aim for it to peak around three quarters.",
              style="Hint.TLabel", wraplength=640, justify="left").grid(
        row=3, column=0, columnspan=3, sticky="w")

    app.v_test_result = tk.StringVar(value="")
    test_row = ttk.Frame(inputs)
    test_row.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 0))
    app.test_button = ttk.Button(test_row, text=t("Record 4s and transcribe"),
                                 command=lambda: _run_mic_test(app))
    app.test_button.pack(side="left")
    ttk.Label(test_row, textvariable=app.v_test_result, style="Value.TLabel",
              wraplength=560).pack(side="left", padx=10)
    ttk.Label(inputs, text=t("Nothing is typed anywhere during a test."),
              style="Hint.TLabel").grid(row=5, column=0, columnspan=3, sticky="w")

    outputs = ttk.LabelFrame(frame, text=t("Cue output"), padding=10)
    outputs.pack(fill="x", pady=(10, 0))
    app.output_devices = speech.list_devices("output")
    app.v_output = tk.StringVar(
        value=_device_label(app.output_devices, cfg.audio.output_device))
    _row(outputs, 0, t("Output device"),
         ttk.Combobox(outputs, textvariable=app.v_output, state="readonly",
                      values=["(system default)"] + [label for _i, label in app.output_devices]))
    ttk.Label(outputs,
              text="Pick something other than your sim's output so the beep doesn't "
                   "end up in the recording.",
              style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")
    ttk.Button(outputs, text=t("Play test cue"),
               command=lambda: _play_test_cue(app)).grid(
        row=2, column=1, sticky="w", pady=(8, 0))

    ttk.Button(footer, text=t("Save"), command=lambda: _save_audio(app)).pack(anchor="e")


DEFAULT_DEVICE = "(system default)"


def _device_label(devices, spec) -> str:
    """The row to show for a stored choice.

    Handles a stored *name* (what is written now) and a stored *index* (what
    older configs hold), so upgrading does not silently reset somebody's
    device to the default.
    """
    if spec is None or spec == "":
        return DEFAULT_DEVICE
    for index, label in devices:
        if isinstance(spec, int) and not isinstance(spec, bool):
            if spec == index:
                return label
            continue
        if speech.matches_device(spec, index, "output"):
            return label
        if str(spec).lower() in label.lower():
            return label
    return DEFAULT_DEVICE


def _device_from_label(devices, label: str, kind: str = "output"):
    """What to store for a chosen row: the device's **name**, not its index.

    Windows renumbers audio devices whenever the set of them changes, so an
    index saved today points somewhere else tomorrow — silently, because sound
    going to the wrong device raises nothing. A name survives that.

    **The fullest name, not whichever row was clicked.** MME truncates every
    device name to 31 characters, so picking the MME row and storing its name
    writes a truncation that can only ever match MME again — and MME is the one
    host API whose writes go nowhere while a game holds the endpoint. See
    `speech.MME_NAME_LIMIT`.
    """
    for index, shown in devices:
        if shown == label:
            return speech.canonical_name(index, kind) or index
    return None


def _gain_text(value: float) -> str:
    return f"{value:.1f}x"


def _reset_gain(app) -> None:
    app.v_gain.set(1.0)
    app.v_gain_label.set(_gain_text(1.0))


def _save_audio(app) -> None:
    cfg = app.store.config
    cfg.audio.gain = min(10.0, max(0.1, round(float(app.v_gain.get()), 2)))
    cfg.audio.input_device = _device_from_label(app.input_devices, app.v_input.get())
    # One device for the whole app — cues, voice and the engineer.
    cfg.audio.output_device = _device_from_label(
        app.output_devices, app.v_output.get())
    app.save_config()


def _play_test_cue(app) -> None:
    """Play on what's selected right now, not what was saved.

    Reading from a config object captured when the tab was built is wrong twice
    over: it ignores an unsaved dropdown change, and save_config() replaces that
    object via store.load(), so after the first save the test would play on
    whatever device was configured at startup, forever.
    """
    from pitradio import config as config_mod

    saved = app.store.config.cues
    cue = config_mod.CueConfig(
        # A test button should make a sound even when cues are switched off;
        # that is what the user is asking to hear.
        enabled=True,
        start_hz=_as_int(app.v_cue_start, saved.start_hz),
        stop_hz=_as_int(app.v_cue_stop, saved.stop_hz),
        duration_ms=_as_int(app.v_cue_ms, saved.duration_ms),
        volume=min(1.0, max(0.0, _as_float(app.v_cue_vol, saved.volume))),
    )
    device = _device_from_label(app.output_devices, app.v_output.get())
    log.info("test cue on device %r", device)
    speech.play_cue(cue, cue.start_hz, device)


def set_level(app, rms: float) -> None:
    # RMS on speech peaks well below 1.0; this scaling keeps the bar readable
    # rather than accurate, which is all it needs to be.
    app.level["value"] = min(100.0, rms * 400.0)


def _run_mic_test(app) -> None:
    if app.recorder is None or app.transcriber is None:
        messagebox.showinfo(
            t("PitRadio"), t("Audio isn't available in this run (GUI preview mode)."))
        return
    if app.state.status not in (state_mod.STATUS_IDLE, state_mod.STATUS_DISABLED):
        messagebox.showinfo(t("PitRadio"), "Busy — try again in a moment.")
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


# -- Voice ---------------------------------------------------------------


def build_voice_tab(app) -> None:
    """Sending the clip to the people you are racing, as well as typing it.

    Who can *hear* you is not on this tab and should not be: it depends on where
    the cars are, only a plugin knows that, and it is therefore per-sim. It
    lives in the profile's plugin settings, next to the standings toggle. This
    tab says so rather than leaving someone hunting for it.
    """
    frame, footer = scrolling_tab(app, t("Voice"))
    cfg = app.store.config

    # First, before any setting: who can hear you. A dictation app that quietly
    # opened the microphone to twenty strangers would be a betrayal, so the
    # window explains the deal before it offers the switch.
    consent = ttk.LabelFrame(frame, text=t("Before you switch this on"), padding=10)
    consent.pack(fill="x")
    ttk.Label(
        consent,
        text=t(
            "Voice sends the recording you just made to the other PitRadio "
            "users in your session, who hear it out loud. Nothing is sent "
            "unless you hold the trigger key — there is no open microphone, "
            "and there is no way to turn one on."
        ),
        style="Hint.TLabel", wraplength=640, justify="left").pack(anchor="w")

    sending = ttk.LabelFrame(frame, text=t("Sending"), padding=10)
    sending.pack(fill="x", pady=(10, 0))

    app.v_voice_enabled = tk.BooleanVar(value=cfg.voice.enabled)
    _row(sending, 0, t("Send my voice"),
         ttk.Checkbutton(sending, variable=app.v_voice_enabled),
         t("off until you switch it on"))

    app.v_voice_name = tk.StringVar(value=cfg.voice.display_name)
    _row(sending, 1, t("Shown to others as"), _entry(sending, app.v_voice_name),
         t("blank uses your name from the sim, which is what is already on "
           "their timing screen"))

    hearing = ttk.LabelFrame(frame, text=t("Hearing"), padding=10)
    hearing.pack(fill="x", pady=(10, 0))

    app.v_voice_playback = tk.BooleanVar(value=cfg.voice.playback)
    _row(hearing, 0, t("Play what others send"),
         ttk.Checkbutton(hearing, variable=app.v_voice_playback),
         t("separate from sending, so you can go quiet without going deaf — "
           "or the reverse"))

    # No device picker here. There is one output device for the whole app and
    # it lives on the Audio tab — three pickers meant three chances to send
    # sound somewhere nobody was listening, which is silent when it happens.
    ttk.Label(hearing, text=t("Plays on the output device set in the Audio tab."),
              style="Muted.TLabel").grid(row=1, column=0, columnspan=3,
                                         sticky="w", pady=(2, 4))

    app.v_voice_volume = tk.DoubleVar(value=cfg.voice.volume)
    app.v_voice_volume_label = tk.StringVar(value=_percent(cfg.voice.volume))
    volume_row = ttk.Frame(hearing)
    ttk.Scale(volume_row, from_=0.0, to=1.0, orient="horizontal", length=260,
              variable=app.v_voice_volume,
              command=lambda _v: app.v_voice_volume_label.set(
                  _percent(app.v_voice_volume.get()))).pack(side="left")
    ttk.Label(volume_row, textvariable=app.v_voice_volume_label,
              width=6).pack(side="left", padx=6)
    _row(hearing, 2, t("Volume"), volume_row)

    app.v_voice_max_age = tk.StringVar(value=str(cfg.voice.max_age_seconds))
    _row(hearing, 3, t("Ignore clips older than"),
         _entry(hearing, app.v_voice_max_age, width=8),
         t("seconds. Racing information goes stale — a warning about a car "
           "alongside is misleading once the corner is over"))

    # Named, not merely implied: someone looking for the proximity control will
    # look here first, and "it is on another tab" is only useful if it says
    # which one.
    who = ttk.LabelFrame(frame, text=t("Who you hear"), padding=10)
    who.pack(fill="x", pady=(10, 0))
    ttk.Label(
        who,
        text=t(
            "Hearing only the cars near you on track is a per-sim setting, "
            "because it depends on where everyone is and only the sim knows "
            "that. It is in Profiles, under the game's plugin settings, next "
            "to \"Recognise standings positions\"."
        ),
        style="Hint.TLabel", wraplength=640, justify="left").pack(anchor="w")

    relay = ttk.LabelFrame(frame, text=t("Relay"), padding=10)
    relay.pack(fill="x", pady=(10, 0))
    app.v_voice_relay = tk.StringVar(value=cfg.voice.relay)
    _row(relay, 0, t("Server"), _entry(relay, app.v_voice_relay, width=40),
         t("leave this alone unless you are running your own"))
    ttk.Label(
        relay,
        text=t(
            "The relay passes clips between everyone in your session. It is "
            "told a hash of the game server you are on and nothing else — not "
            "which server, not where any car is."
        ),
        style="Hint.TLabel", wraplength=640, justify="left").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    ttk.Button(footer, text=t("Save"), command=lambda: _save_voice(app)).pack(anchor="e")


def _percent(value: float) -> str:
    return f"{round(float(value) * 100)}%"


def _save_voice(app) -> None:
    cfg = app.store.config
    cfg.voice.enabled = bool(app.v_voice_enabled.get())
    cfg.voice.playback = bool(app.v_voice_playback.get())
    cfg.voice.display_name = app.v_voice_name.get().strip()
    cfg.voice.volume = min(1.0, max(0.0, round(float(app.v_voice_volume.get()), 2)))
    cfg.voice.max_age_seconds = max(
        0.1, _as_float(app.v_voice_max_age, cfg.voice.max_age_seconds))
    cfg.voice.relay = app.v_voice_relay.get().strip()
    app.save_config()


# -- Engineer ------------------------------------------------------------


def build_engineer_tab(app) -> None:
    """The named voice that talks back.

    Laid out as three questions in the order somebody asks them: who is this,
    what does it sound like, and what does it tell me. The routines go last
    because they are the part you configure once and then talk to.
    """
    frame, footer = scrolling_tab(app, t("Engineer"))
    cfg = app.store.config.engineer

    who = ttk.LabelFrame(frame, text=t("Who"), padding=10)
    who.pack(fill="x")
    app.engineer_frame = who

    app.v_eng_enabled = tk.BooleanVar(value=cfg.enabled)
    _row(who, 0, t("Engineer on"),
         ttk.Checkbutton(who, variable=app.v_eng_enabled),
         t("off until you switch it on; nothing is spoken until you do"))

    app.v_eng_name = tk.StringVar(value=cfg.name)
    _row(who, 1, t("Called"), _entry(who, app.v_eng_name),
         t("what it answers to. \"Chief, target P3\""))

    app.v_eng_terse = tk.BooleanVar(value=cfg.terse)
    _row(who, 2, t("Keep it short"),
         ttk.Checkbutton(who, variable=app.v_eng_terse),
         t("\"Tandy, faster exit, two tenths\" rather than the full sentence"))

    app.v_eng_language = tk.StringVar(value=_engineer_language_label(cfg.language))
    _row(who, 3, t("Speaks"),
         ttk.Combobox(who, textvariable=app.v_eng_language, state="readonly", width=18,
                      values=[label for _code, label in _engineer_languages()]),
         t("follows the transcription language, because that is the language "
           "your commands arrive in"))

    sound = ttk.LabelFrame(frame, text=t("Voice"), padding=10)
    sound.pack(fill="x", pady=(10, 0))

    # Filled in from a thread; asking Windows what speech voices it has means
    # starting a process, and doing that while the window is being built shows
    # up as the app taking a second longer to open.
    app.engineer_voices = []
    app.v_eng_voice = tk.StringVar(
        value=cfg.fallback_voice or t("(let Windows choose)"))
    app.engineer_voice_box = ttk.Combobox(
        sound, textvariable=app.v_eng_voice, state="readonly", width=32,
        values=[t("(let Windows choose)")])
    _row(sound, 1, t("Fallback voice"), app.engineer_voice_box,
         t("used only for driver names, which no pack can contain"))

    app.engineer_packs = _voice_packs()
    app.v_eng_pack = tk.StringVar(value=cfg.voice_pack or t("(no pack)"))
    _row(sound, 0, t("Voice"),
         ttk.Combobox(sound, textvariable=app.v_eng_pack, state="readonly", width=32,
                      values=[t("(no pack)"), *app.engineer_packs]),
         t("a recorded voice pack — the only thing that sounds like a person"))

    app.v_eng_rate = tk.StringVar(value="" if cfg.rate is None else str(cfg.rate))
    _row(sound, 2, t("Pace"), _entry(sound, app.v_eng_rate, width=8),
         t("-10 to 10 for the fallback voice; blank is the default of "
           "3, which is brisk. A voice pack is recorded and cannot be sped up"))

    # Same as voice: the device is the app's, set once on the Audio tab.
    ttk.Label(sound, text=t("Speaks on the output device set in the Audio tab."),
              style="Muted.TLabel").grid(row=3, column=0, columnspan=3,
                                         sticky="w", pady=(2, 4))

    app.v_eng_volume = tk.DoubleVar(value=cfg.volume)
    app.v_eng_volume_label = tk.StringVar(value=_percent(cfg.volume))
    volume_row = ttk.Frame(sound)
    ttk.Scale(volume_row, from_=0.0, to=1.0, orient="horizontal", length=240,
              variable=app.v_eng_volume,
              command=lambda _v: app.v_eng_volume_label.set(
                  _percent(app.v_eng_volume.get()))).pack(side="left")
    ttk.Label(volume_row, textvariable=app.v_eng_volume_label,
              width=6).pack(side="left", padx=6)
    _row(sound, 4, t("Volume"), volume_row)

    buttons = ttk.Frame(sound)
    ttk.Button(buttons, text=t("Test"), command=lambda: _test_engineer(app)).pack(side="left")
    ttk.Button(buttons, text=t("Open voice pack folder"),
               command=_open_voice_packs).pack(side="left", padx=6)
    ttk.Button(buttons, text=t("Write phrase list"),
               command=lambda: _write_phrase_list(app)).pack(side="left")
    buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

    ttk.Label(
        sound,
        text=t(
            "A voice pack is a folder of recorded phrases — the layout Crew "
            "Chief uses, so a pack generated with crew-chief-autovoicepack "
            "drops straight in. Names and lap times are not in any pack and "
            "are always spoken by the Windows voice."
        ),
        style="Hint.TLabel", wraplength=620, justify="left").grid(
        row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

    _build_behaviours(app, frame)
    _build_routines(app, frame)
    _build_questions(app, frame)

    ttk.Button(footer, text=t("Save"), command=lambda: _save_engineer(app)).pack(anchor="e")
    _load_engineer_voices(app)


def _build_behaviours(app, frame) -> None:
    """The things it says without being asked.

    Separate from routines because they are a separate idea: a behaviour is on
    for as long as the engineer is, and a routine is started by speaking and
    stands down again. Each behaviour carries its own **repeat** interval,
    which is what makes the spotter keep telling you a car is still there
    rather than mentioning it once as it arrives.
    """
    from pitradio.engineer import notifications as notifications_mod

    behaviours = ttk.LabelFrame(frame, text=t("Behaviours"), padding=10)
    behaviours.pack(fill="x", pady=(10, 0))
    app.behaviours_frame = behaviours

    ttk.Label(
        behaviours,
        text=t(
            "These run whenever the engineer does. Repeat is how many seconds "
            "before it says the same thing again while it is still true — 0 "
            "says it once. A car alongside is worth repeating; a lap time is "
            "not."
        ),
        style="Hint.TLabel", wraplength=620, justify="left").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

    stored_all = app.store.config.engineer.notifications or {}
    app.v_eng_notifications = {}
    row = 1
    for (identifier, name, description, default_on, default_repeat,
         repeat_help) in notifications_mod.describe():
        stored = stored_all.get(identifier)
        enabled = tk.BooleanVar(
            value=default_on if stored is None else bool(stored.enabled))
        repeat = tk.StringVar(
            value=str(default_repeat if stored is None else stored.repeat_seconds))

        controls = ttk.Frame(behaviours)
        ttk.Checkbutton(controls, variable=enabled).pack(side="left")
        ttk.Label(controls, text=t("repeat")).pack(side="left", padx=(12, 4))
        _entry(controls, repeat, 6).pack(side="left")
        ttk.Label(controls, text=t("s")).pack(side="left", padx=(3, 0))
        _row(behaviours, row, name, controls, repeat_help)

        ttk.Label(behaviours, text=description, style="Muted.TLabel",
                  wraplength=600, justify="left").grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        app.v_eng_notifications[identifier] = (enabled, repeat)
        row += 2

    ttk.Label(
        behaviours,
        text=t(
            "If the spotter calls \"left\" for a car on your right, turn on "
            "\"Swap spotter sides\" in Profiles, under the game's plugin "
            "settings. Which way round it is depends on the sim, and it could "
            "not be checked without a car on a track."
        ),
        style="Hint.TLabel", wraplength=620, justify="left").grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))


def _build_routines(app, frame) -> None:
    """The things you start by saying something."""
    from pitradio.engineer import routines as routines_mod

    cfg = app.store.config.engineer
    running = ttk.LabelFrame(frame, text=t("Routines"), padding=10)
    running.pack(fill="x", pady=(10, 0))
    app.routines_frame = running

    ttk.Label(
        running,
        text=t(
            "Say a start phrase while driving and the routine begins; say its "
            "end phrase, or just \"stop\", and it stands down. Put your own "
            "words in — one per line — and they replace the defaults. A phrase "
            "ending in a {placeholder} takes whatever you say next as its "
            "parameters."
        ),
        style="Hint.TLabel", wraplength=620, justify="left").pack(anchor="w")

    app.v_eng_routines = {}
    for (routine_id, name, description, defaults, ends,
         parameters) in routines_mod.describe():
        stored = (cfg.routines or {}).get(routine_id)
        box = ttk.Frame(running)
        box.pack(fill="x", pady=(12, 0))

        enabled = tk.BooleanVar(value=stored.enabled if stored else True)
        ttk.Checkbutton(box, text=name, variable=enabled).pack(anchor="w")
        ttk.Label(box, text=description, style="Muted.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", padx=(20, 0))
        if parameters:
            ttk.Label(box, text=t("Takes: {parameters}", parameters=parameters),
                      style="Muted.TLabel", wraplength=600,
                      justify="left").pack(anchor="w", padx=(20, 0))

        start_box = _phrase_box(
            app, box, t("Starts on"),
            stored.phrases if stored and stored.phrases else defaults)
        end_box = _phrase_box(
            app, box, t("Ends on"),
            stored.end_phrases if stored and stored.end_phrases else ends)
        app.v_eng_routines[routine_id] = (enabled, start_box, end_box)


def _build_questions(app, frame) -> None:
    """The things you ask, as opposed to the things you start.

    A switch each, and nothing else. A routine gets its trigger phrases edited
    because what a routine is *called* is not the routine; a question is a
    question, and the ones that can be answered are fixed by what the sim
    publishes. A phrase box here would imply you could invent one.

    The switch earns its place for a different reason: every phrase the
    engineer listens for is a phrase that can be taken out of a message meant
    for the whole session, and somebody who never asks these has no reason to
    carry that risk.
    """
    from pitradio.engineer import queries as queries_mod

    asking = ttk.LabelFrame(frame, text=t("Questions"), padding=10)
    asking.pack(fill="x", pady=(10, 0))

    ttk.Label(
        asking,
        text=t(
            "Ask while driving and you get one answer — nothing starts "
            "running. Anything that follows is read against this session: "
            "\"in GT3\" is a class on the grid, \"sector three\" is a "
            "sector. Say the engineer's name first if a question is not "
            "recognised on its own. Switch one off and its phrases go "
            "straight to the chat box like any other words."
        ),
        style="Hint.TLabel", wraplength=620, justify="left").pack(anchor="w")

    # Built here rather than held at module level so every string is a literal
    # the extractor can find — translations come from the same catalogue as
    # everything else in the window.
    described = (
        ("fastest_lap", t("Fastest lap"),
         t("who has the quickest lap of the session. Name a class to ask "
           "about that one instead of your own")),
        ("fastest_sector", t("Fastest sector"),
         t("who holds a sector. Say which sector, and a class if you want "
           "one other than yours")),
        ("my_best_lap", t("Your best lap"), t("what you have done so far")),
        ("fuel_to_finish", t("Fuel to finish"),
         t("what to fill the tank to, as a percentage, for a stop on the "
           "next lap or however many laps away you say. Needs a lap or two "
           "of running first, because the burn rate is measured rather than "
           "assumed")),
    )

    cfg = app.store.config.engineer
    app.v_eng_questions = {}
    for query_id, name, answers in described:
        stored = (cfg.questions or {}).get(query_id)
        box = ttk.Frame(asking)
        box.pack(fill="x", pady=(10, 0))

        enabled = tk.BooleanVar(value=stored.enabled if stored else True)
        ttk.Checkbutton(box, text=name, variable=enabled).pack(anchor="w")
        app.v_eng_questions[query_id] = enabled
        ttk.Label(box, text=answers, style="Muted.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", padx=(20, 0))
        spoken = queries_mod.DEFAULT_PHRASES.get(query_id, ())
        ttk.Label(box, text=" / ".join(f'"{phrase}"' for phrase in spoken),
                  style="Muted.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", padx=(20, 0))


def _phrase_box(app, parent, label: str, lines_: tuple[str, ...]):
    """A small multi-line field of trigger phrases, one per line."""
    ttk.Label(parent, text=label, style="Muted.TLabel").pack(
        anchor="w", padx=(20, 0), pady=(6, 0))
    text = tk.Text(parent, height=3, wrap="none",
                   **theme.text_options(app.palette), font=theme.MONO_FONT,
                   padx=6, pady=4)
    text.insert("1.0", "\n".join(lines_))
    text.pack(fill="x", padx=(20, 0))
    return text


def _engineer_languages() -> list[tuple[str, str]]:
    """(code, label), with "follow the transcription language" first."""
    from pitradio import languages as languages_mod

    choices = [("", t("Follow transcription"))]
    for code in i18n.available():
        choices.append((code, languages_mod.language_name(code)))
    return choices


def _engineer_language_label(code: str) -> str:
    return dict(_engineer_languages()).get(code, _engineer_languages()[0][1])


def _engineer_language_code(label: str) -> str:
    for code, text in _engineer_languages():
        if text == label:
            return code
    return ""


def _voice_packs() -> list[str]:
    from pitradio.engineer import packs

    try:
        return [pack.name for pack in packs.discover(paths.voice_pack_dir())]
    except Exception as exc:
        log.debug("could not list voice packs: %s", exc)
        return []


def _open_voice_packs() -> None:
    open_folder(paths.voice_pack_dir())


def _write_phrase_list(app) -> None:
    """Write the phrase inventory a voice-pack generator needs.

    Generated from the app rather than kept by hand, so it can never drift from
    what the engineer actually says. Written in the engineer's own language,
    because a pack is recorded in the language it will be spoken in.
    """
    from pitradio import i18n as i18n_mod
    from pitradio.engineer import lines, packs

    catalogue = i18n_mod.Catalogue.for_setting(
        _engineer_language_code(app.v_eng_language.get()) or "en")
    target = paths.voice_pack_dir() / "phrase_inventory.csv"
    try:
        spoken = [catalogue.translate(line) for line in lines.FIXED_LINES
                  if "{" not in line]
        packs.inventory([(packs.slug(line), line) for line in spoken], target)
    except OSError as exc:
        messagebox.showerror(t("PitRadio"), f"Could not write the phrase list:\n{exc}")
        return
    log.info("wrote the engineer's phrase list to %s", target)
    open_folder(target.parent)


def _load_engineer_voices(app) -> None:
    """Ask Windows what speech voices exist, off the UI thread.

    Starting the speech host takes the best part of a second. Doing it while
    the tab is being built would show up as the whole window opening slowly,
    for a dropdown almost nobody touches.
    """
    def work() -> None:
        from pitradio.engineer import tts

        try:
            found = tts.installed_voices()
        except Exception as exc:
            log.debug("could not list speech voices: %s", exc)
            found = []
        try:
            app.root.after(0, lambda: _fill_engineer_voices(app, found))
        except Exception:
            # The window went away while the host was starting. Marshalling
            # onto a destroyed root raises RuntimeError rather than TclError,
            # from this thread, where nothing would catch it.
            log.debug("the voice list arrived after the window closed")

    threading.Thread(target=work, name="engineer-voices", daemon=True).start()


def _fill_engineer_voices(app, found) -> None:
    if not found:
        return
    app.engineer_voices = found
    try:
        app.engineer_voice_box.configure(
            values=[t("(let Windows choose)"), *[voice.label for voice in found]])
    except tk.TclError:
        # The window closed while the host was starting.
        log.debug("the voice list arrived after the tab went away")


def _engineer_voice_name(app) -> str:
    """The chosen voice's real name, not the label with its details on."""
    chosen = app.v_eng_voice.get()
    for voice in app.engineer_voices:
        if voice.label == chosen:
            return voice.name
    return "" if chosen == t("(let Windows choose)") else chosen


def _test_engineer(app) -> None:
    """Speak a line with what is selected right now, saved or not."""
    if app.engineer is None:
        messagebox.showinfo(
            t("PitRadio"),
            t("The engineer isn't running in this mode, so there is nothing to "
              "hear."))
        return
    _apply_engineer(app)
    threading.Thread(target=app.engineer.say_test, name="engineer-test",
                     daemon=True).start()


def _apply_engineer(app) -> None:
    """Copy the tab's fields onto the live config, without saving.

    Split from `_save_engineer` so Test can hear an unsaved change. Reading
    from a config captured when the tab was built would test whatever was
    configured at startup, forever — the same trap the cue test fell into.
    """
    cfg = app.store.config.engineer
    cfg.enabled = bool(app.v_eng_enabled.get())
    cfg.name = app.v_eng_name.get().strip()
    cfg.language = _engineer_language_code(app.v_eng_language.get())
    cfg.fallback_voice = _engineer_voice_name(app)
    pack = app.v_eng_pack.get()
    cfg.voice_pack = "" if pack == t("(no pack)") else pack
    cfg.terse = bool(app.v_eng_terse.get())
    rate = str(app.v_eng_rate.get()).strip()
    try:
        cfg.rate = max(-10, min(10, int(rate))) if rate else None
    except ValueError:
        cfg.rate = None
    cfg.volume = min(1.0, max(0.0, round(float(app.v_eng_volume.get()), 2)))
    from pitradio import config as config_mod

    cfg.notifications = {
        identifier: config_mod.NotificationConfig(
            enabled=bool(enabled.get()),
            repeat_seconds=max(0.0, _as_float(repeat, 0.0)),
        )
        for identifier, (enabled, repeat) in app.v_eng_notifications.items()
    }

    cfg.routines = {
        routine_id: config_mod.RoutineConfig(
            enabled=bool(enabled.get()),
            phrases=_phrase_lines(start_box),
            end_phrases=_phrase_lines(end_box),
        )
        for routine_id, (enabled, start_box, end_box) in app.v_eng_routines.items()
    }
    cfg.questions = {
        query_id: config_mod.QuestionConfig(enabled=bool(enabled.get()))
        for query_id, enabled in getattr(app, "v_eng_questions", {}).items()
    }


def _phrase_lines(text) -> list[str]:
    return [line.strip() for line in text.get("1.0", "end").splitlines()
            if line.strip()]


def _save_engineer(app) -> None:
    _apply_engineer(app)
    app.save_config()
    if app.engineer is not None:
        # Applied now rather than left to the next poll: someone who has just
        # changed the voice is about to press Test, and a persona that takes
        # effect a tenth of a second later still reads as "it did not work".
        app.engineer.refresh_voice(force=True)


# -- History -------------------------------------------------------------


def build_history_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text=t("History"))

    columns = ("time", "app", "sent", "text")
    app.history_tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
    for column, heading, width in (
        ("time", "Time", 80), ("app", "App", 160), ("sent", "Sent", 60),
        ("text", "Transcription", 520),
    ):
        app.history_tree.heading(column, text=heading)
        app.history_tree.column(column, width=width, anchor="w")
    app.history_tree.pack(fill="both", expand=True)

    # An empty table looks broken. Say why it's empty instead.
    app.history_empty = ttk.Label(
        frame,
        text=("Nothing yet. Messages appear here after a trigger completes.\n\n"
              "If you've used the trigger and this is still empty, the press "
              "isn't reaching PitRadio — check \"Listening for\" on the Status "
              "tab against what you expect.\n\n"
              "History is kept in memory only, so it starts empty every time "
              "the app launches."),
        style="Hint.TLabel", wraplength=680, justify="left",
    )

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(8, 0))
    ttk.Button(buttons, text=t("Copy"), command=lambda: _copy_history(app)).pack(side="left")
    ttk.Button(buttons, text=t("Re-send"), command=lambda: _resend_history(app)).pack(
        side="left", padx=6)
    ttk.Label(buttons,
              text=t("Re-send waits 3 seconds so you can focus the game first."),
              style="Hint.TLabel").pack(side="left", padx=6)

    for entry in reversed(app.state.history):
        add_history_row(app, entry)
    _refresh_history_placeholder(app)


def _refresh_history_placeholder(app) -> None:
    if app.history_tree.get_children():
        app.history_empty.pack_forget()
    else:
        app.history_empty.pack(fill="x", pady=(8, 0))


def add_history_row(app, entry) -> None:
    app.history_tree.insert(
        "", 0,
        values=(entry.clock, entry.exe, "yes" if entry.typed else "no",
                entry.text or "(nothing said)"),
    )
    _refresh_history_placeholder(app)


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
        messagebox.showinfo(t("PitRadio"), t("Re-send isn't available in this run."))
        return
    log.info("re-sending in 3 seconds — focus the game now")
    app.worker.request_resend(text)


# -- Updates -------------------------------------------------------------


def build_updates_tab(app) -> None:
    frame = ttk.Frame(app.notebook, padding=12)
    app.notebook.add(frame, text=t("Updates"))

    ttk.Label(frame, text=f"Installed version: {app.version}").pack(anchor="w")

    app.v_update_status = tk.StringVar(value="No update pending.")
    ttk.Label(frame, textvariable=app.v_update_status).pack(anchor="w", pady=(6, 0))

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(8, 0))
    ttk.Button(row, text=t("Check now"), command=app.check_for_updates).pack(side="left")

    app.v_auto_update = tk.BooleanVar(value=app.store.config.updates.auto_install)
    ttk.Checkbutton(row, text=t("Install updates automatically"),
                    variable=app.v_auto_update,
                    command=lambda: _save_auto_update(app)).pack(side="left", padx=12)

    ttk.Label(
        frame,
        text=("Updates are downloaded from GitHub and checked against the release's "
              "SHA256SUMS before anything is run. Builds are not code-signed, so that "
              "checksum proves the download is intact — not who produced it. "
              "Automatic installs are off by default for that reason, and are always "
              "deferred while a sim is in focus."),
        style="Hint.TLabel", wraplength=880, justify="left",
    ).pack(fill="x", pady=(12, 6))

    notes = ttk.LabelFrame(frame, text=t("Release notes"), padding=6)
    notes.pack(fill="both", expand=True)
    app.notes_text = tk.Text(notes, wrap="word", height=12, state="disabled",
                             padx=8, pady=6,
                             **theme.text_options(app.palette))
    # One border, not two: the LabelFrame already draws one, and
    # text_options() adds a highlight ring meant for a standalone pane.
    app.notes_text.configure(highlightthickness=0)
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
