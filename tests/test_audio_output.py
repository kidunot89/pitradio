"""The one place sound leaves the app.

No sound card: the stream is injected, so the mixing, the resampling, the
reopen-on-change and the interruption are all exercised anywhere. That matters
more here than usual — every bug this layer exists to fix was silent, and a
test that needs Windows would not have caught any of them.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from pitradio import audio


class FakeStream:
    """A stream that runs its callback when asked, like PortAudio would."""

    def __init__(self, rate: int = 48000, channels: int = 1) -> None:
        self.samplerate = float(rate)
        self.channels = channels
        self.started = False
        self.closed = False
        self.callback = None
        #: Everything the device was asked to make a sound with.
        self.written: list[np.ndarray] = []

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def pump(self, frames: int = 4096) -> None:
        """One callback, as PortAudio's thread would make it."""
        out = np.zeros((frames, self.channels), dtype=np.float32)
        self.callback(out, frames, None, None)
        self.written.append(out.copy())

    def heard(self) -> np.ndarray:
        if not self.written:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([block[:, 0] for block in self.written])


@pytest.fixture
def output():
    """An Output over a fake stream, with the callback pumped by the test.

    `play` blocks until the buffer drains, so the pumping has to happen on
    another thread — exactly as it does in the real thing.
    """
    import threading

    streams: list[FakeStream] = []
    opened: list[object] = []

    def open_stream(device, callback):
        stream = FakeStream()
        stream.callback = callback
        stream.start()
        streams.append(stream)
        opened.append(device)
        return stream, int(stream.samplerate), stream.channels

    out = audio.Output(lambda: opened_device[0], open_stream=open_stream)
    opened_device = [None]

    def play(samples, rate, **kwargs):
        """Play, pumping the stream until it drains."""
        done = threading.Event()

        def pump():
            while not done.wait(0.001):
                if streams:
                    streams[-1].pump()
            if streams:
                streams[-1].pump()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        out.play(samples, rate, **kwargs)
        done.set()
        thread.join(timeout=2.0)

    out.test_play = play
    out.streams = streams
    out.device = opened_device
    return out


def tone(seconds: float, rate: int) -> np.ndarray:
    return np.ones(int(rate * seconds), dtype=np.float32) * 0.5


# -- the device -----------------------------------------------------------


def test_the_stream_is_opened_once_and_kept(output):
    """Opening a WASAPI endpoint is a negotiation, not a function call. Doing
    it per sound is rolling the dice per sound."""
    for _ in range(3):
        output.test_play(tone(0.01, 48000), 48000)

    assert len(output.streams) == 1
    assert output.streams[0].started


def test_changing_the_device_reopens_it(output):
    output.test_play(tone(0.01, 48000), 48000)
    output.device[0] = "Headset"
    output.test_play(tone(0.01, 48000), 48000)

    assert len(output.streams) == 2
    assert output.streams[0].closed


def test_the_rate_comes_from_the_stream_not_from_a_guess(output):
    """What `query_devices` reports and what the endpoint will actually accept
    are not always the same, and being wrong is a refusal, not a resample."""
    output.test_play(tone(0.01, 48000), 48000)
    assert output.rate == 48000


def test_a_device_that_will_not_open_is_warned_about_not_swallowed(caplog):
    """Silence with a correct-looking settings screen is the failure this whole
    module exists to stop being invisible."""
    def refuse(device, callback):
        raise OSError("Invalid sample rate [PaErrorCode -9997]")

    out = audio.Output(lambda: "Anything", open_stream=refuse)
    with caplog.at_level("WARNING"):
        out.play(tone(0.01, 48000), 48000)
    assert "could not open audio output" in caplog.text


# -- what actually gets played -------------------------------------------


def test_a_clip_is_resampled_to_the_device(output):
    """One place does this. WASAPI shared mode accepts only the endpoint's own
    rate and refuses anything else outright."""
    output.test_play(tone(0.1, 22050), 22050)

    heard = output.streams[0].heard()
    # A tenth of a second at the device's 48kHz, not the source's 22.05kHz.
    assert np.count_nonzero(heard) == pytest.approx(4800, rel=0.02)


def test_volume_is_applied_once(output):
    output.test_play(np.ones(480, dtype=np.float32), 48000, volume=0.5)
    heard = output.streams[0].heard()
    assert heard[10] == pytest.approx(0.5)


def test_clips_queue_rather_than_overlapping(output):
    """Two at once are two nobody understood."""
    output.test_play(np.ones(480, dtype=np.float32), 48000)
    output.test_play(np.ones(480, dtype=np.float32) * 0.25, 48000)

    heard = output.streams[0].heard()
    loud = np.count_nonzero(np.isclose(heard, 1.0))
    quiet = np.count_nonzero(np.isclose(heard, 0.25))
    assert loud == 480 and quiet == 480


def test_an_urgent_clip_replaces_what_is_playing(output):
    """The whole content of "car left" is *right now*. Waiting out a lap time
    describes a car that has been passed."""
    output.test_play(np.ones(480, dtype=np.float32), 48000)
    output.test_play(np.ones(480, dtype=np.float32) * 0.25, 48000,
                     interrupt=True)

    heard = output.streams[0].heard()
    assert np.count_nonzero(np.isclose(heard, 0.25)) == 480


def test_silence_is_written_when_there_is_nothing_to_play(output):
    """The callback runs whether or not the app has anything to say, and a
    buffer left untouched is the previous block repeated as a buzz."""
    output.test_play(np.ones(48, dtype=np.float32), 48000)
    output.streams[0].pump(frames=128)

    assert np.all(output.streams[0].written[-1] == 0.0)


def test_an_empty_clip_does_nothing_at_all(output):
    output.play(np.zeros(0, dtype=np.float32), 48000)
    assert output.streams == []


# -- letting go -----------------------------------------------------------


def test_closing_hands_the_device_back(output):
    output.test_play(tone(0.01, 48000), 48000)
    output.close()

    assert output.streams[0].closed
    assert output.rate == 48000        # remembered, but nothing is open


def test_stop_releases_a_clip_that_is_playing(output):
    """From another thread, which is the only way it ever happens: `play`
    blocks its caller, and what calls `stop` is the trigger cycle or a routine
    being stood down."""
    import threading

    # Get the stream open first, so `stop` has something to stop.
    output.test_play(np.ones(48, dtype=np.float32), 48000)

    threading.Timer(0.05, output.stop).start()
    began = time.monotonic()
    output.play(np.ones(48000 * 10, dtype=np.float32), 48000)   # never pumped
    # Released by `stop`, not by the thirty-second ceiling.
    assert time.monotonic() - began < 5.0
