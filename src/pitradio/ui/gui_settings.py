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

from pitradio import paths, speech
from pitradio import state as state_mod
from pitradio.ui import theme

log = logging.getLogger(__name__)

TASK_NAME = "PitRadio"

# Capture arms a global hook and a joystick poll. Leaving either armed
# indefinitely because someone clicked the button and wandered off is worse
# than making them click again.
CAPTURE_TIMEOUT_MS = 5000


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
        line = row + (index // columns)
        ttk.Label(parent, text=label).grid(
            row=line, column=column, sticky="w", pady=3, padx=(0, 8))
        widget.grid(row=line, column=column + 1, sticky="w", pady=3, padx=(0, 20))
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
                row=line + 1, column=column, columnspan=2, sticky="w",
                pady=(0, 4))
    used = (len(fields) + columns - 1) // columns
    return row + used + 1


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
    frame, footer = scrolling_tab(app, "Settings")
    cfg = app.store.config

    trigger = ttk.LabelFrame(frame, text="Trigger", padding=10)
    trigger.pack(fill="x")

    app.v_trigger = tk.StringVar(value=cfg.trigger_key)
    key_row = ttk.Frame(trigger)
    _entry(key_row, app.v_trigger, 20).pack(side="left")
    app.capture_key_button = ttk.Button(key_row, text="Press a key…")
    app.capture_key_button.pack(side="left", padx=6)
    app.trigger_capture = KeyCapture(
        app, app.v_trigger, app.capture_key_button,
        append=False, label="Press a key…")
    app.capture_key_button.configure(command=app.trigger_capture.start)
    _row(trigger, 0, "Trigger key", key_row,
         "hold it to talk; it never reaches the game")

    # Every controller binding is held until Save, so cancelling out of
    # Settings changes nothing.
    app.joy_slots = {}
    talk = joy_slot(app, "talk", cfg.joystick)
    _row(trigger, 1, "Wheel / gamepad button", _binding_row(app, trigger, talk),
         "works alongside the key — either one triggers")

    # Send and clear act on a message left waiting when a profile has
    # auto-send off. The tap/double-tap gestures on the talk trigger do the
    # same job; these exist for a wheel with buttons to spare, where one
    # button per action beats counting taps at speed.
    app.v_send_key = tk.StringVar(value=cfg.review.send_key)
    app.v_clear_key = tk.StringVar(value=cfg.review.clear_key)
    send = joy_slot(app, "send", cfg.send_joystick)
    clear = joy_slot(app, "clear", cfg.clear_joystick)
    _row(trigger, 2, "Send waiting message",
         _binding_row(app, trigger, send, app.v_send_key),
         "optional; same as tapping the trigger once")
    _row(trigger, 3, "Clear waiting message",
         _binding_row(app, trigger, clear, app.v_clear_key),
         "optional; same as tapping the trigger twice")

    if app.joystick is None:
        for slot in app.joy_slots.values():
            slot["button"].state(["disabled"])
        ttk.Label(
            trigger,
            text="Joystick input is unavailable in this run.",
            foreground="#777",
        ).grid(row=4, column=0, columnspan=3, sticky="w")
    else:
        app.v_joystick_devices = tk.StringVar(value="")
        ttk.Label(trigger, textvariable=app.v_joystick_devices, foreground="#777",
                  wraplength=640, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(trigger, text="Rescan controllers",
                   command=lambda: _refresh_joystick_devices(app)).grid(
            row=5, column=1, sticky="w", pady=(4, 0))
        _refresh_joystick_devices(app)

    defaults = ttk.LabelFrame(frame, text="Default profile", padding=10)
    defaults.pack(fill="x", pady=(10, 0))
    app.v_default = _profile_vars(app, defaults, cfg.default_profile, show_plugin=False)

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

    appearance = ttk.LabelFrame(frame, text="Appearance", padding=10)
    appearance.pack(fill="x", pady=(10, 0))
    app.v_theme = tk.StringVar(value=_theme_label(cfg.gui.theme))
    _row(appearance, 0, "Theme",
         ttk.Combobox(appearance, textvariable=app.v_theme, width=18,
                      values=[label for _mode, label in THEME_CHOICES],
                      state="readonly"),
         "takes effect next time PitRadio starts")

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
            style="Muted.TLabel",
        ).pack(anchor="w")

    ttk.Button(footer, text="Save", command=lambda: _save_settings(app)).pack(anchor="e")


