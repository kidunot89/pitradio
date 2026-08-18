import numpy as np
import pytest

from pitradio import speech


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  hello there  ", "hello there"),
        ("hello    there", "hello there"),
        ("", ""),
        ("   \t  ", ""),
        (None, ""),
    ],
)
def test_whitespace_is_collapsed(raw, expected):
    assert speech.sanitize(raw, 200) == expected


@pytest.mark.parametrize("raw", ["box\nthis lap", "box\r\nthis lap", "box\rthis lap"])
def test_newlines_become_spaces(raw):
    """A stray newline would submit the message halfway through typing it."""
    assert speech.sanitize(raw, 200) == "box this lap"


def test_truncation_respects_max_chars():
    assert speech.sanitize("abcdefghij", 4) == "abcd"


def test_truncation_does_not_leave_a_trailing_space():
    assert speech.sanitize("ab cdefgh", 3) == "ab"


def test_max_chars_zero_means_no_limit():
    assert speech.sanitize("abcdefghij", 0) == "abcdefghij"


def test_text_shorter_than_the_limit_is_untouched():
    assert speech.sanitize("box this lap", 200) == "box this lap"


def test_non_ascii_survives():
    """Whisper emits smart quotes and dashes routinely."""
    assert speech.sanitize("a “quote” and em—dash", 200) == "a “quote” and em—dash"


# -- levelling voice clips -----------------------------------------------
#
# Transcription and voice want different things from the same recording, and
# only one of them has ever complained. Whisper normalises internally, so
# PitRadio worked at any capture level while the identical clip was inaudible
# over a headset — measured at 0.015 peak, needing 47x to hear.


def _tone(peak: float, seconds: float = 1.0, rate: int = 16000):
    t = np.linspace(0.0, seconds, int(rate * seconds), endpoint=False)
    return (np.sin(2 * np.pi * 220 * t) * peak).astype("float32")


def _hiss(peak: float, seconds: float = 1.0, rate: int = 16000):
    """Steady noise: far less energy than speech at the same peak."""
    rng = np.random.default_rng(1)
    noise = rng.normal(0.0, peak / 4.0, int(rate * seconds))
    return np.clip(noise, -peak, peak).astype("float32")


def test_a_quiet_clip_is_brought_up_to_something_audible():
    assert np.abs(speech.normalise_voice(_tone(0.015))).max() > 0.4


def test_a_clip_that_is_already_loud_is_left_alone():
    assert np.abs(speech.normalise_voice(_tone(0.9))).max() == pytest.approx(0.9)


def test_silence_is_not_amplified_into_hiss():
    """Measured on **RMS**, not peak: peak cannot tell silence from quiet
    speech, because room noise routinely peaks above any floor low enough to
    admit a quiet voice."""
    noise = _hiss(0.002)
    assert np.abs(speech.normalise_voice(noise)).max() == pytest.approx(
        np.abs(noise).max())


def test_audible_noise_is_levelled_like_anything_else():
    """It does not attempt to tell speech from noise, and should not.

    That is voice activity detection — Whisper's VAD already does it, on the
    transcription side. A normaliser that guessed would sometimes guess wrong
    and silence somebody mid-race, which is far worse than levelling a clip of
    a noisy garage that its speaker chose to send by holding the trigger.

    What bounds the damage is the gain cap, not a cleverer test.
    """
    levelled = speech.normalise_voice(_hiss(0.02))
    assert np.abs(levelled).max() > 0.02
    assert np.abs(levelled).max() <= speech.VOICE_TARGET_PEAK + 1e-6


def test_gain_is_capped():
    """Unbounded, a whisper's own noise floor becomes a shriek."""
    assert np.abs(speech.normalise_voice(_tone(0.05))).max() <= 0.7 + 1e-6


def test_an_empty_clip_is_handled():
    assert speech.normalise_voice(np.zeros(0, dtype="float32")).size == 0
    assert speech.normalise_voice(None) is None


def test_resampling_keeps_the_clip_the_same_length_in_time():
    """Otherwise a device that refuses 16kHz plays everybody back chipmunked."""
    resampled = speech._resample(_tone(0.5, seconds=1.0), 16000, 48000)
    assert resampled.size == pytest.approx(48000, rel=0.01)
    assert np.abs(resampled).max() == pytest.approx(0.5, abs=0.05)
