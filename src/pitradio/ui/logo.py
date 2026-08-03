"""The PitRadio mark.

One drawing, used for the window icon, the executable icon and the tray. Kept
in a module of its own rather than inside `tray.py` so the packaging step can
render it without importing pystray.

Drawn with primitives at a nominal 256px and scaled down, because the mark has
to survive 16px in a tray and a taskbar. Everything about the shape follows
from that: heavy forms, wide gaps, and as few elements as will still say what
the app does.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

# Matches theme.LIGHT_PALETTE.accent. Duplicated rather than imported because
# this module is used by packaging, which should not need the GUI.
BRAND = (200, 16, 46, 255)
BRAND_DARK = (150, 10, 34, 255)
WHITE = (255, 255, 255, 255)

# Tray states. The mark is the same; only the field colour changes, so the
# silhouette stays recognisable while the state is readable at a glance.
STATE_COLOURS = {
    "idle": BRAND,
    "recording": (230, 60, 80, 255),
    "busy": (200, 130, 40, 255),
    "disabled": (110, 116, 128, 255),
}

SIZE = 256


def draw(size: int = SIZE, state: str = "idle", rounded: bool = True) -> Image.Image:
    """The mark at `size` pixels square, on a transparent background.

    A chat bubble with a speaking mouth in it: what the app does, in one
    silhouette. An earlier microphone-with-transmission-arcs version needed a
    second, simplified drawing to stay legible below 40px; this one does not,
    because everything inside the bubble is a hole rather than a shape.
    """
    field = STATE_COLOURS.get(state, BRAND)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    if rounded:
        pen.rounded_rectangle((6, 6, 250, 250), radius=58, fill=field)
    else:
        pen.ellipse((6, 6, 250, 250), fill=field)

    # Bubble, with the tail low on the left so the mark sits visually centred
    # rather than looking as though it has slipped downwards.
    pen.rounded_rectangle((38, 48, 218, 176), radius=38, fill=WHITE)
    pen.polygon([(74, 170), (74, 222), (124, 170)], fill=WHITE)

    # An open mouth — the lower half of an ellipse, flat edge up — with sound
    # coming off its right. Both are knocked out of the bubble rather than
    # drawn on top of it, so there is only ever one light shape on one dark
    # field, which is what keeps the mark readable at sixteen pixels.
    # The chord fills the *lower* half of its box, so the box has to sit above
    # the optical centre for the mouth to line up with the waves: filled from
    # y=99 to y=125, centred on 112, which is where the arcs are centred too.
    pen.chord((78, 73, 138, 125), start=0, end=180, fill=field)
    for radius in (26, 50):
        pen.arc((150 - radius, 112 - radius, 150 + radius, 112 + radius),
                start=-56, end=56, fill=field, width=11)

    if size != SIZE:
        image = image.resize((size, size), Image.LANCZOS)
    return image


def icon_image(state: str = "idle") -> Image.Image:
    """The tray icon: 64px, which is what pystray asks for."""
    return draw(64, state=state)


def ico_sizes() -> list[tuple[int, int]]:
    """Every size Windows picks from, smallest first.

    16 and 32 are the tray and the title bar; 256 is what File Explorer shows
    at its largest setting. Omitting one makes Windows scale a neighbour and
    the result looks soft next to every other icon on the desktop.
    """
    return [(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