THEME_CHOICES = (
    ("system", "Match the system"),
    ("light", "Light"),
    ("dark", "Dark"),
)


def _theme_label(mode: str) -> str:
    return dict(THEME_CHOICES).get(mode, THEME_CHOICES[0][1])


def _theme_mode(label: str) -> str:
    for mode, text in THEME_CHOICES:
        if text == label:
            return mode
    return "system"


def _joystick_label(app, joystick_cfg) -> str:
    """What the binding is, whether or not the controller is plugged in.

    A binding resolves by identity, so the bound device may be absent — saying
    "(not bound)" then would be a lie, and showing a stale index would be worse.
    The remembered name covers that case.
    """
    if joystick_cfg.button is None:
        return "(not bound)"

    remembered = joystick_cfg.name or None
    if app.joystick is None:
        return f"{remembered or f'device {joystick_cfg.device}'}, button {joystick_cfg.button}"

    if joystick_cfg.guid:
        for device in app.joystick.devices():
            if device.guid == joystick_cfg.guid:
                return app.joystick.describe(device.index, joystick_cfg.button)
        if remembered:
            return f"{remembered} - button {joystick_cfg.button} (not connected)"

    if joystick_cfg.device is None:
        return f"button {joystick_cfg.button} (not connected)"
    return app.joystick.describe(joystick_cfg.device, joystick_cfg.button)


def _refresh_joystick_devices(app) -> None:
    """Report what the joystick interface can actually see.

    Steam Input can capture a controller and re-present it in a form this
    interface never enumerates, and a Steam Controller in desktop mode is a
    keyboard and mouse rather than a joystick at all. Without this the user
    cannot tell "your controller is invisible to Windows here" from "capture is
    broken", which are entirely different problems.
    """
    if app.joystick is None:
        return
    lines = app.joystick.diagnose()
    app.v_joystick_devices.set("\n".join(lines))
    for line in lines:
        log.info("joystick: %s", line)


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
                "PitRadio",
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


def joy_slot(app, name: str, binding) -> dict:
    """State for one controller binding in the Settings tab.

    The talk trigger, send and clear each get one. They behave identically, so
    they share the capture machinery rather than having three near-copies that
    drift apart — which is how the talk binding ended up with a timeout message
    the others would have lacked.
    """
    slot = {
        "name": name,
        "binding": binding,
        # Held until Save, so leaving Settings without saving changes nothing.
        "captured": (binding.device, binding.button, binding.guid, binding.name),
        "var": tk.StringVar(value=_joystick_label(app, binding)),
        "button": None,
        "deadline": 0,
        "timer": None,
    }
    app.joy_slots[name] = slot
    return slot


def _capture_button(app, slot) -> None:
    """Bind the next wheel or gamepad button pressed."""
    if app.joystick is None:
        return

    slot["button"].state(["disabled"])
    slot["var"].set("waiting for a button…")
    slot["deadline"] = CAPTURE_TIMEOUT_MS
    _tick_button_capture(app, slot)

    def done(device, button) -> None:
        def apply() -> None:
            _end_button_capture(app, slot)
            slot["captured"] = (device.index, button, device.guid, device.name)
            slot["var"].set(app.joystick.describe(device.index, button))
            log.info("captured %s on %r [%s] for %s (save to apply)",
                     app.joystick.describe(device.index, button),
                     device.name, device.guid or "no identity", slot["name"])

        app.root.after(0, apply)

    app.joystick.start_capture(done)


