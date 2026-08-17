"""Turning what the engineer wants to say into sound.

One thread, one utterance at a time, for the reason a real radio works that
way: two calls at once are two calls nobody heard. Everything else here is in
service of that queue staying honest.

**Fragments are resolved one at a time, pack first.** A voice pack supplies
recorded takes for the fixed phrases; anything it has never heard of — a
driver's name, a lap time in a language whose numbers are read as digits —
falls through to the synthesiser. Mixing the two inside one sentence is
audible, and it is still the right trade: the alternative is a pack that goes
unused the moment a driver's name appears in a call, which is most of them.

**Stale calls are dropped, not spoken.** Racing information has a shelf life
measured in corners. A comparison for turn four that reaches the driver on the
run to turn seven is not merely late, it is wrong — they will hear a corner
number and think about the wrong piece of track.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from pitradio.engineer import packs, tts

log = logging.getLogger(__name__)

#: Calls waiting to be spoken. Short: a backlog means the engineer is talking
#: more than the driver can listen to, and the fix is to say less, not to
#: remember more.
QUEUE_SIZE = 6

#: How long a call may sit in the queue before it is no longer worth saying.
MAX_AGE = 6.0

#: Urgency. A spotter call about a car alongside cannot wait behind a comment
#: about the last corner; everything else is ordinary.
URGENT, NORMAL = 0, 1


@dataclass
class VoiceSettings:
    """What the engineer currently sounds like.

    Held rather than read from the config each time, because resolving a
    persona to an installed voice means asking Windows what it has, and that is
    not a question to answer inside the speaking loop.
    """

    voice: str = ""
    rate: int = 0
    pack: packs.VoicePack | None = None


@dataclass(order=True)
class _Queued:
    priority: int
    sequence: int
    queued_at: float = field(compare=False)
    utterance: list[str] = field(compare=False, default_factory=list)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A WAV file as mono float32, whatever it was written as.

    Hand-parsed rather than left to `wave`, which raises on anything that is
    not integer PCM. That matters: `crew-chief-autovoicepack` writes 32-bit
    float, so the standard library cannot read the exact files this feature
    exists to support.

    Returns an empty array for anything unreadable. A pack with one corrupt
    clip in it must cost that clip.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        log.debug("could not read %s: %s", path, exc)
        return np.zeros(0, dtype=np.float32), 0

    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return np.zeros(0, dtype=np.float32), 0

    fmt: tuple[int, int, int, int] | None = None
    offset = 12
    while offset + 8 <= len(raw):
        chunk = raw[offset:offset + 4]
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        body = raw[offset + 8:offset + 8 + size]
        # Chunks are word-aligned; an odd size carries a pad byte after it.
        offset += 8 + size + (size % 2)

        if chunk == b"fmt " and len(body) >= 16:
            fmt = (
                int.from_bytes(body[0:2], "little"),      # format tag
                int.from_bytes(body[2:4], "little"),      # channels
                int.from_bytes(body[4:8], "little"),      # sample rate
                int.from_bytes(body[14:16], "little"),    # bits per sample
            )
        elif chunk == b"data" and fmt is not None:
            return _samples(body, fmt)
    return np.zeros(0, dtype=np.float32), 0


def _samples(body: bytes, fmt: tuple[int, int, int, int]) -> tuple[np.ndarray, int]:
    tag, channels, rate, bits = fmt
    channels = max(1, channels)

    if tag == 3 and bits == 32:                       # IEEE float
        data = np.frombuffer(body[:len(body) - len(body) % 4], dtype="<f4")
    elif tag == 1 and bits == 16:
        data = np.frombuffer(body[:len(body) - len(body) % 2], dtype="<i2")
        data = data.astype(np.float32) / 32768.0
    elif tag == 1 and bits == 8:
        # 8-bit WAV is unsigned, which is the one that silently comes out as
        # a loud buzz if it is treated like every other depth.
        data = np.frombuffer(body, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif tag == 1 and bits == 32:
        data = np.frombuffer(body[:len(body) - len(body) % 4], dtype="<i4")
        data = data.astype(np.float32) / 2147483648.0
    else:
        log.debug("unsupported WAV: format %d, %d bits", tag, bits)
        return np.zeros(0, dtype=np.float32), 0

    mono = data.astype(np.float32)
    if channels > 1:
        usable = len(mono) - (len(mono) % channels)
        mono = mono[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(mono), rate


def resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    """Linear resampling, so fragments from different sources join up.

    A pack take at 24kHz and a synthesised name at 22.05kHz have to be played
    as one sentence; concatenating them untouched plays one of them at the
    wrong pitch. Linear is crude and entirely adequate for speech at these
    ratios — the alternative is a resampling dependency for a difference
    nobody can hear over an engine.
    """
    if source <= 0 or target <= 0 or source == target or audio.size == 0:
        return audio
    count = round(audio.size * target / source)
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    position = np.linspace(0.0, audio.size - 1, count, dtype=np.float64)
    return np.interp(position, np.arange(audio.size), audio).astype(np.float32)


#: A breath between fragments. Without it "turn four" and "Taylor" run together
#: into one word; with much more they stop sounding like one sentence.
GAP_SECONDS = 0.06


def join(clips: list[tuple[np.ndarray, int]]) -> tuple[np.ndarray, int]:
    """Fragments into one utterance at one sample rate."""
    usable = [(audio, rate) for audio, rate in clips if audio.size and rate > 0]
    if not usable:
        return np.zeros(0, dtype=np.float32), 0

    rate = usable[0][1]
    gap = np.zeros(int(rate * GAP_SECONDS), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index, (audio, source) in enumerate(usable):
        if index:
            parts.append(gap)
        parts.append(resample(audio, source, rate))
    return np.concatenate(parts), rate


class Speaker(threading.Thread):
    """The engineer's mouth: a queue, a resolver, and an output device.

    `config` is a callable returning the live `EngineerConfig`, so volume and
    output device follow the Settings tab without a restart. What the voice
    *is* comes through `configure` instead, because resolving it asks Windows
    what is installed and that is not work for this thread's hot path.
    """

    def __init__(self, config, *, host: tts.SapiHost | None = None, play=None):
        super().__init__(name="engineer-speech", daemon=True)
        self._config = config
        self._host = host if host is not None else tts.SapiHost()
        # Injected so the queue, the resolution and the mixing can all be
        # tested without a sound card.
        self._play = play or _play
        self._queue: queue.PriorityQueue[_Queued] = queue.PriorityQueue(QUEUE_SIZE)
        self._settings = VoiceSettings()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._sequence = 0

    # -- what it sounds like ----------------------------------------------

    def configure(self, settings: VoiceSettings) -> None:
        with self._lock:
            self._settings = settings

    @property
    def settings(self) -> VoiceSettings:
        with self._lock:
            return self._settings

    # -- queueing ---------------------------------------------------------

    def say(self, utterance: list[str], *, urgent: bool = False) -> None:
        """Queue a call. Never blocks and never raises.

        Called from the polling thread with a sim read in progress and from the
        worker with somebody's trigger held down, so it does neither.
        """
        words = [str(part).strip() for part in utterance if str(part).strip()]
        if not words or self._stopping.is_set():
            return

        with self._lock:
            self._sequence += 1
            item = _Queued(URGENT if urgent else NORMAL, self._sequence,
                           time.monotonic(), words)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            log.debug("the engineer is behind; dropped %r", " ".join(words))

    def clear(self) -> None:
        """Throw away everything waiting.

        What stopping a routine has to do. A routine that has been stood down
        must not go on talking about the last four corners, which is exactly
        what a driver reaches for the stop phrase to make it stop doing.
        """
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            waited = time.monotonic() - item.queued_at
            if waited > MAX_AGE:
                log.debug("dropped a call that waited %.1fs: %r",
                          waited, " ".join(item.utterance))
                continue

            try:
                self._speak(item.utterance)
            except Exception:
                # The engineer going quiet is a nuisance. This thread dying
                # means it never comes back for the rest of the session.
                log.exception("saying %r failed", " ".join(item.utterance))

    def _speak(self, utterance: list[str]) -> None:
        settings = self.settings
        clips = [self._resolve(fragment, settings) for fragment in utterance]
        audio, rate = join([clip for clip in clips if clip is not None])
        if audio.size == 0:
            log.debug("nothing to play for %r", " ".join(utterance))
            return
        self._play(audio, rate, self._config())

    def _resolve(
        self, fragment: str, settings: VoiceSettings
    ) -> tuple[np.ndarray, int] | None:
        """One fragment as audio: a recorded take, or a synthesised one."""
        pack = settings.pack
        if pack is not None:
            take = pack.take(fragment)
            if take is not None:
                audio, rate = read_wav(take)
                if audio.size:
                    return audio, rate
                log.debug("%s is unreadable; synthesising %r instead", take, fragment)

        rendered = self._host.synthesize(
            fragment, voice=settings.voice, rate=settings.rate)
        if rendered is None:
            return None
        audio, rate = read_wav(rendered)
        return (audio, rate) if audio.size else None

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self.clear()
        if self.is_alive():
            self.join(timeout=timeout)
        self._host.close()


def _play(audio: np.ndarray, rate: int, engineer_cfg) -> None:
    """Out of the headset, on the configured device.

    Goes through the same helper as voice chat and for the same reason: one
    place decides how a device name becomes a device index, and the engineer
    lands wherever the driver put everything else.
    """
    from pitradio import speech

    speech.play_clip(audio, rate, engineer_cfg)
