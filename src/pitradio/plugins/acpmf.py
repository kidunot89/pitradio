"""Reading Assetto Corsa's shared memory pages, as bytes.

Split from the plugin so all of it is **pure** — every function takes a `bytes`
and returns a value — which is what lets the layout be exercised without any of
the three games installed.

Assetto Corsa publishes three memory-mapped pages rather than one block:
`acpmf_static` (what does not change during a session), `acpmf_graphics` (the
timing screen) and `acpmf_physics` (the car). Only the first two are needed
here.

**The layout is declared as a table, not as offsets.** Each page is a run of
fields in order, and the offset of every one is derived from the widths of
those before it. Writing `0x1A4` anywhere would be a number nobody could check
and that silently reads a different field when a game inserts something; a
table is at least readable next to the C struct it mirrors.

**Strings are UTF-16.** `wchar_t` on Windows is two bytes, so every name in
these pages is twice as long as it looks and decoding it as UTF-8 yields the
first character followed by rubbish. That is the mistake this format invites.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

#: The pages, and how much of each to map. The sizes are generous: the games
#: disagree about the tail of these structures and mapping more than exists is
#: harmless, while mapping less truncates the car coordinates.
STATIC_PAGE = "Local\\acpmf_static"
GRAPHICS_PAGE = "Local\\acpmf_graphics"
PHYSICS_PAGE = "Local\\acpmf_physics"
STATIC_SIZE = 4096
GRAPHICS_SIZE = 8192
PHYSICS_SIZE = 2048

#: `AC_STATUS`: 0 off, 1 replay, 2 live, 3 pause. Below live there is no
#: session to describe, and the page keeps its last contents when you leave it.
STATUS_LIVE = 2

#: How many cars the coordinate and id arrays hold, in every version of these
#: pages.
MAX_CARS = 60

# Field widths, by the letter used in the tables below.
_WIDTHS = {"i": 4, "f": 4, "?": 1}


def _wide(count: int) -> int:
    """Bytes taken by a `wchar_t[count]` on Windows."""
    return count * 2


#: `SPageFileStatic`, as far as the fields that are wanted. Each entry is
#: (name, kind, count); `w` is a wide string and its count is characters.
STATIC_LAYOUT = (
    ("smVersion", "w", 15),
    ("acVersion", "w", 15),
    ("numberOfSessions", "i", 1),
    ("numCars", "i", 1),
    ("carModel", "w", 33),
    ("track", "w", 33),
    ("playerName", "w", 33),
    ("playerSurname", "w", 33),
    ("playerNick", "w", 33),
    ("sectorCount", "i", 1),
)
# Everything past `sectorCount` in the real structure — the aids, the ERS
# fields, `trackSPlineLength`, `trackConfiguration` — is deliberately absent.
# The run to here is fixed-width and identical in all three games; past it sits
# a `bool` whose padding is a compiler's business, and a single byte of
# disagreement shifts every field after it. Reading a name from the wrong side
# of that returns something that looks like a track name and is not.

#: `SPageFileGraphic`. The run stops at the car arrays, which is as far as the
#: three games agree.
GRAPHICS_LAYOUT = (
    ("packetId", "i", 1),
    ("status", "i", 1),
    ("session", "i", 1),
    ("currentTime", "w", 15),
    ("lastTime", "w", 15),
    ("bestTime", "w", 15),
    ("split", "w", 15),
    ("completedLaps", "i", 1),
    ("position", "i", 1),
    ("iCurrentTime", "i", 1),
    ("iLastTime", "i", 1),
    ("iBestTime", "i", 1),
    ("sessionTimeLeft", "f", 1),
    ("distanceTraveled", "f", 1),
    ("isInPit", "i", 1),
    ("currentSectorIndex", "i", 1),
    ("lastSectorTime", "i", 1),
    ("numberOfLaps", "i", 1),
    ("tyreCompound", "w", 33),
    ("replayTimeMultiplier", "f", 1),
    ("normalizedCarPosition", "f", 1),
    ("activeCars", "i", 1),
    ("carCoordinates", "f", MAX_CARS * 3),
    ("carID", "i", MAX_CARS),
)

#: `SPageFilePhysics`, only as far as the speed. Read from a separate page
#: purely for that: the lap book records nothing from a car with no speed, so
#: without it the trainers would never see a single sample. These are the first
#: eight fields of the oldest structure of the three, which is as safe as an
#: offset gets in this format.
PHYSICS_LAYOUT = (
    ("packetId", "i", 1),
    ("gas", "f", 1),
    ("brake", "f", 1),
    ("fuel", "f", 1),
    ("gear", "i", 1),
    ("rpms", "i", 1),
    ("steerAngle", "f", 1),
    ("speedKmh", "f", 1),
)


def offsets(layout) -> dict[str, tuple[int, str, int]]:
    """name -> (offset, kind, count), derived from the field order."""
    found: dict[str, tuple[int, str, int]] = {}
    cursor = 0
    for name, kind, count in layout:
        found[name] = (cursor, kind, count)
        cursor += _wide(count) if kind == "w" else _WIDTHS[kind] * count
    return found


STATIC = offsets(STATIC_LAYOUT)
GRAPHICS = offsets(GRAPHICS_LAYOUT)
PHYSICS = offsets(PHYSICS_LAYOUT)
#: How much of each page has to be present for it to be worth reading.
STATIC_NEEDED = sum(
    _wide(count) if kind == "w" else _WIDTHS[kind] * count
    for _name, kind, count in STATIC_LAYOUT)
GRAPHICS_NEEDED = sum(
    _wide(count) if kind == "w" else _WIDTHS[kind] * count
    for _name, kind, count in GRAPHICS_LAYOUT)


def text(raw: bytes, table: dict, name: str) -> str:
    """A `wchar_t[]` field as a string, stopping at the first NUL.

    UTF-16, because `wchar_t` on Windows is two bytes. Decoding these as UTF-8
    returns the first letter of a track name followed by rubbish, which looks
    enough like a bad read to send somebody hunting for the wrong bug.
    """
    entry = table.get(name)
    if entry is None:
        return ""
    offset, kind, count = entry
    if kind != "w":
        return ""
    chunk = raw[offset:offset + _wide(count)]
    if len(chunk) < 2:
        return ""
    decoded = chunk.decode("utf-16-le", "replace")
    return decoded.split("\x00", 1)[0].strip()


def number(raw: bytes, table: dict, name: str, index: int = 0):
    """One numeric field, or None when it is not there to be read.

    None rather than zero throughout: zero is a real lap count, a real sector
    index and a real coordinate, so a default would be indistinguishable from a
    reading.
    """
    entry = table.get(name)
    if entry is None:
        return None
    offset, kind, count = entry
    if kind == "w" or index < 0 or index >= count:
        return None

    width = _WIDTHS[kind]
    at = offset + index * width
    if at + width > len(raw):
        return None
    try:
        return struct.unpack_from("<" + kind, raw, at)[0]
    except struct.error:
        return None


def coordinates(raw: bytes, slot: int) -> tuple[float, float, float] | None:
    """One car's world position, or None.

    Assetto Corsa's axes are (x, y, z) with y up, which is what the spotter and
    proximity both already assume — the same convention LMU uses.
    """
    values = []
    for axis in range(3):
        value = number(raw, GRAPHICS, "carCoordinates", slot * 3 + axis)
        if value is None:
            return None
        values.append(float(value))
    return (values[0], values[1], values[2])


def plausible(static: bytes, graphics: bytes) -> bool:
    """Whether these pages look like Assetto Corsa's at all.

    A cheap guard against the layout having moved under us, which is the real
    risk with three games sharing one set of page names and none of them
    versioning it. Wrong offsets do not raise — they produce a track called
    nothing, a grid of two hundred cars, and coordinates in the millions — so
    something has to look at the values and refuse.
    """
    if len(static) < STATIC_NEEDED or len(graphics) < GRAPHICS_NEEDED:
        return False

    cars = number(static, STATIC, "numCars")
    sectors = number(static, STATIC, "sectorCount")
    status = number(graphics, GRAPHICS, "status")
    laps = number(graphics, GRAPHICS, "completedLaps")

    return (
        cars is not None and 0 <= cars <= MAX_CARS
        and sectors is not None and 0 <= sectors <= 12
        and status is not None and 0 <= status <= 4
        and laps is not None and 0 <= laps <= 10000
    )
