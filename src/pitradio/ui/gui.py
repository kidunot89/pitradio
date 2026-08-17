"""Main window: status, live log, and the shell the other tabs hang off.

Closing the window hides it rather than exiting — the hook keeps running and
the tray icon is how you get back. Quit, from the tray or the File menu, is the
only thing that actually stops the app.

This module never imports winapi. That is what lets the whole GUI be launched
on a development Mac with `--gui-only` to check layout and the event pump,
which is otherwise untestable away from the target machine.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import ClassVar

from pitradio import i18n
from pitradio import state as state_mod
from pitradio.i18n import t
from pitradio.state import AppState
from pitradio.ui import gui_language, gui_settings, theme

log = logging.getLogger(__name__)

POLL_MS = 100

# Windows groups taskbar buttons by Application User Model ID, and takes the
# icon for the group from whatever it believes the application to be. A Python
# process that never sets one is identified by its host executable, so the
# button shows the interpreter's icon and pinning it pins the interpreter.
# Setting it before the first window exists is the whole requirement.
APP_USER_MODEL_ID = "AxisTaylor.PitRadio"


def _set_app_user_model_id() -> None:
    """Claim a taskbar identity. Windows only, and never fatal."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception as exc:
        # Costs a wrong taskbar icon, nothing more.
        log.debug("could not set the app user model id: %s", exc)


