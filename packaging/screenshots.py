"""Generate the README's screenshots.

    python packaging/screenshots.py            # every shot
    python packaging/screenshots.py trigger    # just one

Each shot is cropped to a **named widget**, not to guessed pixel coordinates —
the window is asked where the control actually is, so a shot stays correct
after the layout moves. Run it on Windows: that is where the app runs and what
the README's readers will see, and ttk draws differently on every platform.

The window is real. It runs with no hook, no audio and no model, the way
`--gui-only` does, so nothing is recorded and nothing is typed anywhere.

Capture is per platform. Windows and macOS use `ImageGrab`; macOS needs Screen
Recording permission for the terminal, or the grab comes back as the desktop
wallpaper with no window in it — silently, with no error, so there is a check
for that below. X11 uses ImageMagick's `import`, which is what makes this work
under Xvfb in a container with no display and no permissions at all:

    docker run --rm -v "$PWD":/app -e PYTHONPATH=/app/src:/app/vendor IMAGE \
        xvfb-run -s "-screen 0 1400x900x24" python packaging/screenshots.py

The window is filled with plausible sample state first. It is the real UI —
nothing is drawn or faked — but an empty log and five em-dashes document
nothing, and the point of a screenshot is to show what the control looks like
in use.
"""

from __future__ import annotations

import contextlib
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

OUTPUT = ROOT / "docs" / "images"

# Tall enough that the Settings tab fits without scrolling, so every
# section can be cropped to rather than hunted for.
WINDOW = (1040, 1000)

# name -> (tab, attribute on App to crop to, padding in pixels)
# None means the whole window.
SHOTS = {
    "window": ("Status", None, 0),
    "status": ("Status", "status_frame", 12),
    "log": ("Status", "log_text", 8),
    "trigger": ("Settings", "trigger_frame", 12),
    "appearance": ("Settings", "appearance_frame", 12),
    "profiles": ("Profiles", None, 0),
    "language": ("Language", None, 0),
    "audio": ("Audio", None, 0),
    "history": ("History", None, 0),
    "updates": ("Updates", None, 0),
}


def _grab_screen():
    """The whole screen, however this platform allows.

    X11 goes through ImageMagick rather than ImageGrab: Pillow's X11 support
    needs an XCB backend that is not always compiled in, and `import` is
    present anywhere a virtual framebuffer is.
    """
    from PIL import Image, ImageGrab

    if sys.platform in ("win32", "darwin"):
        return ImageGrab.grab()

    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        target = handle.name
    subprocess.run(["import", "-window", "root", target],
                   check=True, stdin=subprocess.DEVNULL)
    return Image.open(target).convert("RGB")


def populate(app) -> None:
    """Fill the window with plausible state.

    The real UI, with sample data. A screenshot of five em-dashes and an empty
    log documents nothing — every label the docs point at needs something in
    it to point *at*.
    """
    import time

    from pitradio import state as state_mod

    app.state.set_context("le mans ultimate.exe", "le mans ultimate.exe")
    app.state.set_status(state_mod.STATUS_IDLE)
    app.status_var.set(state_mod.STATUS_IDLE)
    app.exe_var.set("le mans ultimate.exe")
    app.profile_var.set("le mans ultimate.exe")
    app.armed_var.set("f13")
    app.last_trigger_var.set(time.strftime("%H:%M:%S"))
    app.last_var.set("@G.Taylor box this lap")

    for entry in (
        "trigger: exe=le mans ultimate.exe profile=le mans ultimate.exe",
        "pre-keys sent (+2ms)",
        "session drivers: Geoff Taylor, Nyck de Vries, Nicolas Lapierre",
        "transcribed 1.84s of audio in 0.42s: 'Taylor box this lap'",
        "marked up driver names: '@G.Taylor box this lap'",
        "sent 24 chars in 2.31s total",
    ):
        app.log_text.configure(state="normal")
        app.log_text.insert("end", f"20:14:07 INFO    worker: {entry}\n")
        app.log_text.configure(state="disabled")
    app.log_text.see("end")


def _looks_like_a_desktop(image) -> bool:
    """Whether a capture is the wallpaper rather than the window.

    A screenshot of this GUI is flat colour and text: a few hundred distinct
    colours at most, and the container render measures under 200. A
    photographic wallpaper has thousands. Worth checking, because macOS
    returns the desktop rather than an error when Screen Recording permission
    is missing, and the result looks plausible until someone opens it.
    """
    sample = image.convert("RGB").resize((80, 50))
    colours = sample.getcolors(maxcolors=1_000_000) or []
    return len(colours) > 1500


