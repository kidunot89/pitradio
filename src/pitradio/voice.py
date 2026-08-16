"""Which room a session is, and who in it you can hear.

The decisions voice chat rests on, with none of the machinery — no sockets, no
audio, no `winapi`. Both of the things in here are wrong in ways that produce no
error: a session key that disagrees between two clients puts them in separate
rooms that each look like an empty session, and a proximity test that is subtly
wrong silences somebody you are racing. So they live where they can be tested.

See [docs/voice.md](../../docs/voice.md) for why the design is what it is.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from dataclasses import dataclass

#: Bumped when the key derivation changes. Clients computing different keys do
#: not fail — they land in different rooms, each of which looks like a session
#: where nobody else is running PitRadio. The salt makes an upgrade a clean
#: break rather than a silent half-migration.
KEY_VERSION = 1

#: Hex characters kept. Full SHA-256 in a URL is noise; 128 bits is far beyond
#: what guessing a room would need to be impractical.
KEY_LENGTH = 32


def session_key(server_address: int, server_port: int) -> str:
    """A room id every client on the same game server agrees on.

    Derived rather than announced: the relay is handed a hash, so it never
    learns which game server its users are on, and neither does anyone watching.
    The inputs are what the sim already knows about the server it connected to,
    so two clients agree without ever talking to each other.

    Offline and single-player have no server. That yields no key rather than a
    default one — a room shared by every person in the world who is not in a
    multiplayer session would be quite a thing to open a microphone into.
    """
    if not server_address or not server_port:
        return ""
    raw = f"pitradio/{KEY_VERSION}:{int(server_address)}:{int(server_port)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:KEY_LENGTH]


def is_session_key(value: str) -> bool:
    """Whether a string is shaped like one of ours.

    The relay puts this in a URL path, and a path segment arriving from the
    network is not to be trusted with anything — least of all the filesystem.
    """
    return (
        isinstance(value, str)
        and len(value) == KEY_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class Speaker:
    """Who said something, and where they were when they said it.

    The position travels with the clip so the listener measures against
    somewhere the speaker vouched for, rather than against whatever its own
    scoring block happened to hold — which for a car that just joined, or one
    the listener's block has not caught up with, is nothing at all.
    """

    driver: str
    #: None means "nobody said where", which is not the same as the origin —
    #: the origin is a real place on a track, and a car sitting on it would
    #: otherwise be permanently audible no matter how far away it got.
    position: tuple[float, float, float] | None = None


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Metres between two world positions."""
    return math.dist(a, b)


# -- the wire format -----------------------------------------------------
#
# A clip is a header and some audio. The relay never looks inside either — it
# checks the size and forwards the bytes — so this format is agreed between
# clients alone and changing it never needs the server redeployed.

#: Magic and version. Present so a frame that is not one of ours is rejected
#: rather than handed to the sound card as noise, which is what it would sound
#: like: a burst of static in somebody's headset mid-corner.
CLIP_MAGIC = b"PRV1"

#: Refuse anything larger. A push-to-talk clip is seconds of 16kHz mono; past
#: this it is a mistake or an attack, and either way not something to allocate.
MAX_CLIP_BYTES = 2_000_000

_HEADER = struct.Struct(">4sH")