def _tick_button_capture(app, slot) -> None:
    remaining = slot["deadline"]
    if remaining <= 0:
        log.info("button capture timed out; no button was pressed")
        _end_button_capture(app, slot)
        # Say why, rather than silently reverting: with no detected controller
        # this is the expected outcome and the user needs to know that.
        slot["var"].set(_joystick_label(app, slot["binding"]))
        if not app.joystick.list_devices():
            messagebox.showinfo(
                "PitRadio",
                "No controller was detected, so no button could be captured.\n\n"
                "See the controller list under the Trigger section for what "
                "PitRadio can currently see.",
            )
        return
    slot["button"].configure(text=f"Press a button… {remaining // 1000}")
    slot["deadline"] = remaining - 1000
    slot["timer"] = app.root.after(1000, lambda: _tick_button_capture(app, slot))


def _end_button_capture(app, slot) -> None:
    if slot["timer"] is not None:
        app.root.after_cancel(slot["timer"])
        slot["timer"] = None
    slot["deadline"] = 0
    if app.joystick is not None:
        app.joystick.cancel_capture()
    slot["button"].state(["!disabled"])
    slot["button"].configure(text="Press a button…")


def _clear_joystick(app, slot) -> None:
    slot["captured"] = (None, None, None, None)
    slot["var"].set("(not bound)")
    _end_button_capture(app, slot)


