"""The one place sound leaves this app.

Everything audible — the push-to-talk cues, the engineer, voice chat — goes
through a single `Output`. That is not tidiness. It is the fix for a class of
bug this app kept producing, and each of the four reasons below was a real
silence somebody had to diagnose from the outside.

**One stream, opened once.** Every sound used to call `sd.play()`, which opens
a device, writes, and closes it again. Opening a WASAPI endpoint is neither
fast nor certain: it is a negotiation, and it fails when the format has moved
or the device is busy at that instant. Doing it per beep means rolling the dice
per beep, while a stream held open is negotiated once and then simply written
to.

**The rate is the device's, and it is read back rather than assumed.** WASAPI
shared mode accepts *only* the endpoint's configured rate and refuses anything
else outright — `Invalid sample rate [PaErrorCode -9997]`. The cue built a
44.1kHz tone and handed it straight over; the TV runs at 48kHz; the error was
swallowed at debug level and the symptom was silence with a correct-looking
settings screen. Asking `query_devices` first is not enough either, because
what it reports and what the endpoint will actually accept can differ. So the
stream is opened, PortAudio negotiates, and whatever `stream.samplerate` comes
back is what everything is resampled to.

**Shared mode is asked for explicitly.** `WasapiSettings(exclusive=False)`. A
dictation app that seized an output device would silence the game it exists to
talk over, and the default is not something to leave to chance on somebody
else's machine.

**MME is avoided rather than merely deprioritised.** Its writes succeed and
produce no sound while another process holds the endpoint — no error, nothing
logged. `speech.resolve_device` ranks host APIs so WASAPI wins; this is where
that ranking finally matters, because a stream that is open is a stream that
had to negotiate honestly.

Threading: `play` blocks its caller until the sound has been heard, because two
clips at once are two clips nobody understood, and the callers already own
queues built on that. The callback itself never blocks and never allocates
beyond a slice — it runs on PortAudio's own thread and stalling it is a dropout.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: How long to wait for a sound to finish before giving up on it.
#:
#: A ceiling, not a timing mechanism — playback ends when the buffer drains.
#: This is only here so a stream that has died under us cannot hang the
#: engineer's speaking thread for the rest of the session.
MAX_WAIT_SECONDS = 30.0


def _sd():
    """sounddevice, imported late.

    Kept out of module scope so this file is importable — and testable —
    without a sound card, which is most of where this gets developed.
    """
    import sounddevice as sd

    return sd


class Output:
    """A held-open output device that everything writes into.

    `device` is a callable returning the configured device, so the window can
    change it without anybody holding a stale handle. The stream is reopened
    when the answer changes, and not otherwise.
    """

    def __init__(self, device, *, open_stream=None) -> None:
        self._device = device
        # Injected so the mixing, the resampling and the reopen logic can all
        # be tested without PortAudio.
        self._open = open_stream or _open_stream
        self._lock = threading.Lock()
        self._stream = None
        self._opened_for: Any = object()      # nothing equals this
        self._rate = 0
        self._channels = 1

        #: What is left to play, and the event that says it has all gone.
        self._pending = np.zeros(0, dtype=np.float32)
        self._drained = threading.Event()
        self._drained.set()

    # -- the device -------------------------------------------------------

    def _ensure(self) -> bool:
        """Open the stream if it is not open, or if the choice has changed."""
        wanted = self._device() if callable(self._device) else self._device
        if self._stream is not None and wanted == self._opened_for:
            return True

        self._close_locked()
        try:
            stream, rate, channels = self._open(wanted, self._callback)
        except Exception as exc:
            # **Warned, not debugged.** This is the only signal that sound is
            # going nowhere, and at debug level it was invisible for months.
            log.warning("could not open audio output %r: %s", wanted, exc)
            return False

        self._stream, self._opened_for = stream, wanted
        self._rate, self._channels = rate, channels
        log.info("audio output open: %s at %dHz, %d channel(s)",
                 wanted or "system default", rate, channels)
        return True

    def _close_locked(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            log.debug("closing the output stream failed", exc_info=True)
        self._stream = None
        self._opened_for = object()

    def close(self) -> None:
        with self._lock:
            self._pending = np.zeros(0, dtype=np.float32)
            self._drained.set()
            self._close_locked()

    @property
    def rate(self) -> int:
        """What the device is running at, or 0 while it is not open."""
        return self._rate

    # -- the callback -----------------------------------------------------

    def _callback(self, out, frames, _time, status) -> None:
        """PortAudio's thread. Never blocks, never waits on anything.

        `status` carries underflows, which are worth knowing about but not
        worth a log line each — at a few hundred callbacks a second that is a
        log nobody can read.
        """
        if status:
            log.debug("output stream status: %s", status)
        with self._lock:
            take = min(frames, len(self._pending))
            if take:
                # Mono into however many channels the device opened with.
                out[:take] = self._pending[:take, None]
                self._pending = self._pending[take:]
            if take < frames:
                out[take:] = 0.0
            if len(self._pending) == 0:
                self._drained.set()

    # -- playing ----------------------------------------------------------

    def play(self, audio: np.ndarray, rate: int, *, volume: float = 1.0,
             interrupt: bool = False) -> None:
        """Play a clip, blocking until it has been heard.

        `interrupt` replaces whatever is playing rather than queueing behind
        it. That is what a spotter call needs: the entire content of "car left"
        is *right now*, and waiting out a lap time means describing a car that
        has been passed.
        """
        if audio is None or getattr(audio, "size", 0) == 0:
            return

        with self._lock:
            if not self._ensure():
                return
            samples = _prepare(audio, int(rate) or 16000, self._rate, volume)
            if samples.size == 0:
                return
            self._pending = samples if interrupt else np.concatenate(
                (self._pending, samples))
            self._drained.clear()

        # Outside the lock: the callback needs it to drain the buffer.
        if not self._drained.wait(MAX_WAIT_SECONDS):
            log.warning("a sound did not finish playing; reopening the output")
            with self._lock:
                self._pending = np.zeros(0, dtype=np.float32)
                self._drained.set()
                self._close_locked()

    def stop(self) -> None:
        """Drop whatever is playing and whatever is queued behind it."""
        with self._lock:
            self._pending = np.zeros(0, dtype=np.float32)
            self._drained.set()


def _prepare(audio: np.ndarray, rate: int, device_rate: int,
             volume: float) -> np.ndarray:
    """One clip, at the device's rate and level, as mono float32."""
    from pitradio import speech

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if device_rate and rate and device_rate != rate:
        samples = speech.resample(samples, rate, device_rate)
    level = min(1.0, max(0.0, float(volume)))
    return (samples * level).astype(np.float32)


def _open_stream(device, callback):
    """(stream, rate, channels) for a device, in WASAPI shared mode.

    **The rate is read back, not chosen.** What `query_devices` reports and
    what the endpoint will actually accept are not always the same number, and
    guessing wrong is a hard refusal rather than a resample. Letting PortAudio
    negotiate and then asking what it settled on is the only version of this
    that cannot be wrong.
    """
    from pitradio import speech

    sd = _sd()
    index = speech.resolve_device(device, "output")

    extra = None
    try:
        # Shared mode, said out loud. A dictation app that seized the output
        # would silence the game it exists to talk over.
        if speech.host_api_of(index) == "Windows WASAPI":
            extra = sd.WasapiSettings(exclusive=False)
    except Exception:
        log.debug("could not ask for WASAPI shared mode", exc_info=True)

    stream = sd.OutputStream(device=index, channels=1, dtype="float32",
                             callback=callback, extra_settings=extra)
    stream.start()
    return stream, int(stream.samplerate), int(stream.channels)
