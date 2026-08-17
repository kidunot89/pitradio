"""Reading iRacing's shared memory, as bytes.

Split from the plugin so all of it is **pure**: every function here takes a
`bytes` and returns a value. That is what lets the layout be tested without the
game — a block can be built in memory and parsed back, which is the only way to
check a field offset on a machine that does not have iRacing installed.

The format, briefly, because nothing else in this repository looks like it:

* A **header** of fixed size, giving where everything else lives.
* A **variable header** per telemetry channel — its name, type, offset and how
  many entries it has. Channels are discovered by name at runtime rather than
  by offset, which is why an iRacing update that adds a channel does not move
  the ones already being read.
* Up to four **telemetry buffers**, written in rotation. The one with the
  highest tick count is the newest complete frame, and reading any other is
  reading a frame that may be half-written.
* A **session string** of YAML, rewritten only when the session changes. Driver
  names live there, not in telemetry.

The rotation is the part that bites. Picking a buffer by index rather than by
tick count works perfectly until the moment the sim wraps round to it mid-read,
at which point a car's lap distance comes from one frame and its speed from
another — plausible numbers describing a moment that never happened.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

#: The mapping the sim publishes, and the size it publishes it at.
MEMORY_NAME = "Local\\IRSDKMemMapFileName"
MEMORY_SIZE = 1164 * 1024

#: `irsdk_header`: eleven ints and then the buffer table.
HEADER = struct.Struct("<12i")
HEADER_SIZE = HEADER.size
#: `irsdk_varBuf`: tick count, offset, two words of padding.
VAR_BUF = struct.Struct("<2i2i")
MAX_BUFFERS = 4

#: `irsdk_varHeader`: type, offset, count, a bool and padding, then three
#: fixed-width strings.
VAR_HEADER = struct.Struct("<3i4s32s64s32s")
VAR_HEADER_SIZE = VAR_HEADER.size

#: `irsdk_StatusField`: the one bit that says the sim is actually running.
STATUS_CONNECTED = 1

# irsdk_VarType, and how each unpacks.
CHAR, BOOL, INT, BITFIELD, FLOAT, DOUBLE = range(6)
_FORMATS = {CHAR: "c", BOOL: "?", INT: "i", BITFIELD: "I", FLOAT: "f",
            DOUBLE: "d"}
_WIDTHS = {CHAR: 1, BOOL: 1, INT: 4, BITFIELD: 4, FLOAT: 4, DOUBLE: 8}


class Header:
    """Where everything in the block is, this tick."""

    __slots__ = (
        "_raw",
        "buffer_length",
        "buffers",
        "count",
        "session_length",
        "session_offset",
        "session_update",
        "status",
        "tick_rate",
        "var_offset",
        "version",
    )

    def __init__(self, raw: bytes) -> None:
        (self.version, self.status, self.tick_rate, self.session_update,
         self.session_length, self.session_offset, self.count,
         self.var_offset, self.buffers, self.buffer_length,
         _pad1, _pad2) = HEADER.unpack_from(raw)
        self._raw = raw

    @property
    def connected(self) -> bool:
        return bool(self.status & STATUS_CONNECTED)

    def latest(self) -> int | None:
        """Offset of the newest complete telemetry buffer, or None.

        **By tick count, never by index.** The sim writes the buffers in
        rotation, so the newest is whichever was stamped last; taking a fixed
        one works until the sim wraps onto it mid-read, and then a car's lap
        distance comes from one frame and its speed from another. Nothing about
        the result looks wrong.
        """
        best_tick, best_offset = None, None
        for index in range(min(self.buffers, MAX_BUFFERS)):
            at = HEADER_SIZE + index * VAR_BUF.size
            if at + VAR_BUF.size > len(self._raw):
                break
            tick, offset, _p1, _p2 = VAR_BUF.unpack_from(self._raw, at)
            if best_tick is None or tick > best_tick:
                best_tick, best_offset = tick, offset
        return best_offset


class Channel:
    """One telemetry channel: what it is called, and where its values are."""

    __slots__ = ("count", "name", "offset", "type", "unit")

    def __init__(self, name: str, kind: int, offset: int, count: int,
                 unit: str) -> None:
        self.name = name
        self.type = kind
        self.offset = offset
        self.count = count
        self.unit = unit

    def __repr__(self) -> str:      # pragma: no cover - debugging only
        return f"<Channel {self.name} type={self.type} count={self.count}>"


def _text(raw: bytes) -> str:
    """A fixed-width field as a string, stopping at the first NUL."""
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def channels(raw: bytes, header: Header) -> dict[str, Channel]:
    """Every channel in the block, by name.

    Discovered rather than hardcoded, which is what makes this survive an
    iRacing update: channels are added and reordered between builds, and
    anything keyed on an offset would read the wrong field rather than fail.
    """
    found: dict[str, Channel] = {}
    for index in range(max(0, header.count)):
        at = header.var_offset + index * VAR_HEADER_SIZE
        if at + VAR_HEADER_SIZE > len(raw):
            break
        kind, offset, count, _pad, name, _desc, unit = VAR_HEADER.unpack_from(raw, at)
        label = _text(name)
        if label:
            found[label] = Channel(label, kind, offset, count, _text(unit))
    return found


def value(raw: bytes, buffer_offset: int, channel: Channel, index: int = 0):
    """One entry of a channel, or None if it is not readable.

    None rather than a default, throughout. A zero here is indistinguishable
    from a real reading of zero — a car stationary on the grid, a lap time not
    set — and the engineer would act on it.
    """
    if channel is None or index < 0 or index >= max(1, channel.count):
        return None
    fmt = _FORMATS.get(channel.type)
    if fmt is None:
        return None

    at = buffer_offset + channel.offset + index * _WIDTHS[channel.type]
    if at + _WIDTHS[channel.type] > len(raw):
        return None
    try:
        return struct.unpack_from("<" + fmt, raw, at)[0]
    except struct.error:
        return None


def values(raw: bytes, buffer_offset: int, channel: Channel) -> list:
    """A whole channel as a list. Entries that will not read come back None."""
    if channel is None:
        return []
    return [value(raw, buffer_offset, channel, index)
            for index in range(max(1, channel.count))]


def session_string(raw: bytes, header: Header) -> str:
    """The YAML session description, or an empty string."""
    start = header.session_offset
    end = start + max(0, header.session_length)
    if start <= 0 or end > len(raw) or end <= start:
        return ""
    return raw[start:end].split(b"\x00", 1)[0].decode("utf-8", "replace")


# -- the session string ---------------------------------------------------
#
# It is YAML, but pulling in a YAML parser for it would be a new dependency in
# a build that has already shipped four releases broken by one. The subset
# iRacing actually emits is small and rigid — two-space indentation, `key:
# value`, and lists of `- key: value` — so it is parsed here, and anything
# unexpected is skipped rather than raised over.


def parse_session(text: str) -> dict:
    """The session string as nested dicts and lists.

    Deliberately forgiving: a line that does not fit the shape is dropped. This
    is a ~200KB document from another process, most of which is of no interest,
    and refusing to read the driver list because a tyre compound contained a
    colon would be a poor trade.

    Recursive rather than a running stack, because of the one shape that makes
    the flat version wrong: iRacing indents a list *level with the key that
    introduced it*, not inside it —

        DriverInfo:
         Drivers:
         - CarIdx: 0
           UserName: A Driver

    so "this line is less indented, close the block" is not true of `- CarIdx`,
    and a stack that believes it drops the whole driver list.
    """
    lines = [line for line in text.splitlines()
             if line.strip() and not line.startswith("---")
             and not line.lstrip().startswith("#")]
    if not lines:
        return {}
    parsed, _index = _block(lines, 0, _indent(lines[0]))
    return parsed if isinstance(parsed, dict) else {}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(lines: list[str], index: int, indent: int):
    """One mapping or list, starting at `index`. Returns (value, next index)."""
    if index < len(lines) and lines[index].lstrip().startswith("- "):
        items: list = []
        while index < len(lines):
            line = lines[index]
            if _indent(line) < indent or not line.lstrip().startswith("- "):
                break
            item, index = _item(lines, index)
            items.append(item)
        return items, index

    mapping: dict = {}
    while index < len(lines):
        line = lines[index]
        here = _indent(line)
        if here < indent or line.lstrip().startswith("- "):
            break

        key, separator, raw = line.strip().partition(":")
        index += 1
        if not separator:
            continue
        key, raw = key.strip(), raw.strip()

        if raw:
            mapping[key] = _scalar(raw)
            continue

        # A bare key opens a block: either indented under it, or a list at the
        # same indent, which is how iRacing writes them.
        if index < len(lines) and (
            _indent(lines[index]) > here
            or (lines[index].lstrip().startswith("- ")
                and _indent(lines[index]) >= here)
        ):
            mapping[key], index = _block(lines, index, _indent(lines[index]))
        else:
            mapping[key] = ""
    return mapping, index


def _item(lines: list[str], index: int):
    """One `- key: value` entry, with the lines indented under it."""
    line = lines[index]
    here = _indent(line)
    # Rewritten without the dash so the entry parses as an ordinary mapping.
    block = [" " * (here + 2) + line.lstrip()[2:]]
    index += 1
    while index < len(lines) and _indent(lines[index]) > here:
        block.append(lines[index])
        index += 1
    parsed, _ = _block(block, 0, here + 2)
    return parsed, index


def _scalar(raw: str):
    """A YAML scalar, as int, float or string."""
    text = raw.strip()
    if text.startswith(("'", '"')) and text.endswith(("'", '"')) and len(text) > 1:
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