def _binding_row(app, parent, slot, key_var=None, key_label="Press a key…"):
    """A key box and a controller box side by side, for one action."""
    row = ttk.Frame(parent)

    if key_var is not None:
        _entry(row, key_var, 14).pack(side="left")
        key_button = ttk.Button(row, text=key_label)
        key_button.pack(side="left", padx=(4, 10))
        capture = KeyCapture(app, key_var, key_button, append=False, label=key_label)
        key_button.configure(command=capture.start)

    ttk.Label(row, textvariable=slot["var"], foreground="#333",
              width=30).pack(side="left")
    slot["button"] = ttk.Button(
        row, text="Press a button…", command=lambda: _capture_button(app, slot))
    slot["button"].pack(side="left", padx=6)
    ttk.Button(row, text="Clear",
               command=lambda: _clear_joystick(app, slot)).pack(side="left")
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
    _field_grid(
        parent, 3,
        ("Chat open delay (ms)", _entry(parent, v["pre_delay_ms"], 8),
         "raise this if the first characters go missing"),
        ("Send delay (ms)", _entry(parent, v["post_delay_ms"], 8), ""),
        ("Key hold (ms)", _entry(parent, v["key_hold_ms"], 8),
         "below ~20ms games miss the press entirely"),
        ("Gap between keys (ms)", _entry(parent, v["key_gap_ms"], 8), ""),
        ("Per character (ms)", _entry(parent, v["type_delay_ms"], 8), ""),
        ("Max characters", _entry(parent, v["max_chars"], 8), ""),
    )

    mode = ttk.Combobox(parent, textvariable=v["text_mode"], width=12,
                        values=("unicode", "scancode"), state="readonly")
    _row(parent, 10, "Text injection", mode,
         "switch to scancode if the game ignores typed text")

    # Off leaves the message in the chat box to be read before it goes out.
    # Whisper does mishear things, and in a public session a mistake is
    # everyone's problem.
    ttk.Checkbutton(
        parent, text="Send automatically", variable=v["auto_send"],
    ).grid(row=11, column=1, sticky="w", pady=3, padx=(8, 0))
    ttk.Label(parent, text="off types the message and leaves it for you to send",
              style="Muted.TLabel").grid(row=11, column=2, sticky="w", padx=(8, 0))

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
    _row(parent, 12, "Session plugin", picker,
         "reads who is in the session; automatic picks by executable name")

    # The assigned plugin's own options, rebuilt whenever the choice changes so
    # only the relevant ones are ever on screen.
    v["_plugin_settings"] = dict(getattr(profile, "plugin_settings", {}) or {})
    v["_settings_vars"] = {}
    v["_settings_frame"] = ttk.Frame(parent)
    v["_settings_frame"].grid(row=13, column=0, columnspan=3, sticky="we", pady=(4, 0))
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

    ttk.Label(frame, text=f"{plugin.name} options", foreground="#555").grid(
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
            ttk.Label(frame, text=setting.help, foreground="#777",
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

    ttk.Button(frame, text="Clear", width=6,
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

    for slot_name, binding in (
        ("talk", cfg.joystick),
        ("send", cfg.send_joystick),
        ("clear", cfg.clear_joystick),
    ):
        slot = app.joy_slots.get(slot_name)
        if slot is None:
            continue
        binding.device, binding.button, binding.guid, binding.name = slot["captured"]

    cfg.review.send_key = app.v_send_key.get().strip()
    cfg.review.clear_key = app.v_clear_key.get().strip()
    cfg.gui.theme = _theme_mode(app.v_theme.get())

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
    app.profile_list = tk.Listbox(left, width=28, exportselection=False,
                                  **theme.listbox_options(app.palette))
    app.profile_list.pack(fill="y", expand=True)
    app.profile_list.bind("<<ListboxSelect>>", lambda _e: _load_profile(app))

    buttons = ttk.Frame(left)
    buttons.pack(fill="x", pady=6)
    ttk.Button(buttons, text="Add", command=lambda: _add_profile(app)).pack(side="left")
    ttk.Button(buttons, text="Remove", command=lambda: _remove_profile(app)).pack(
        side="left", padx=4)

    right = ttk.LabelFrame(body, text="Profile settings", padding=0)
    right.pack(side="left", fill="both", expand=True, padx=(10, 0))
    fields, profile_footer = scrolling_pane(right, padding=10)

    from pitradio import config as config_mod

    app.v_profile = _profile_vars(app, fields, config_mod.Profile())
    ttk.Button(profile_footer, text="Save profile",
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
    frame, footer = scrolling_tab(app, "Vocabulary")

    ttk.Label(
        frame,
        text=("Words Whisper should expect. Corner names, car and series terms, "
              "team mates' names — this measurably improves proper nouns. Applies "
              "on the next trigger; no model reload."),
        foreground="#666", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 8))

    app.vocab_text = tk.Text(frame, wrap="word", height=10,
                             **theme.text_options(app.palette))
    app.vocab_text.insert("1.0", app.store.config.whisper.initial_prompt)
    app.vocab_text.pack(fill="both", expand=True)

    ttk.Button(footer, text="Save", command=lambda: _save_vocab(app)).pack(anchor="e")

    session = ttk.LabelFrame(frame, text="From the session (read-only)", padding=8)
    session.pack(fill="both", expand=True, pady=(12, 0))

    ttk.Label(
        session,
        text=("Supplied by plugins at the moment you trigger, and prepended to "
              "the text above. Today that means driver names; another sim's "
              "plugin might contribute car names, teams or commentators. Shown "
              "here because a name Whisper keeps mangling is usually a name it "
              "was never told about."),
        foreground="#666", wraplength=860, justify="left",
    ).pack(fill="x", pady=(0, 6))

    app.runtime_vocab_text = tk.Text(session, wrap="word", height=8,
                                     **theme.text_options(app.palette),
                                     state="disabled", font=("Consolas", 9))
    app.runtime_vocab_text.pack(fill="both", expand=True)

    ttk.Button(session, text="Refresh",
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
    frame, footer = scrolling_tab(app, "Audio")
    cfg = app.store.config

    inputs = ttk.LabelFrame(frame, text="Microphone", padding=10)
    inputs.pack(fill="x")

    app.input_devices = speech.list_devices("input")
    app.v_input = tk.StringVar(value=_device_label(app.input_devices, cfg.audio.input_device))
    combo = ttk.Combobox(inputs, textvariable=app.v_input, state="readonly",
                         values=["(system default)"] + [label for _i, label in app.input_devices])
    _row(inputs, 0, "Input device", combo)

    app.v_gain = tk.DoubleVar(value=cfg.audio.gain)
    app.v_gain_label = tk.StringVar(value=_gain_text(cfg.audio.gain))
    gain_row = ttk.Frame(inputs)
    ttk.Scale(gain_row, from_=0.1, to=10.0, orient="horizontal", length=260,
              variable=app.v_gain,
              command=lambda _v: app.v_gain_label.set(_gain_text(app.v_gain.get()))
              ).pack(side="left")
    ttk.Label(gain_row, textvariable=app.v_gain_label, width=8).pack(side="left", padx=6)
    ttk.Button(gain_row, text="Reset",
               command=lambda: _reset_gain(app)).pack(side="left")
    _row(inputs, 1, "Microphone gain", gain_row,
         "raise if the level bar barely moves when you speak")

    app.level = ttk.Progressbar(inputs, maximum=100)
    _row(inputs, 2, "Level", app.level)
    ttk.Label(inputs,
              text="The level bar shows the signal after gain — what Whisper "
                   "actually receives. Aim for it to peak around three quarters.",
              foreground="#777", wraplength=640, justify="left").grid(
        row=3, column=0, columnspan=3, sticky="w")

    app.v_test_result = tk.StringVar(value="")
    test_row = ttk.Frame(inputs)
    test_row.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 0))
    app.test_button = ttk.Button(test_row, text="Record 4s and transcribe",
                                 command=lambda: _run_mic_test(app))
    app.test_button.pack(side="left")
    ttk.Label(test_row, textvariable=app.v_test_result, foreground="#333",
              wraplength=560).pack(side="left", padx=10)
    ttk.Label(inputs, text="Nothing is typed anywhere during a test.",
              foreground="#777").grid(row=5, column=0, columnspan=3, sticky="w")

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
               command=lambda: _play_test_cue(app)).grid(
        row=2, column=1, sticky="w", pady=(8, 0))

    ttk.Button(footer, text="Save", command=lambda: _save_audio(app)).pack(anchor="e")


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


def _gain_text(value: float) -> str:
    return f"{value:.1f}x"


def _reset_gain(app) -> None:
    app.v_gain.set(1.0)
    app.v_gain_label.set(_gain_text(1.0))


def _save_audio(app) -> None:
    cfg = app.store.config
    cfg.audio.gain = min(10.0, max(0.1, round(float(app.v_gain.get()), 2)))
    cfg.audio.input_device = _device_from_label(app.input_devices, app.v_input.get())
    cfg.cues.output_device = _device_from_label(app.output_devices, app.v_output.get())
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
        output_device=_device_from_label(app.output_devices, app.v_output.get()),
        start_hz=_as_int(app.v_cue_start, saved.start_hz),
        stop_hz=_as_int(app.v_cue_stop, saved.stop_hz),
        duration_ms=_as_int(app.v_cue_ms, saved.duration_ms),
        volume=min(1.0, max(0.0, _as_float(app.v_cue_vol, saved.volume))),
    )
    log.info("test cue on device %r", cue.output_device)
    speech.play_cue(cue, cue.start_hz)


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

    # An empty table looks broken. Say why it's empty instead.
    app.history_empty = ttk.Label(
        frame,
        text=("Nothing yet. Messages appear here after a trigger completes.\n\n"
              "If you've used the trigger and this is still empty, the press "
              "isn't reaching PitRadio — check \"Listening for\" on the Status "
              "tab against what you expect.\n\n"
              "History is kept in memory only, so it starts empty every time "
              "the app launches."),
        foreground="#777", wraplength=680, justify="left",
    )

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
    app.notes_text = tk.Text(notes, wrap="word", height=12, state="disabled",
                             **theme.text_options(app.palette))
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