def encode_clip(speaker: Speaker, audio: bytes, *, sent_at: float,
                fmt: str = "pcm16", rate: int = 16000) -> bytes:
    """One clip, ready to hand to the relay.

    The speaker's position travels with it. See `audible` for why the listener
    measures against this rather than against its own view of where they were.
    """
    header = json.dumps({
        "from": speaker.driver,
        "pos": list(speaker.position) if speaker.position is not None else None,
        "sent": round(float(sent_at), 3),
        "fmt": fmt,
        "rate": int(rate),
    }, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(CLIP_MAGIC, len(header)) + header + audio


@dataclass(frozen=True)
class Clip:
    speaker: Speaker
    audio: bytes
    sent_at: float = 0.0
    fmt: str = "pcm16"
    rate: int = 16000

    def age(self, now: float) -> float:
        """Seconds since it was recorded, never negative.

        Clocks disagree, and a clip stamped in somebody's future would
        otherwise read as infinitely fresh and outlive every cutoff.
        """
        return max(0.0, now - self.sent_at)


def decode_clip(frame: bytes) -> Clip | None:
    """A clip off the wire, or None if it is not one.

    **Returns None rather than raising, for everything.** This parses bytes from
    a stranger's machine on the audio path: a malformed frame must cost that
    clip and nothing else — not the connection, not the worker, and certainly
    not the trigger somebody is holding down at the time.
    """
    if not isinstance(frame, (bytes, bytearray)):
        return None
    if len(frame) > MAX_CLIP_BYTES or len(frame) < _HEADER.size:
        return None

    magic, header_length = _HEADER.unpack_from(frame)
    if magic != CLIP_MAGIC:
        return None

    start = _HEADER.size
    end = start + header_length
    if end > len(frame):
        return None

    try:
        header = json.loads(frame[start:end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(header, dict):
        return None

    driver = header.get("from")
    if not isinstance(driver, str) or not driver.strip():
        return None

    return Clip(
        speaker=Speaker(driver.strip(), _position(header.get("pos"))),
        audio=bytes(frame[end:]),
        sent_at=_number(header.get("sent")),
        fmt=str(header.get("fmt") or "pcm16"),
        rate=int(_number(header.get("rate")) or 16000),
    )


def _position(value) -> tuple[float, float, float] | None:
    """Three coordinates, or None for "nobody said".

    All three or none of them. Coercing one bad coordinate to zero and keeping
    the rest yields a position that looks entirely plausible and puts the
    speaker somewhere they have never been; None at least fails towards being
    heard, which is the recoverable direction.

    None rather than the origin, because the origin is a real place on a track.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float))
           for item in value):
        return None
    numbers = [float(item) for item in value]
    if any(math.isnan(n) or math.isinf(n) for n in numbers):
        return None
    return (numbers[0], numbers[1], numbers[2])


def _number(value) -> float:
    """A float, or zero. `bool` is excluded: it is an int and would read as 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def locate(
    speaker: Speaker,
    positions: dict[str, tuple[float, float, float]] | None = None,
) -> tuple[float, float, float] | None:
    """Where the speaker is, preferring what we can see over what they said.

    A clip arrives after somebody has finished talking, so the position it
    carries is a second or two old — at racing speed that is a hundred metres,
    and against a 200m radius it decides the answer. Our own scoring block has
    every car's position as of *now*, which is the one that matters: the
    question is who is near the target car when the message plays.

    The clip's own position is the fallback, for a speaker our block has not
    caught up with — somebody who just joined, or whose entry has gone. Better
    a stale position than none.

    Matched on driver name, which is the only identity shared between a clip
    and the scoring block; `mSteamID` reads zero in practice.
    """
    if positions:
        seen = positions.get(speaker.driver)
        if seen is not None:
            return seen
    return speaker.position


def audible(
    speaker: Speaker,
    listener_position: tuple[float, float, float] | None,
    *,
    proximity_only: bool,
    metres: int,
    positions: dict[str, tuple[float, float, float]] | None = None,
) -> bool:
    """Whether the listener should hear this clip.

    Decided here, on the listener's machine, from positions the sim already
    hands out for every car. Nothing about where anyone is is ever published,
    and the feature keeps working with a hostile relay.

    **Not knowing means hearing.** If there is no position for the listener —
    the sim closed, the player's car not in the block yet, a pause between
    sessions — the clip plays. Silence that cannot be explained is
    indistinguishable from the feature being broken, and a driver who cannot
    tell which one is happening will turn it off.
    """
    if not proximity_only:
        return True
    if listener_position is None or metres <= 0:
        return True
    where = locate(speaker, positions)
    if where is None:
        # Nobody knows where they are — not our block, not their own sim.
        # Same reasoning as an unknown listener: audible.
        return True
    return distance(where, listener_position) <= metres
