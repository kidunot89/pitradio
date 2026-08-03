"""Light and dark palettes, and following the system preference.

tkinter has no theme support worth the name: ttk widgets take colours from a
style, plain tk widgets (Text, Listbox, Canvas) take them as options, and
nothing inherits. So every colour the app uses is named here once, and
`apply()` pushes it into both places.

Detection is per platform and deliberately quiet — a machine that will not
answer gets light, because a wrong guess is a cosmetic annoyance and an
exception at startup is not.

No Win32 import. This is `ui/`, which must stay importable off Windows;
`winreg` is standard library and imported inside the function that needs it.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"
MODES = (SYSTEM, LIGHT, DARK)


@dataclass(frozen=True)
class Palette:
    """Every colour the app draws with.

    Named by role rather than by shade, so a mode swap is a table lookup and
    nothing has to reason about whether "grey" is currently dark or light.
    """

    name: str
    window: str          # window and tab background
    surface: str         # cards, group boxes, entry fields
    surface_alt: str     # striped rows, the log pane
    border: str
    text: str
    text_muted: str      # hints beside a field
    accent: str          # buttons, selection, the focus ring
    accent_text: str     # text on top of accent
    ok: str
    warn: str
    danger: str
    recording: str


LIGHT_PALETTE = Palette(
    name=LIGHT,
    window="#f4f5f7",
    surface="#ffffff",
    surface_alt="#eceef1",
    border="#d3d7de",
    text="#1b1f27",
    text_muted="#606a7b",
    accent="#c8102e",        # the red used on the tray icon while recording
    accent_text="#ffffff",
    ok="#1a7f45",
    warn="#a2680a",
    danger="#b3261e",
    recording="#c8102e",
)

DARK_PALETTE = Palette(
    name=DARK,
    window="#16181d",
    surface="#1e2128",
    surface_alt="#252932",
    border="#333945",
    text="#e6e8ec",
    text_muted="#9aa4b4",
    accent="#e63c50",        # lifted, or it disappears against the dark surface
    accent_text="#ffffff",
    ok="#4ac47f",
    warn="#e0a33c",
    danger="#f2685e",
    recording="#e63c50",
)

PALETTES = {LIGHT: LIGHT_PALETTE, DARK: DARK_PALETTE}

# Segoe UI is the Windows system face and the one users expect; elsewhere the
# default is already right, so nothing is forced. An empty tuple means "leave
# the font alone", because ttk rejects a font of None.
if sys.platform == "win32":
    BODY_FONT = ("Segoe UI", 10)
    HEADING_FONT = ("Segoe UI Semibold", 11)
    STATUS_FONT = ("Segoe UI Semibold", 13)
elif sys.platform == "darwin":
    BODY_FONT = ()
    HEADING_FONT = ("Helvetica Neue", 13, "bold")
    STATUS_FONT = ("Helvetica Neue", 15, "bold")
else:
    BODY_FONT = ()
    HEADING_FONT = ("TkDefaultFont", 11, "bold")
    STATUS_FONT = ("TkDefaultFont", 13, "bold")


def system_prefers_dark() -> bool:
    """Whether the desktop is set to a dark appearance.

    False whenever the answer cannot be had. Every branch is guarded: this runs
    during startup, and no colour scheme is worth failing to open a window over.
    """
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            with key:
                # 0 means "use dark for apps"; the name reads backwards.
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0

        if sys.platform == "darwin":
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
                stdin=subprocess.DEVNULL,
            )
            # The key is absent entirely in light mode, so a non-zero exit is
            # the normal answer rather than a failure.
            return result.returncode == 0 and "dark" in result.stdout.lower()

        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0 and "dark" in result.stdout.lower()
    except Exception as exc:
        log.debug("could not read the system colour scheme: %s", exc)
        return False


def resolve(mode: str) -> Palette:
    """The palette to use for a configured mode."""
    if mode == DARK:
        return DARK_PALETTE
    if mode == LIGHT:
        return LIGHT_PALETTE
    return DARK_PALETTE if system_prefers_dark() else LIGHT_PALETTE


def apply(root, mode: str = SYSTEM) -> Palette:
    """Style every ttk widget class the app uses. Returns the palette applied.

    Plain tk widgets are not covered — they take colours as constructor options
    and there is no cascade, so each one is coloured at its call site from the
    palette this returns.
    """
    from tkinter import ttk

    palette = resolve(mode)
    style = ttk.Style(root)

    # 'clam' is the only built-in theme that honours background colour on every
    # widget class. The native themes on Windows and macOS draw their own
    # chrome and silently ignore most of what follows.
    with_suppressed(lambda: style.theme_use("clam"))

    root.configure(background=palette.window)

    base = {
        "background": palette.window,
        "foreground": palette.text,
        "fieldbackground": palette.surface,
        "bordercolor": palette.border,
        "lightcolor": palette.window,
        "darkcolor": palette.window,
        "focuscolor": palette.accent,
    }
    # Only when there is one to set: ttk rejects a font of None outright.
    if BODY_FONT:
        base["font"] = BODY_FONT
    style.configure(".", **base)

    style.configure("TFrame", background=palette.window)
    style.configure("TLabel", background=palette.window, foreground=palette.text)
    style.configure("Muted.TLabel", foreground=palette.text_muted)
    style.configure("Heading.TLabel", foreground=palette.text, font=HEADING_FONT)
    style.configure("Status.TLabel", foreground=palette.text, font=STATUS_FONT)
    style.configure("Ok.TLabel", foreground=palette.ok)
    style.configure("Warn.TLabel", foreground=palette.warn)
    style.configure("Danger.TLabel", foreground=palette.danger)
    style.configure("Recording.TLabel", foreground=palette.recording)

    style.configure("TLabelframe", background=palette.window,
                    bordercolor=palette.border, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=palette.window,
                    foreground=palette.text_muted)

    style.configure("TEntry", fieldbackground=palette.surface,
                    foreground=palette.text, bordercolor=palette.border,
                    insertcolor=palette.text, padding=4)
    style.configure("TCombobox", fieldbackground=palette.surface,
                    foreground=palette.text, background=palette.surface,
                    arrowcolor=palette.text, padding=3)
    style.map("TCombobox",
              fieldbackground=[("readonly", palette.surface)],
              foreground=[("readonly", palette.text)])

    style.configure("TButton", background=palette.surface_alt,
                    foreground=palette.text, bordercolor=palette.border,
                    focusthickness=1, padding=(10, 5), relief="flat")
    style.map("TButton",
              background=[("active", palette.border), ("disabled", palette.window)],
              foreground=[("disabled", palette.text_muted)])

    style.configure("Accent.TButton", background=palette.accent,
                    foreground=palette.accent_text, bordercolor=palette.accent)
    style.map("Accent.TButton",
              background=[("active", palette.recording),
                          ("disabled", palette.surface_alt)],
              foreground=[("disabled", palette.text_muted)])

    style.configure("TCheckbutton", background=palette.window,
                    foreground=palette.text, focuscolor=palette.accent)
    style.map("TCheckbutton",
              background=[("active", palette.window)],
              indicatorcolor=[("selected", palette.accent),
                              ("!selected", palette.surface)])

    style.configure("TNotebook", background=palette.window,
                    bordercolor=palette.border, tabmargins=(6, 4, 6, 0))
    style.configure("TNotebook.Tab", background=palette.window,
                    foreground=palette.text_muted, padding=(14, 7),
                    bordercolor=palette.border)
    style.map("TNotebook.Tab",
              background=[("selected", palette.surface)],
              foreground=[("selected", palette.text)])

    style.configure("TSeparator", background=palette.border)
    style.configure("TScrollbar", background=palette.surface_alt,
                    troughcolor=palette.window, bordercolor=palette.border,
                    arrowcolor=palette.text_muted)
    style.configure("Horizontal.TProgressbar", background=palette.accent,
                    troughcolor=palette.surface_alt, bordercolor=palette.border)

    style.configure("Treeview", background=palette.surface,
                    fieldbackground=palette.surface, foreground=palette.text,
                    bordercolor=palette.border, rowheight=24)
    style.configure("Treeview.Heading", background=palette.surface_alt,
                    foreground=palette.text_muted, relief="flat")
    style.map("Treeview",
              background=[("selected", palette.accent)],
              foreground=[("selected", palette.accent_text)])

    return palette


def listbox_options(palette: Palette) -> dict:
    """Colours for a plain tk widget, which ttk styles cannot reach."""
    return {
        "background": palette.surface,
        "foreground": palette.text,
        "selectbackground": palette.accent,
        "selectforeground": palette.accent_text,
        "highlightthickness": 1,
        "highlightbackground": palette.border,
        "highlightcolor": palette.border,
        "borderwidth": 0,
    }


def text_options(palette: Palette) -> dict:
    """As above, plus the caret colour — which a Listbox has no option for."""
    return {**listbox_options(palette), "insertbackground": palette.text}


def with_suppressed(fn) -> None:
    try:
        fn()
    except Exception:
        log.debug("theme call failed", exc_info=True)
