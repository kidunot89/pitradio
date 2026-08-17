"""Reading the Project CARS 2 shared memory block, as bytes.

Pure, like the other readers here: every function takes a `bytes` and returns a
value, so the layout can be exercised without any of the games that speak it.

The format is one memory-mapped block, `$pcars2$`, and this reads only the head
of it:

    unsigned int mVersion
    unsigned int mBuildVersionNumber
    unsigned int mGameState
    unsigned int mSessionState
    unsigned int mRaceState
    int          mViewedParticipantIndex
    int          mNumParticipants
    ParticipantInfo mParticipantInfo[64]

**Only the head, and deliberately.** Everything past the participant array —
the timing arrays, the tyre and weather blocks — is where the layout is least
certain from the outside, and a wrong offset there does not raise. It produces
lap times of eleven hours and track lengths in the millions, which the engineer
would then talk about. What the head gives is enough: names, world positions,
lap distance **in metres**, race position and lap counts. Lap times are derived
from lap counts changing rather than read from a field this cannot vouch for.

`ParticipantInfo` has a `bool` at the front and a 64-byte name after it, so the
world position that follows is padded to a four-byte boundary. That padding is
the whole reason the offsets are written out below rather than summed from the
field widths: getting it wrong shifts every car's position by three bytes and
turns a grid into noise.
"""

from __future__ import annotations

import logging
import math
import struct

log = logging.getLogger(__name__)

MEMORY_NAME = "$pcars2$"
#: Generous: the real structure is several kilobytes and only its head is read,
#: but mapping short would truncate the participant array.
MEMORY_SIZE = 64 * 1024

#: `STORED_PARTICIPANTS_MAX`, in every version of this API.
MAX_PARTICIPANTS = 64
#: `STRING_LENGTH_MAX`.
NAME_LENGTH = 64

#: The head, up to the participant array.
HEADER = struct.Struct("<5I2i")
PARTICIPANTS_AT = HEADER.size

#: `GAME_INGAME_PLAYING` and friends. Below this there is no session; the block
#: keeps its last contents in the menus.
GAME_INGAME_PLAYING = 2

# ParticipantInfo, written out rather than summed. `mIsActive` is one byte,
# `mName` is 64 more, and `mWorldPosition` then aligns to four — so the name
# ends at 65 and the position starts at 68, not 65.
PARTICIPANT_SIZE = 100
_IS_ACTIVE = 0
_NAME = 1
_WORLD_POSITION = 68
_LAP_DISTANCE = 80
_RACE_POSITION = 84
_LAPS_COMPLETED = 88
_CURRENT_LAP = 92
_CURRENT_SECTOR = 96


class Header:
    """The head of the block."""

    __slots__ = ("build", "game_state", "participants", "race_state",
                 "session_state", "version", "viewed")

    def __init__(self, raw: bytes) -> None:
        (self.version, self.build, self.game_state, self.session_state,
         self.race_state, self.viewed, self.participants) = HEADER.unpack_from(raw)

    @property
    def playing(self) -> bool:
        """Whether there is a session, rather than a menu."""
        return self.game_state >= GAME_INGAME_PLAYING


class Participant:
    """One car, as the block describes it."""

    __slots__ = ("active", "lap", "lap_distance", "laps", "name", "place",
                 "position", "sector")

    def __init__(self, active, name, position, lap_distance, place, laps, lap,
                 sector) -> None:
        self.active = active
        self.name = name
        self.position = position
        self.lap_distance = lap_distance
        self.place = place
        self.laps = laps
        self.lap = lap
        self.sector = sector


def _text(raw: bytes) -> str:
    """A fixed-width name, stopping at the first NUL.

    Latin-1 rather than UTF-8: the field is `char[]` and the games put whatever
    the player typed in it, so a stray byte must cost a character rather than
    the whole grid. Latin-1 cannot fail.
    """
    return raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


def participant(raw: bytes, index: int) -> Participant | None:
    """One entry of the participant array, or None if it is not there."""
    if index < 0 or index >= MAX_PARTICIPANTS:
        return None
    at = PARTICIPANTS_AT + index * PARTICIPANT_SIZE
    if at + PARTICIPANT_SIZE > len(raw):
        return None

    try:
        active = bool(raw[at + _IS_ACTIVE])
        name = _text(raw[at + _NAME:at + _NAME + NAME_LENGTH])
        x, y, z = struct.unpack_from("<3f", raw, at + _WORLD_POSITION)
        distance = struct.unpack_from("<f", raw, at + _LAP_DISTANCE)[0]
        place = struct.unpack_from("<I", raw, at + _RACE_POSITION)[0]
        laps = struct.unpack_from("<I", raw, at + _LAPS_COMPLETED)[0]
        lap = struct.unpack_from("<I", raw, at + _CURRENT_LAP)[0]
        sector = struct.unpack_from("<i", raw, at + _CURRENT_SECTOR)[0]
    except struct.error:
        return None

    if not all(math.isfinite(value) for value in (x, y, z, distance)):
        return None
    return Participant(active, name, (x, y, z), float(distance), int(place),
                       int(laps), int(lap), int(sector))


def participants(raw: bytes, header: Header) -> list[Participant]:
    """Every active, named car. Order is the block's, which is the car index."""
    count = max(0, min(header.participants, MAX_PARTICIPANTS))
    found = []
    for index in range(count):
        entry = participant(raw, index)
        if entry is not None and entry.active and entry.name:
            found.append((index, entry))
    return found


def plausible(raw: bytes, header: Header) -> bool:
    """Whether this looks like the block it claims to be.

    A version check alone is not enough — the games do not all report the same
    one and a future build may bump it — so the values are checked instead.
    Wrong offsets here do not raise; they produce a grid of hundreds at
    coordinates in the millions, and something has to refuse that rather than
    hand it to the engineer.
    """
    if len(raw) < PARTICIPANTS_AT + PARTICIPANT_SIZE:
        return False
    if not 0 <= header.participants <= MAX_PARTICIPANTS:
        return False
    if not -1 <= header.viewed < MAX_PARTICIPANTS:
        return False

    for _index, entry in participants(raw, header)[:8]:
        if not 0 <= entry.place <= MAX_PARTICIPANTS:
            return False
        if not 0 <= entry.laps <= 10000:
            return False
        if not -1000.0 <= entry.lap_distance <= 100_000.0:
            return False
        if any(abs(axis) > 100_000.0 for axis in entry.position):
            return False
    return True
