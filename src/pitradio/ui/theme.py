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
    SMALL_FONT = ("Segoe UI", 9)
    VALUE_FONT = ("Segoe UI Semibold", 10)
    HEADING_FONT = ("Segoe UI Semibold", 11)
    STATUS_FONT = ("Segoe UI Semibold", 13)
    BRAND_FONT = ("Segoe UI Semibold", 15)
    MONO_FONT = ("Consolas", 9)
elif sys.platform == "darwin":
    BODY_FONT = ()
    SMALL_FONT = ("Helvetica Neue", 11)
    VALUE_FONT = ("Helvetica Neue", 13, "bold")
    HEADING_FONT = ("Helvetica Neue", 13, "bold")
    STATUS_FONT = ("Helvetica Neue", 15, "bold")
    BRAND_FONT = ("Helvetica Neue", 17, "bold")
    MONO_FONT = ("Menlo", 11)
else:
    BODY_FONT = ()
    SMALL_FONT = ("TkDefaultFont", 9)
    VALUE_FONT = ("TkDefaultFont", 10, "bold")
    HEADING_FONT = ("TkDefaultFont", 11, "bold")
    STATUS_FONT = ("TkDefaultFont", 13, "bold")
    BRAND_FONT = ("TkDefaultFont", 15, "bold")
    MONO_FONT = ("TkFixedFont", 9)


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


INDICATOR_PX = 15
# Transparent space to the right of the box. An image element ignores
# `indicatormargin`, so the gap between indicator and label has to be part of
# the artwork or the tick sits flush against the text.
INDICATOR_GAP_PX = 7


def _indicator_images(palette: Palette) -> dict:
    """Draw the checkbox indicator, because clam's is an X.

    clam renders a *cross* for the selected state, not a tick. On a control
    labelled "Enabled" that reads as "no" — it is the glyph that made the
    header checkbox look broken. The shape is baked into the theme's C
    element, so no amount of colour configuration reaches it; the only way to
    get a checkmark is to supply the artwork.

    Drawn at 4x and downsampled, so the tick has smooth edges at the 15px it
    is actually shown at. Returned rather than stored: the caller has to keep
    a reference, because Tk holds images weakly and a collected one leaves an
    empty square with no error.
    """
    from PIL import Image, ImageDraw, ImageTk

    scale = 4
    size = INDICATOR_PX * scale
    width = (INDICATOR_PX + INDICATOR_GAP_PX) * scale
    radius = 4 * scale

    def draw(fill: str, border: str, tick: str | None) -> ImageTk.PhotoImage:
        image = Image.new("RGBA", (width, size), (0, 0, 0, 0))
        pen = ImageDraw.Draw(image)
        pen.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius,
                              fill=fill, outline=border, width=scale)
        if tick:
            # Three points, not a font glyph: a checkmark drawn as two strokes
            # stays legible at 15px where a scaled-down "✓" turns to mush.
            pen.line([(size * 0.26, size * 0.52),
                      (size * 0.44, size * 0.70),
                      (size * 0.76, size * 0.30)],
                     fill=tick, width=int(scale * 1.8), joint="curve")
        return ImageTk.PhotoImage(image.resize(
            (INDICATOR_PX + INDICATOR_GAP_PX, INDICATOR_PX), Image.LANCZOS))

    return {
        "off": draw(palette.surface, palette.border, None),
        "on": draw(palette.accent, palette.accent, palette.accent_text),
        "off_hover": draw(palette.surface_alt, palette.text_muted, None),
        "on_hover": draw(palette.recording, palette.recording, palette.accent_text),
        "disabled": draw(palette.window, palette.border, None),
    }


