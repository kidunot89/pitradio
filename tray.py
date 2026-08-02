"""System tray icon.

Closing the window hides it; this is what keeps the app reachable afterwards.
pystray's run() blocks and wants its own message loop, so it gets a thread of
its own — which means every menu callback arrives off the Tk thread and has to
be marshalled back through root.after before touching a widget.

The icon is drawn at runtime rather than shipped as .ico files, so it can
recolour to show state without carrying a set of near-identical binaries.
"""

from __future__ import annotations

import logging
import threading

from PIL import Image, ImageDraw

import state as state_mod

log = logging.getLogger(__name__)

COLOURS = {
    "idle": (86, 156, 214),
    "recording": (220, 68, 68),
    "busy": (232, 174, 58),
    "disabled": (120, 120, 120),
}


def icon_image(kind: str) -> Image.Image:
    """A microphone on a coloured disc.

    Drawn at 64px and scaled down by the tray, so the shapes are kept chunky
    and well separated — at 16px anything finer collapses into a blob. The
    gap between capsule and cradle is what makes it read as a microphone.
    """
    colour = COLOURS.get(kind, COLOURS["idle"])
    white = (255, 255, 255, 240)

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=colour)

    draw.rounded_rectangle((26, 14, 38, 34), radius=6, fill=white)
    draw.arc((20, 24, 44, 44), start=0, end=180, fill=white, width=4)
    draw.rectangle((30, 44, 34, 50), fill=white)
    draw.rectangle((23, 50, 41, 54), fill=white)
    return image


def _kind_for(status: str, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if status == state_mod.STATUS_RECORDING:
        return "recording"
    if status in (state_mod.STATUS_TRANSCRIBING, state_mod.STATUS_TYPING,
                  state_mod.STATUS_LOADING):
        return "busy"
    return "idle"


class Tray:
    def __init__(self, app):
        import pystray

        self.app = app
        self._pystray = pystray
        self._kind = "idle"
        self._thread: threading.Thread | None = None

        self.icon = pystray.Icon(
            "pitradio",
            icon_image("idle"),
            "PitRadio",
            menu=pystray.Menu(
                pystray.MenuItem("Show", self._show, default=True),
                pystray.MenuItem(
                    "Enabled", self._toggle_enabled,
                    checked=lambda _item: self.app.state.enabled,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Check for updates", self._check_updates),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            log.debug("tray stop failed", exc_info=True)

    def refresh(self, status: str, enabled: bool) -> None:
        kind = _kind_for(status, enabled)
        if kind != self._kind:
            self._kind = kind
            self.icon.icon = icon_image(kind)
        self.icon.title = f"PitRadio — {status}"
        self.icon.update_menu()

    # -- menu callbacks (tray thread) ------------------------------------

    def _on_ui(self, fn) -> None:
        self.app.root.after(0, fn)

    def _show(self, _icon=None, _item=None) -> None:
        self._on_ui(self.app.show_window)

    def _toggle_enabled(self, _icon=None, _item=None) -> None:
        self._on_ui(lambda: self.app.set_enabled(not self.app.state.enabled))

    def _check_updates(self, _icon=None, _item=None) -> None:
        self._on_ui(self.app.check_for_updates)

    def _quit(self, _icon=None, _item=None) -> None:
        self._on_ui(self.app.quit)
