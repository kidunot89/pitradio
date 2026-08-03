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
import time
import tkinter as tk
from tkinter import messagebox, ttk

import gui_language
import gui_settings
import state as state_mod
from state import AppState

log = logging.getLogger(__name__)

POLL_MS = 100


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
        joystick=None,
        plugins=None,
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
        self.joystick = joystick
        self.plugins = plugins
        self.tray = None
        self._quitting = False
        self._log_lines = 0

        root.title(f"PitRadio {version}")
        root.minsize(720, 520)
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

    # -- construction ----------------------------------------------------

    def _build(self) -> None:
        self.enabled_var = tk.BooleanVar(value=self.state.enabled)
        self.status_var = tk.StringVar(value=self.state.status)
        self.exe_var = tk.StringVar(value="—")
        self.profile_var = tk.StringVar(value="—")
        self.last_var = tk.StringVar(value="—")
        self.armed_var = tk.StringVar(value="—")
        self.last_trigger_var = tk.StringVar(value="not yet this session")

        self.header = ttk.Frame(self.root, padding=(12, 10))
        self.header.pack(fill="x")

        ttk.Label(self.header, textvariable=self.status_var,
                  font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Checkbutton(self.header, text="Enabled", variable=self.enabled_var,
                        command=lambda: self.set_enabled(self.enabled_var.get())
                        ).pack(side="right")

        self.warning_var = tk.StringVar(value="")
        self.warning_label = ttk.Label(
            self.root, textvariable=self.warning_var, foreground="#b34700",
            wraplength=900, justify="left", padding=(12, 0, 12, 6),
        )

        self.update_frame = ttk.Frame(self.root, padding=(12, 6))
        self.update_var = tk.StringVar(value="")
        ttk.Label(self.update_frame, textvariable=self.update_var).pack(side="left")
        ttk.Button(self.update_frame, text="Install now",
                   command=self.install_update).pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._build_status_tab()
        gui_settings.build_settings_tab(self)
        gui_settings.build_profiles_tab(self)
        gui_language.build_language_tab(self)
        gui_settings.build_vocabulary_tab(self)
        gui_settings.build_audio_tab(self)
        gui_settings.build_history_tab(self)
        gui_settings.build_updates_tab(self)

        self.refresh_armed()
        self._refresh_warnings()

    def _build_status_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Status")

        grid = ttk.Frame(frame)
        grid.pack(fill="x")
        for row, (label, var) in enumerate(
            (("Listening for", self.armed_var),
             ("Last trigger", self.last_trigger_var),
             ("Focused app", self.exe_var),
             ("Profile in use", self.profile_var),
             ("Last message", self.last_var)),
        ):
            ttk.Label(grid, text=f"{label}:", width=16).grid(row=row, column=0, sticky="w")
            ttk.Label(grid, textvariable=var, foreground="#333").grid(
                row=row, column=1, sticky="w")
        grid.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=("\"Listening for\" is what the hook is armed with right now — not "
                  "what's saved. If it doesn't match Settings, the change hasn't "
                  "been applied. \"Last trigger\" updates the moment a press is "
                  "detected, so you can tell input from transcription problems."),
            foreground="#666", wraplength=880, justify="left",
        ).pack(fill="x", pady=(10, 6))

        log_frame = ttk.LabelFrame(frame, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=14, wrap="none", state="disabled",
                                font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Open log folder",
                   command=gui_settings.open_log_folder).pack(side="left")
        ttk.Button(buttons, text="Open config folder",
                   command=lambda: gui_settings.open_folder(self.store.path.parent)
                   ).pack(side="left", padx=6)
        ttk.Button(buttons, text="Quit", command=self.quit).pack(side="right")

    def _start_tray(self) -> None:
        try:
            import tray as tray_mod

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

        joy = self.store.config.joystick
        if joy.device is not None and joy.button is not None:
            parts.append(f"or button {joy.button}")

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
        self.store.config.enabled = value
        self.save_config()
        log.info("trigger key %s", "enabled" if value else "disabled (passing through)")

    def save_config(self) -> None:
        """Write the config out. The worker picks it up on its next trigger."""
        import config as config_mod

        problems = self.store.config.validate()
        self.state.config_problems = problems
        try:
            config_mod.save(self.store.path, self.store.config)
        except OSError as exc:
            messagebox.showerror("PitRadio", f"Could not save config:\n{exc}")
            return
        # Keep the store's mtime in step so this write doesn't read back as an
        # external edit on the next trigger.
        self.store.load()
        self._apply_trigger_key()
        self._apply_joystick_binding()
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
        import keys

        try:
            mods, vk = keys.parse_trigger(self.store.config.trigger_key)
            self.hook.set_trigger(vk, mods)
            log.info("trigger key is now %s", self.store.config.trigger_key)
        except keys.KeyNameError as exc:
            log.error("keeping the previous trigger key: %s", exc)

    def _apply_joystick_binding(self) -> None:
        """Same reasoning as the trigger key: apply on save, not on next trigger."""
        if self.joystick is None:
            return
        joy = self.store.config.joystick
        self.joystick.set_binding(joy.device, joy.button)
        if joy.device is not None and joy.button is not None:
            log.info("joystick trigger: device %s button %s", joy.device, joy.button)

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
            messagebox.showinfo("PitRadio", "Update checks are disabled in this run.")
            return
        log.info("checking for updates")
        self.checker.check_now()

    def install_update(self) -> None:
        info = self.state.pending_update
        if info is None:
            return
        import updater

        if not updater.installed_via_installer():
            messagebox.showinfo(
                "PitRadio",
                "This copy was not installed with the installer, so it can't update "
                "itself. Download the new release from GitHub.",
            )
            return

        if not messagebox.askyesno(
            "PitRadio",
            f"Install version {info.version} now?\n\n"
            "PitRadio will close, update, and restart. Don't do this mid-session.",
        ):
            return

        def work():
            import paths

            try:
                installer = updater.download(info, paths.log_dir().parent / "updates")
            except Exception as exc:
                # Bound to a local: `exc` is cleared when the except block ends,
                # so the lambda below would otherwise close over an unbound name.
                message = str(exc)
                log.error("update failed: %s", message)
                self.root.after(
                    0, lambda: messagebox.showerror("PitRadio", f"Update failed:\n{message}")
                )
                return
            self.root.after(0, lambda: self._launch_installer(installer))

        import threading

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _launch_installer(self, installer) -> None:
        import logging as logging_mod
        import os

        import paths
        import updater

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

        for component in (self.tray, self.checker, self.worker, self.hook, self.joystick,
                          self.plugins):
            if component is None:
                continue
            try:
                component.stop()
            except Exception:
                log.debug("stopping %s failed", component, exc_info=True)

        self.root.quit()
        self.root.destroy()