def _install_indicator(root, style, palette) -> bool:
    """Swap the drawn indicator in for clam's. True if it took.

    Element names are per-interpreter and cannot be redefined, so each palette
    gets its own and a second call for the same one simply reuses it — which
    is what makes switching theme at runtime survivable.
    """
    from tkinter import TclError

    name = f"PitRadio{palette.name.capitalize()}.Checkbutton.indicator"
    images = getattr(root, "_pitradio_indicators", {})

    if palette.name not in images:
        try:
            drawn = _indicator_images(palette)
        except Exception as exc:
            # Pillow missing or a draw failure: keep clam's X rather than
            # losing the checkbox entirely.
            log.debug("could not draw the checkbox indicator: %s", exc)
            return False
        images[palette.name] = drawn
        # Held on the root: Tk keeps only a weak reference to a PhotoImage.
        root._pitradio_indicators = images
        try:
            style.element_create(
                name, "image", drawn["on"],
                ("disabled", drawn["disabled"]),
                ("!selected", "active", drawn["off_hover"]),
                ("selected", "active", drawn["on_hover"]),
                ("!selected", drawn["off"]),
                border=0, sticky="",
            )
        except TclError as exc:
            log.debug("could not create the indicator element: %s", exc)
            return False

    for widget in ("TCheckbutton", "TRadiobutton"):
        style.layout(widget, [
            (f"{widget[1:]}.padding", {"sticky": "nswe", "children": [
                (name, {"side": "left", "sticky": ""}),
                (f"{widget[1:]}.focus", {"side": "left", "sticky": "w",
                                         "children": [
                    (f"{widget[1:]}.label", {"sticky": "nswe"})]}),
            ]}),
        ])
    return True


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
    style.configure("Card.TFrame", background=palette.surface)
    style.configure("Header.TFrame", background=palette.surface)

    style.configure("TLabel", background=palette.window, foreground=palette.text)
    style.configure("Muted.TLabel", foreground=palette.text_muted)
    style.configure("Heading.TLabel", foreground=palette.text, font=HEADING_FONT)
    style.configure("Status.TLabel", foreground=palette.text, font=STATUS_FONT)
    style.configure("Ok.TLabel", foreground=palette.ok)
    style.configure("Warn.TLabel", foreground=palette.warn)
    style.configure("Danger.TLabel", foreground=palette.danger)
    style.configure("Recording.TLabel", foreground=palette.recording)

    # The three roles every tab needs, and the reason eighteen labels across the
    # UI used to carry a literal "#666". Hardcoding a grey is invisible in light
    # mode and unreadable in dark, where #333 on a #16181d window is very nearly
    # the same colour; test_gui_contracts now fails on any literal colour under
    # ui/. "Value" is deliberately full-contrast — it is the answer to the
    # question the label asks, so it should not be dimmer than the question.
    style.configure("Value.TLabel", foreground=palette.text, font=VALUE_FONT)
    style.configure("Hint.TLabel", foreground=palette.text_muted,
                    font=SMALL_FONT)
    style.configure("FieldLabel.TLabel", foreground=palette.text_muted)

    # On a card, which is a different background from the window. ttk has no
    # cascade, so a label inside a Card.TFrame keeps the window colour behind
    # its text unless it is told otherwise, and shows as a rectangle of the
    # wrong shade.
    for suffix in ("", "Muted.", "Heading.", "Status.", "Value.", "Hint."):
        style.configure(f"Card.{suffix}TLabel", background=palette.surface)
    style.configure("Card.Muted.TLabel", foreground=palette.text_muted)
    style.configure("Card.Hint.TLabel", foreground=palette.text_muted,
                    font=SMALL_FONT)
    style.configure("Card.Value.TLabel", foreground=palette.text, font=VALUE_FONT)
    style.configure("Card.Heading.TLabel", foreground=palette.text, font=HEADING_FONT)
    style.configure("Card.Status.TLabel", foreground=palette.text, font=STATUS_FONT)
    for name, colour in (("Ok", palette.ok), ("Warn", palette.warn),
                         ("Danger", palette.danger),
                         ("Recording", palette.recording)):
        style.configure(f"Card.{name}.TLabel", background=palette.surface,
                        foreground=colour)

    # The wordmark beside the logo in the header.
    style.configure("Brand.TLabel", background=palette.surface,
                    foreground=palette.text, font=BRAND_FONT)
    style.configure("BrandVersion.TLabel", background=palette.surface,
                    foreground=palette.text_muted, font=SMALL_FONT)

    # Window-coloured, not surface. ttk has no cascade: a plain ttk.Label
    # inside a group box keeps whatever background its own style says, so
    # filling the frame with `surface` left every label sitting in a
    # window-coloured rectangle. Fixing it the other way would mean a Card.
    # variant at several hundred call sites. The frame earns its shape from
    # the border and the heading weight instead.
    style.configure("TLabelframe", background=palette.window,
                    bordercolor=palette.border, relief="solid", borderwidth=1,
                    lightcolor=palette.border, darkcolor=palette.border)
    style.configure("TLabelframe.Label", background=palette.window,
                    foreground=palette.text, font=HEADING_FONT)

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
                    lightcolor=palette.surface_alt, darkcolor=palette.surface_alt,
                    focusthickness=1, padding=(12, 6), relief="flat",
                    borderwidth=1)
    style.map("TButton",
              background=[("active", palette.border), ("disabled", palette.window)],
              foreground=[("disabled", palette.text_muted)])

    style.configure("Accent.TButton", background=palette.accent,
                    foreground=palette.accent_text, bordercolor=palette.accent)
    style.map("Accent.TButton",
              background=[("active", palette.recording),
                          ("disabled", palette.surface_alt)],
              foreground=[("disabled", palette.text_muted)])

    # The option is `indicatorbackground`, not `indicatorcolor`. clam's
    # Checkbutton.indicator element offers indicatorsize, indicatormargin,
    # indicatorbackground, indicatorforeground, upperbordercolor and
    # lowerbordercolor — and nothing else. ttk ignores an option an element
    # does not declare, without raising, so mapping `indicatorcolor` did
    # exactly nothing and a ticked box was indistinguishable from an empty
    # one. Ask element_options() before writing a map; guessing the name
    # fails silently every time.
    style.configure("TCheckbutton", background=palette.window,
                    foreground=palette.text, focuscolor=palette.accent,
                    indicatorbackground=palette.surface,
                    indicatorforeground=palette.accent_text,
                    upperbordercolor=palette.border,
                    lowerbordercolor=palette.border,
                    indicatormargin=(0, 0, 7, 0), padding=(2, 3))
    style.map(
        "TCheckbutton",
        background=[("active", palette.window)],
        indicatorbackground=[
            # Order matters: ttk takes the first matching spec, so the
            # selected+hover pair has to precede the plain hover one or a
            # ticked box loses its fill the moment the pointer touches it.
            ("selected", "pressed", palette.recording),
            ("selected", "active", palette.recording),
            ("selected", palette.accent),
            ("active", palette.surface_alt),
            ("disabled", palette.window),
            ("!selected", palette.surface),
        ],
        indicatorforeground=[("selected", palette.accent_text),
                             ("disabled", palette.text_muted)],
        upperbordercolor=[("selected", palette.accent),
                          ("active", palette.text_muted)],
        lowerbordercolor=[("selected", palette.accent),
                          ("active", palette.text_muted)],
    )
    style.configure("Card.TCheckbutton", background=palette.surface)
    style.map("Card.TCheckbutton", background=[("active", palette.surface)])

    # Replaces clam's cross with a drawn tick. Falls back silently to the
    # stock indicator, which is wrong-looking but present.
    _install_indicator(root, style, palette)

    # Radiobuttons share the indicator element and the same trap.
    style.configure("TRadiobutton", background=palette.window,
                    foreground=palette.text, focuscolor=palette.accent,
                    indicatorbackground=palette.surface,
                    indicatorforeground=palette.accent_text,
                    upperbordercolor=palette.border,
                    lowerbordercolor=palette.border)
    style.map("TRadiobutton",
              background=[("active", palette.window)],
              indicatorbackground=[("selected", palette.accent),
                                   ("active", palette.surface_alt),
                                   ("!selected", palette.surface)])

    # borderwidth=0 on the notebook itself: clam draws a sunken frame around
    # the page area, and a 3D bevel around flat content is the other half of
    # what made this look like a 1998 dialog.
    style.configure("TNotebook", background=palette.window,
                    bordercolor=palette.window, borderwidth=0,
                    tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=palette.window,
                    foreground=palette.text_muted, padding=(16, 9),
                    bordercolor=palette.window, borderwidth=0,
                    lightcolor=palette.window, darkcolor=palette.window)
    # clam maps padding to "6 4 6 2" when selected against a base of
    # "6 2 6 2" — two extra pixels on top, which shifts the selected tab's
    # label down and makes the strip look misaligned as you click along it.
    # Only the colour should change, so padding is mapped back to the same
    # value it has unselected. Mapping `expand` instead does nothing: clam
    # leaves that map empty and never consults it.
    tab_padding = (16, 9, 16, 9)
    style.configure("TNotebook.Tab", padding=tab_padding)
    style.map("TNotebook.Tab",
              padding=[("selected", tab_padding), ("active", tab_padding)],
              background=[("selected", palette.surface),
                          ("active", palette.surface_alt)],
              foreground=[("selected", palette.text),
                          ("active", palette.text)],
              lightcolor=[("selected", palette.surface)],
              darkcolor=[("selected", palette.surface)])

    style.configure("TSeparator", background=palette.border)

    # Arrowless, flat, and no border: clam's default scrollbar has stepper
    # buttons at both ends and a raised bevel, which is the single most dated
    # thing on screen next to a modern window. The layout has to be replaced
    # to drop the arrows — configure() alone cannot remove an element.
    #
    # Both orientations are named explicitly. Configuring bare "TScrollbar"
    # looks like it works and does not: ttk resolves the orientation-prefixed
    # style first, and the log pane kept its stepper arrows for exactly that
    # reason.
    for orient in ("Vertical", "Horizontal"):
        with_suppressed(lambda o=orient: style.layout(
            f"{o}.TScrollbar",
            [(f"{o}.Scrollbar.trough", {
                "sticky": "ns" if o == "Vertical" else "ew",
                "children": [(f"{o}.Scrollbar.thumb",
                              {"expand": "1", "sticky": "nswe"})],
            })],
        ))
        style.configure(f"{orient}.TScrollbar",
                        background=palette.border,
                        troughcolor=palette.window,
                        bordercolor=palette.window,
                        lightcolor=palette.border, darkcolor=palette.border,
                        borderwidth=0, relief="flat",
                        arrowsize=0, width=10)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", palette.text_muted),
                              ("disabled", palette.window)])
    # lightcolor and darkcolor are the bevel clam draws on trough and bar.
    # Left at their defaults they are near-white, which is why the level meter
    # and the gain slider showed up as a pale beige strip in a dark window —
    # the two widgets that most obviously had never been themed.
    # Both the bare and the orientation-prefixed names. A widget created
    # without an explicit `orient=` does not always resolve to the prefixed
    # style, and configuring only "Horizontal.TProgressbar" left the level
    # meter drawing clam's default cream trough in a dark window.
    for name in ("TProgressbar", "Horizontal.TProgressbar",
                 "Vertical.TProgressbar"):
        style.configure(name, background=palette.accent,
                        troughcolor=palette.surface_alt,
                        bordercolor=palette.border,
                        lightcolor=palette.accent, darkcolor=palette.accent,
                        borderwidth=0, thickness=10)

    # `background` is the slider thumb's fill, not the widget's backdrop.
    # Leaving it at the window colour with an accent bevel drew the thumb as a
    # hollow red outline rather than a grip.
    # light/dark are the trough's bevel here, not the thumb's — setting them
    # to the accent outlined the whole track in red.
    for name in ("TScale", "Horizontal.TScale", "Vertical.TScale"):
        style.configure(name, background=palette.accent,
                        troughcolor=palette.surface_alt,
                        bordercolor=palette.border,
                        lightcolor=palette.border, darkcolor=palette.border,
                        borderwidth=0)
        style.map(name,
                  background=[("active", palette.recording),
                              ("disabled", palette.border)],
                  lightcolor=[("active", palette.recording)],
                  darkcolor=[("active", palette.recording)])

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