def _scroll_into_view(target) -> None:
    """Bring a widget inside a scrolling pane into the visible area.

    The Settings tab scrolls, so a section far enough down is simply not on
    screen — and cropping to it then yields whatever *is* at those coordinates.
    The Appearance shot came out as a 45px sliver of the footer's Save button,
    which is the kind of thing that looks like a capture bug rather than a
    scroll position.
    """
    widget, canvas = target, None
    while True:
        parent = widget.winfo_parent()
        if not parent:
            return
        widget = widget._nametowidget(parent)
        if widget.winfo_class() == "Canvas":
            canvas = widget
            break

    body = next((child for child in canvas.winfo_children()), None)
    if body is None or not body.winfo_height():
        return

    offset = target.winfo_rooty() - body.winfo_rooty()
    # A little above the target, so it does not sit flush against the edge.
    canvas.yview_moveto(max(0.0, (offset - 12) / body.winfo_height()))
    canvas.update_idletasks()


def capture(app, root, name: str, tab: str, attribute: str | None, pad: int) -> Path:
    for tab_id in app.notebook.tabs():
        if app.notebook.tab(tab_id, "text") == tab:
            app.notebook.select(tab_id)
            break
    else:
        raise SystemExit(f"no tab called {tab!r}")

    root.update()
    root.update_idletasks()

    target = root if attribute is None else getattr(app, attribute, None)
    if target is not None and target is not root:
        _scroll_into_view(target)
        root.update()
        root.update_idletasks()
    if target is None:
        raise SystemExit(
            f"{name}: App has no attribute {attribute!r} to crop to. "
            f"Give the widget a name in the GUI, or use None for the whole window."
        )

    screen = _grab_screen()

    # Clamped to the window, not just the screen. A frame inside a scrolling
    # pane reports the width of its *content*, which can be wider than the
    # viewport showing it, and a frame below the fold reports coordinates that
    # are off the bottom entirely — cropping to either gives a garbled image
    # or an inverted box.
    window = (root.winfo_rootx(), root.winfo_rooty(),
              root.winfo_rootx() + root.winfo_width(),
              root.winfo_rooty() + root.winfo_height())
    box = (
        max(window[0], target.winfo_rootx() - pad),
        max(window[1], target.winfo_rooty() - pad),
        min(window[2], screen.width, target.winfo_rootx() + target.winfo_width() + pad),
        min(window[3], screen.height, target.winfo_rooty() + target.winfo_height() + pad),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise SystemExit(
            f"{name}: {attribute} is not visible in a {root.winfo_width()}x"
            f"{root.winfo_height()} window — it is scrolled out of view. "
            f"Make the window taller, or scroll it in before capturing."
        )
    image = screen.crop(box)

    if _looks_like_a_desktop(image):
        raise SystemExit(
            f"{name}: the capture is the desktop, not the window.\n"
            f"On macOS, grant Screen Recording permission to your terminal in\n"
            f"System Settings > Privacy & Security, then run this again."
        )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / f"{name}.png"
    image.save(path)
    print(f"wrote {path.relative_to(ROOT)}  ({image.width}x{image.height})")
    return path


def main() -> int:
    from pitradio import config as config_mod
    from pitradio import paths
    from pitradio import state as state_mod
    from pitradio.ui import gui

    wanted = sys.argv[1:] or list(SHOTS)
    unknown = [name for name in wanted if name not in SHOTS]
    if unknown:
        raise SystemExit(f"unknown shot(s): {', '.join(unknown)}\n"
                         f"available: {', '.join(SHOTS)}")

    store = config_mod.ConfigStore(paths.config_path())
    store.load()

    root = tk.Tk()
    root.geometry(f"{WINDOW[0]}x{WINDOW[1]}+20+20")
    app = gui.App(root, store, state_mod.AppState(), "", use_tray=False)
    populate(app)
    root.update()
    with contextlib.suppress(tk.TclError):
        root.lift()
        root.attributes("-topmost", True)
    root.update()
    root.update_idletasks()

    for name in wanted:
        tab, attribute, pad = SHOTS[name]
        capture(app, root, name, tab, attribute, pad)

    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
