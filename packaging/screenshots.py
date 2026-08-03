"""Generate the README's screenshots.

    python packaging/screenshots.py            # every shot
    python packaging/screenshots.py trigger    # just one

Each shot is cropped to a **named widget**, not to guessed pixel coordinates —
the window is asked where the control actually is, so a shot stays correct
after the layout moves. Run it on Windows: that is where the app runs and what
the README's readers will see, and ttk draws differently on every platform.

The window is real. It runs with no hook, no audio and no model, the way
`--gui-only` does, so nothing is recorded and nothing is typed anywhere.

macOS needs Screen Recording permission for the terminal, or the capture comes
back as the desktop wallpaper with no window in it — silently, with no error.
There is a check for that below.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

OUTPUT = ROOT / "docs" / "images"

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


def _looks_like_a_desktop(image) -> bool:
    """Whether a capture is the wallpaper rather than the window.

    A screenshot of this GUI is flat colour and text: a few hundred distinct
    colours at most. A photographic wallpaper has thousands. Worth checking,
    because macOS returns the desktop rather than an error when Screen
    Recording permission is missing, and the result looks plausible until
    someone opens it.
    """
    sample = image.convert("RGB").resize((80, 50))
    colours = sample.getcolors(maxcolors=1_000_000) or []
    return len(colours) > 1500


def capture(app, root, name: str, tab: str, attribute: str | None, pad: int) -> Path:
    from PIL import ImageGrab

    for tab_id in app.notebook.tabs():
        if app.notebook.tab(tab_id, "text") == tab:
            app.notebook.select(tab_id)
            break
    else:
        raise SystemExit(f"no tab called {tab!r}")

    root.update()
    root.update_idletasks()

    target = root if attribute is None else getattr(app, attribute, None)
    if target is None:
        raise SystemExit(
            f"{name}: App has no attribute {attribute!r} to crop to. "
            f"Give the widget a name in the GUI, or use None for the whole window."
        )

    box = (
        target.winfo_rootx() - pad,
        target.winfo_rooty() - pad,
        target.winfo_rootx() + target.winfo_width() + pad,
        target.winfo_rooty() + target.winfo_height() + pad,
    )
    image = ImageGrab.grab(bbox=box)

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
    root.geometry("1040x680+60+60")
    app = gui.App(root, store, state_mod.AppState(), "", use_tray=False)
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    root.update()

    for name in wanted:
        tab, attribute, pad = SHOTS[name]
        capture(app, root, name, tab, attribute, pad)

    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