class App:
    def __init__(
        self,
        root: tk.Tk,
        store,
        app_state: AppState,
        version: str,
        *,
        worker=None,
        checker=None,
        recorder=None,
        transcriber=None,
        hook=None,
        plugins=None,
        voice=None,
        engineer=None,
        use_tray: bool = True,
    ):
        self.root = root
        self.store = store
        self.state = app_state
        self.version = version
        self.worker = worker
        self.checker = checker
        self.recorder = recorder
        self.transcriber = transcriber
        self.hook = hook
        self.plugins = plugins
        # None in --gui-only and in tests; every use site copes, like the rest.
        self.voice = voice
        self.engineer = engineer
        self.tray = None
        self._quitting = False
        self._log_lines = 0

        root.title(f"PitRadio {version}")
        root.minsize(720, 520)

        # Both before anything is built. Labels are read once when a widget is
        # created and ttk styles likewise, so either applied afterwards leaves
        # whatever already exists in the old language or the old colours.
        i18n.activate(store.config.gui.language)
        self.palette = theme.apply(root, store.config.gui.theme)
        # Findable from any widget, for helpers that are handed a
        # container rather than the App.
        root._pitradio_palette = self.palette
        _set_app_user_model_id()
        self._set_window_icon()
        if store.config.gui.geometry:
            # A saved geometry can be off-screen or malformed after a monitor
            # change; falling back to the default beats failing to open.
            with contextlib.suppress(tk.TclError):
                root.geometry(store.config.gui.geometry)
        root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._build()

        if use_tray:
            self._start_tray()

        if store.config.gui.start_minimized:
            root.after(200, self.hide_window)

        root.after(POLL_MS, self._drain)

    def _set_window_icon(self) -> None:
        """The mark, in the title bar and the taskbar.

        Windows takes the **`.ico`** route and everything else takes
        `iconphoto`, because on Windows they are not equivalent.
        `iconphoto` hands Tk a single bitmap per size and Tk sets WM_ICON from
        it; the taskbar then rescales whichever one it picked, and the button
        shows a soft, wrongly-sized mark next to every other pin on the bar.
        `iconbitmap(default=...)` passes a real multi-resolution ICO to
        Windows, which picks the exact size it wants — 16px for the title bar,
        32px for the taskbar, 256px for Alt-Tab — and draws it crisp.

        `default=` matters as much as the file does: without it the icon
        applies to this window only, and every dialog the app opens reverts to
        the stock Tk feather.
        """
        try:
            from pitradio import paths

            ico = paths.icon_path()
            if sys.platform == "win32" and ico is not None:
                self.root.iconbitmap(default=str(ico))
                return
        except Exception as exc:
            # Fall through to iconphoto rather than leaving no icon at all.
            log.debug("could not set the window icon from %s: %s", "the .ico", exc)

        try:
            from PIL import ImageTk

            from pitradio.ui import logo

            # Held on the instance: Tk keeps only a weak reference to a
            # PhotoImage, and a collected one leaves the default empty icon
            # with no error.
            self._icons = [ImageTk.PhotoImage(logo.draw(size))
                           for size in (16, 32, 48, 256)]
            self.root.iconphoto(True, *self._icons)
        except Exception as exc:
            # Cosmetic; never worth failing to open the window over.
            log.debug("could not set the window icon: %s", exc)

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        self.enabled_var = tk.BooleanVar(value=self.state.enabled)
        self.status_var = tk.StringVar(value=self.state.status)
        self.exe_var = tk.StringVar(value="—")
        self.profile_var = tk.StringVar(value="—")
        self.last_var = tk.StringVar(value="—")
        self.armed_var = tk.StringVar(value="—")
        self.last_trigger_var = tk.StringVar(value="not yet this session")

        self._build_header()

        self.warning_var = tk.StringVar(value="")
        self.warning_label = ttk.Label(
            self.root, textvariable=self.warning_var, style="Warn.TLabel",
            wraplength=900, justify="left", padding=(12, 0, 12, 6),
        )

        self.update_frame = ttk.Frame(self.root, padding=(12, 6))
        self.update_var = tk.StringVar(value="")
        ttk.Label(self.update_frame, textvariable=self.update_var).pack(side="left")
        ttk.Button(self.update_frame, text=t("Install now"),
                   command=self.install_update).pack(side="right")

        self.notebook = ttk.Notebook(self.root, padding=(0, 0))
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(10, 12))

        self._build_status_tab()
        gui_settings.build_settings_tab(self)
        gui_settings.build_profiles_tab(self)
        gui_language.build_language_tab(self)
        gui_settings.build_vocabulary_tab(self)
        gui_settings.build_audio_tab(self)
        gui_settings.build_voice_tab(self)
        gui_settings.build_engineer_tab(self)
        gui_settings.build_history_tab(self)
        gui_settings.build_updates_tab(self)

        self.refresh_armed()
        self._refresh_warnings()
        self._refresh_status_colour()

    def _build_header(self) -> None:
        """The band across the top: mark, wordmark, state, and the kill switch.

        A card surface rather than the window colour, with a hairline under it,
        so the chrome reads as one bar and the tabs below start a new region.
        The old header was the word "idle" alone on a wide empty strip, which
        gave the window nothing to identify itself with — this is the only
        place the logo appears at all.
        """
        self.header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 11))
        self.header.pack(fill="x")

        left = ttk.Frame(self.header, style="Header.TFrame")
        left.pack(side="left")

        # Held on the instance: Tk keeps only a weak reference to a PhotoImage,
        # and a collected one leaves an empty space with no error.
        self._brand_icon = None
        try:
            from PIL import ImageTk

            from pitradio.ui import logo

            self._brand_icon = ImageTk.PhotoImage(logo.draw(26))
            ttk.Label(left, image=self._brand_icon,
                      style="Card.TLabel").pack(side="left", padx=(0, 9))
        except Exception as exc:
            # The wordmark alone still identifies the window.
            log.debug("could not draw the header mark: %s", exc)

        ttk.Label(left, text="PitRadio", style="Brand.TLabel").pack(side="left")
        ttk.Label(left, text=self.version, style="BrandVersion.TLabel").pack(
            side="left", padx=(7, 0), pady=(4, 0))

        right = ttk.Frame(self.header, style="Header.TFrame")
        right.pack(side="right")

        ttk.Checkbutton(right, text=t("Enabled"), variable=self.enabled_var,
                        style="Card.TCheckbutton",
                        command=lambda: self.set_enabled(self.enabled_var.get())
                        ).pack(side="right", padx=(14, 0))

        # A dot plus a word, coloured by state. "Recording" in red at the top
        # of the window is readable from a driving position in a way that a
        # lowercase word in body text is not.
        self.status_dot = ttk.Label(right, text="●", style="Card.Muted.TLabel")
        self.status_dot.pack(side="right", padx=(0, 5))
        self.status_label = ttk.Label(right, textvariable=self.status_var,
                                      style="Card.Value.TLabel")
        self.status_label.pack(side="right")
        # Traced rather than refreshed at each call site: the dot and the word
        # are one piece of information, and any assignment that updated only
        # the word would leave them contradicting each other.
        self.status_var.trace_add("write",
                                  lambda *_: self._refresh_status_colour())

        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

    # Status name -> the style that colours the header dot.
    _STATUS_STYLES: ClassVar[dict[str, str]] = {
        state_mod.STATUS_RECORDING: "Card.Recording.TLabel",
        state_mod.STATUS_IDLE: "Card.Ok.TLabel",
    }

    def _refresh_status_colour(self) -> None:
        """Colour the header dot for the current state.

        Only the dot. Colouring the word as well makes a glance at the window
        read two things that say one thing, and a red word beside a red dot is
        louder than the information deserves.
        """
        style = "Card.Muted.TLabel"
        if self.state.enabled:
            style = self._STATUS_STYLES.get(self.status_var.get(),
                                            "Card.Warn.TLabel")
        theme.with_suppressed(lambda: self.status_dot.configure(style=style))

    def _build_status_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(frame, text=t("Status"))

        card = ttk.Frame(frame, style="Card.TFrame", padding=(14, 12))
        card.pack(fill="x")
        # Named so packaging/screenshots.py can crop to it.
        self.status_frame = card

        for row, (label, var) in enumerate(
            ((t("Listening for"), self.armed_var),
             (t("Last trigger"), self.last_trigger_var),
             (t("Focused app"), self.exe_var),
             (t("Profile in use"), self.profile_var),
             (t("Last message"), self.last_var)),
        ):
            ttk.Label(card, text=label, style="Card.Muted.TLabel",
                      width=15).grid(row=row, column=0, sticky="w", pady=3)
            # Value.TLabel, not a hardcoded grey: these are answers, and they
            # were previously dimmer than the questions beside them.
            ttk.Label(card, textvariable=var, style="Card.Value.TLabel").grid(
                row=row, column=1, sticky="w", pady=3)
        card.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=("\"Listening for\" is what the hook is armed with right now — not "
                  "what's saved. If it doesn't match Settings, the change hasn't "
                  "been applied. \"Last trigger\" updates the moment a press is "
                  "detected, so you can tell input from transcription problems."),
            style="Hint.TLabel", wraplength=880, justify="left",
        ).pack(fill="x", pady=(9, 12))

        log_frame = ttk.LabelFrame(frame, text=t("Log"), padding=(10, 8))
        log_frame.pack(fill="both", expand=True)

        # height=10, not 14: with expand=True the pane takes whatever is left
        # anyway, and the larger request forced the window taller than it
        # needed to be at its minimum size.
        self.log_text = tk.Text(log_frame, height=10, wrap="none", state="disabled",
                                **theme.text_options(self.palette),
                                font=theme.MONO_FONT, padx=8, pady=6)
        # One border, not two: the LabelFrame around this already draws one,
        # and text_options() adds a highlight ring meant for standalone panes.
        self.log_text.configure(highlightthickness=0)
        scroll = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y", padx=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text=t("Open log folder"),
                   command=gui_settings.open_log_folder).pack(side="left")
        ttk.Button(buttons, text=t("Open config folder"),
                   command=lambda: gui_settings.open_folder(self.store.path.parent)
                   ).pack(side="left", padx=8)
        ttk.Button(buttons, text=t("Quit"), command=self.quit).pack(side="right")

    def _start_tray(self) -> None:
        try:
            from pitradio.ui import tray as tray_mod

            self.tray = tray_mod.Tray(self)
            self.tray.start()
        except Exception as exc:
            log.warning("tray icon unavailable: %s", exc)
            self.tray = None

    # -- event pump ------------------------------------------------------

    def _drain(self) -> None:
        """Pull everything the worker/hook published since the last tick.

        Bounded per tick so a burst of log lines can't stall the UI thread.
        """
        for _ in range(200):
            try:
                kind, payload = self.state.events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, payload)
        self.root.after(POLL_MS, self._drain)

    def _handle(self, kind: str, payload) -> None:
        if kind == state_mod.EV_LOG:
            self._append_log(str(payload))
        elif kind == state_mod.EV_STATUS:
            snap = self.state.snapshot()
            self.status_var.set(snap["status"])
            self._refresh_status_colour()
            self.exe_var.set(snap["exe"] or "—")
            self.profile_var.set(snap["profile"])
            self.refresh_armed()
            if snap["status"] == state_mod.STATUS_RECORDING:
                # Stamped on detection, before any audio or transcription work,
                # so "did it see my key?" is answerable separately from "did it
                # transcribe?".
                self.last_trigger_var.set(time.strftime("%H:%M:%S"))
            if self.tray is not None:
                self.tray.refresh(snap["status"], snap["enabled"])
        elif kind == state_mod.EV_HISTORY:
            gui_settings.add_history_row(self, payload)
            self.last_var.set(payload.text if payload.typed else "(nothing sent)")
        elif kind == state_mod.EV_LEVEL:
            gui_settings.set_level(self, float(payload))
        elif kind == state_mod.EV_UPDATE:
            self._show_update(payload)

    def refresh_armed(self) -> None:
        """Show the binding the hook actually holds, plus any wheel button."""
        parts = []
        if self.hook is not None:
            parts.append(self.hook.describe_binding())
            if not self.hook.is_installed():
                parts.append("(hook not installed)")
        else:
            parts.append(self.store.config.trigger_key)

        if not self.state.enabled:
            parts.append("— disabled")
        self.armed_var.set(" ".join(parts))

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self._log_lines += 1

        limit = max(50, self.store.config.gui.log_lines)
        if self._log_lines > limit:
            self.log_text.delete("1.0", f"{self._log_lines - limit}.0")
            self._log_lines = limit

        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_warnings(self) -> None:
        messages = []
        if self.state.elevated is False:
            messages.append(
                "Not running as administrator. If a sim runs elevated, Windows will "
                "discard every keystroke this app sends and nothing will be typed — "
                "with no error."
            )
        messages.extend(f"Config: {p}" for p in self.state.config_problems)

        if messages:
            self.warning_var.set("\n".join(messages))
            self.warning_label.pack(fill="x", after=self.header)
        else:
            self.warning_label.pack_forget()

    def _show_update(self, info) -> None:
        if info is None:
            self.update_frame.pack_forget()
            return
        self.update_var.set(f"Version {info.version} is available.")
        self.update_frame.pack(fill="x", before=self.notebook)
        gui_settings.set_update_details(self, info)

    # -- actions ---------------------------------------------------------

    def set_enabled(self, value: bool) -> None:
        self.state.set_enabled(value)
        self.enabled_var.set(value)
        self._refresh_status_colour()
        self.store.config.enabled = value
        self.save_config()
        log.info("trigger key %s", "enabled" if value else "disabled (passing through)")

    def save_config(self) -> None:
        """Write the config out. The worker picks it up on its next trigger."""
        from pitradio import config as config_mod

        problems = self.store.config.validate()
        self.state.config_problems = problems
        try:
            config_mod.save(self.store.path, self.store.config)
        except OSError as exc:
            messagebox.showerror(t("PitRadio"), f"Could not save config:\n{exc}")
            return
        # Keep the store's mtime in step so this write doesn't read back as an
        # external edit on the next trigger. Deliberately not store.load():
        # that would swap in a fresh object graph and orphan every sub-object
        # the tabs are holding — see ConfigStore.mark_saved.
        self.store.mark_saved()
        self._apply_trigger_key()
        self._apply_action_bindings()
        self._refresh_warnings()
        if problems:
            log.warning("config saved with %d problem(s); see the banner", len(problems))
        else:
            log.info("config saved")

    def _apply_trigger_key(self) -> None:
        """Push a changed trigger key straight to the hook.

        The worker only re-reads config at the start of a trigger, so without
        this a new trigger key would not take effect until the *old* one was
        pressed. That is a trap: the usual reason to change it is that you can't
        press the old one — F13 doesn't exist on most keyboards — and the only
        way out would be restarting the app.
        """
        if self.hook is None:
            return
        from pitradio import keys

        try:
            mods, vk = keys.parse_trigger(self.store.config.trigger_key)
            self.hook.set_trigger(vk, mods)
            log.info("trigger key is now %s", self.store.config.trigger_key)
        except keys.KeyNameError as exc:
            log.error("keeping the previous trigger key: %s", exc)

    def _apply_action_bindings(self) -> None:
        """Arm the send and clear bindings.

        Applied on save for the same reason as the trigger: these act on a
        message that is waiting *now*, so leaving them to the worker's next
        reload would mean they do nothing exactly when they are wanted.

        Event kinds come from `state`, never from `hook` — importing hook here
        would drag winapi in and break `--gui-only` off Windows.
        """
        from pitradio import keys

        cfg = self.store.config
        if self.hook is not None:
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
                    log.error("ignoring the %s key %r: %s", kind, spec, exc)
                    continue
                actions[kind] = (vk, mods)
                log.info("%s key is now %s", kind, spec)
            self.hook.set_actions(actions)

    def reload_model(self) -> None:
        """Load the configured model now, off the Tk thread.

        Changing language has to take effect without a restart, and the
        transcriber otherwise keeps whichever model it loaded at startup until
        the worker happens to notice the config changed.
        """
        if self.transcriber is None:
            return

        import threading

        def work() -> None:
            try:
                self.transcriber.load(self.store.config.whisper)
            except Exception as exc:
                log.error("could not load %s: %s", self.store.config.whisper.model, exc)

        threading.Thread(target=work, name="model-reload", daemon=True).start()

    def check_for_updates(self) -> None:
        if self.checker is None:
            messagebox.showinfo(t("PitRadio"), t("Update checks are disabled in this run."))
            return
        log.info("checking for updates")
        self.checker.check_now()

    def install_update(self) -> None:
        info = self.state.pending_update
        if info is None:
            return
        from pitradio import updater

        if not updater.installed_via_installer():
            messagebox.showinfo(
                t("PitRadio"),
                "This copy was not installed with the installer, so it can't update "
                "itself. Download the new release from GitHub.",
            )
            return

        if not messagebox.askyesno(
            t("PitRadio"),
            f"Install version {info.version} now?\n\n"
            "PitRadio will close, update, and restart. Don't do this mid-session.",
        ):
            return

        def work():
            from pitradio import paths

            try:
                installer = updater.download(info, paths.log_dir().parent / "updates")
            except Exception as exc:
                # Bound to a local: `exc` is cleared when the except block ends,
                # so the lambda below would otherwise close over an unbound name.
                message = str(exc)
                log.error("update failed: %s", message)
                self.root.after(
                    0, lambda: messagebox.showerror(t("PitRadio"), f"Update failed:\n{message}")
                )
                return
            self.root.after(0, lambda: self._launch_installer(installer))

        import threading

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _launch_installer(self, installer) -> None:
        import logging as logging_mod
        import os

        from pitradio import paths, updater

        # The shim waits on this process id before touching a file, so exiting
        # promptly is part of the contract. Normal shutdown does the work —
        # saving config, stopping threads — and the hard exit afterwards is a
        # backstop against a lingering thread stalling the update indefinitely.
        updater.launch_installer(installer, paths.install_dir() / "pitradio.exe")
        self.quit()
        logging_mod.shutdown()
        os._exit(0)

    # -- window lifecycle ------------------------------------------------

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        if self.tray is None:
            # With no tray there would be no way back, so treat close as quit.
            self.quit()
            return
        self.root.withdraw()
        log.info("minimised to tray; the trigger key is still active")

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True

        try:
            self.store.config.gui.geometry = self.root.geometry()
            self.save_config()
        except Exception:
            log.debug("could not persist window geometry", exc_info=True)

        for component in (self.tray, self.checker, self.worker, self.voice,
                          self.engineer, self.hook, self.plugins):
            if component is None:
                continue
            try:
                component.stop()
            except Exception:
                log.debug("stopping %s failed", component, exc_info=True)

        self.root.quit()
        self.root.destroy()
