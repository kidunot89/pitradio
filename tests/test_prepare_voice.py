"""Preparing baseline clips for the voice-pack generator.

Every rule here comes from `crew-chief-autovoicepack`'s README, and each one is
mechanical — which is the argument for doing it in code rather than by hand in
an audio editor twenty times. Pure signal processing, so none of it needs an
audio file or a sound card.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# The repo's `packaging/` shares its name with the installed PyPI package, so
# it is reached by path rather than as a package — the same route
# `test_build_flags` takes to `build`.
sys.path.insert(0, str(Path(__file__).parent.parent / "packaging"))

import prepare_voice as prep

RATE = 22050


def speech(seconds: float, level: float = 0.3) -> np.ndarray:
    """Something that looks like talking: loud, and not a constant."""
    samples = int(RATE * seconds)
    t = np.arange(samples) / RATE
    return (np.sin(2 * np.pi * 200 * t) * level).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.float32)


# -- trimming and splitting ----------------------------------------------


def test_silence_is_trimmed_off_both_ends():
    """The README asks for this "ruthlessly"."""
    padded = np.concatenate([silence(1.0), speech(2.0), silence(1.5)])
    trimmed = prep.trim(padded, RATE)

    assert trimmed.size == pytest.approx(RATE * 2.0, rel=0.1)


def test_a_recording_of_nothing_trims_to_nothing():
    assert prep.trim(silence(3.0), RATE).size == 0


def test_a_long_pause_splits_the_clip_in_two():
    """"For any other silence or pauses longer than ~0.5 seconds, split the
    clip into two separate clips at that point"."""
    joined = np.concatenate([speech(2.0), silence(1.0), speech(2.0)])
    assert len(prep.split_on_silence(joined, RATE)) == 2


def test_a_short_pause_does_not():
    """A breath is not a join. Splitting on one turns a sentence into
    fragments that each teach the model half a word."""
    joined = np.concatenate([speech(2.0), silence(0.2), speech(2.0)])
    assert len(prep.split_on_silence(joined, RATE)) == 1


def test_splitting_leaves_no_silence_in_the_pieces():
    joined = np.concatenate([silence(0.8), speech(2.0), silence(1.0),
                             speech(2.0), silence(0.8)])
    for piece in prep.split_on_silence(joined, RATE):
        assert piece.size == pytest.approx(RATE * 2.0, rel=0.15)


# -- length ---------------------------------------------------------------


def test_anything_over_ten_seconds_is_cut_up():
    """Not an error — the generator reads the first ten seconds and throws the
    rest away. Cutting it up turns one clip into the whole recommended set."""
    pieces = prep.chunk(speech(35.0), RATE)

    assert len(pieces) == 4
    assert all(piece.size <= RATE * prep.MAX_CLIP_SECONDS for piece in pieces)


def test_something_already_short_enough_is_left_alone():
    assert len(prep.chunk(speech(6.0), RATE)) == 1


# -- level ----------------------------------------------------------------


def test_a_quiet_clip_is_brought_up():
    assert np.max(np.abs(prep.normalise(speech(1.0, level=0.05)))) == \
        pytest.approx(prep.PEAK, rel=0.01)


def test_a_loud_clip_is_brought_down_below_full_scale():
    """A clip that touches 1.0 is a clip that may already have clipped."""
    assert np.max(np.abs(prep.normalise(speech(1.0, level=0.99)))) == \
        pytest.approx(prep.PEAK, rel=0.01)


def test_silence_is_not_amplified():
    """Dividing by the peak of nothing is how a noise floor becomes a roar."""
    assert np.max(np.abs(prep.normalise(silence(1.0)))) == 0.0


# -- the whole thing ------------------------------------------------------


def test_a_realistic_recording_becomes_usable_clips():
    parts = []
    for _ in range(4):
        parts += [speech(7.0, level=0.2), silence(1.0)]
    clips = prep.prepare(np.concatenate(parts), RATE)

    assert len(clips) >= 3, "the generator wants at least three"
    for clip in clips:
        assert clip.size <= RATE * prep.MAX_CLIP_SECONDS
        assert clip.size >= RATE * prep.MIN_CLIP_SECONDS
        assert np.max(np.abs(clip)) == pytest.approx(prep.PEAK, rel=0.01)


def test_scraps_are_dropped_rather_than_padded_out():
    """A one-second clip carries too little voice to be worth one of the
    twenty-five slots."""
    parts = [speech(6.0), silence(1.0), speech(0.4), silence(1.0), speech(6.0)]
    clips = prep.prepare(np.concatenate(parts), RATE)

    assert len(clips) == 2


def test_no_more_than_the_cap_is_written():
    parts = []
    for _ in range(30):
        parts += [speech(5.0), silence(1.0)]
    assert len(prep.prepare(np.concatenate(parts), RATE, max_clips=12)) == 12


def test_each_clip_is_normalised_on_its_own():
    """Normalising the whole recording first would leave a quietly-spoken
    passage quiet relative to a loud one elsewhere in the same file."""
    parts = [speech(6.0, level=0.9), silence(1.0), speech(6.0, level=0.05)]
    clips = prep.prepare(np.concatenate(parts), RATE)

    assert len(clips) == 2
    for clip in clips:
        assert np.max(np.abs(clip)) == pytest.approx(prep.PEAK, rel=0.01)


# -- the file that comes out ---------------------------------------------


def test_the_wav_is_the_format_the_generator_asks_for(tmp_path):
    """32-bit float PCM, mono, 22050Hz. `wave` cannot write this, which is why
    it is hand-rolled — the same mismatch as at the reading end."""
    from pitradio.engineer import speaking

    target = tmp_path / "1.wav"
    prep.write_wav(target, speech(1.0), prep.TARGET_RATE)

    audio, rate = speaking.read_wav(target)
    assert rate == prep.TARGET_RATE
    assert audio.size == pytest.approx(prep.TARGET_RATE, rel=0.01)


def test_what_is_written_survives_a_round_trip(tmp_path):
    from pitradio.engineer import speaking

    original = prep.normalise(speech(0.5))
    target = tmp_path / "1.wav"
    prep.write_wav(target, original, prep.TARGET_RATE)

    read_back, _rate = speaking.read_wav(target)
    assert np.allclose(read_back, original, atol=1e-6)


# -- decoded input --------------------------------------------------------


def test_stereo_is_mixed_down():
    """XTTS wants mono, and one channel of a stereo interview is one side of a
    conversation."""
    left, right = speech(1.0, 0.4), speech(1.0, 0.2)
    mono = prep.to_mono([np.stack([left, right])])

    assert mono.ndim == 1
    assert np.allclose(mono, (left + right) / 2, atol=1e-6)


def test_integer_audio_is_scaled_not_reinterpreted():
    """A 16-bit track handed straight through is a burst of noise at full
    volume, which is exactly the kind of failure that looks like a bad clone
    rather than a bad decode."""
    loud = np.full(100, 16384, dtype=np.int16)
    assert prep.to_mono([loud]) == pytest.approx(0.5, rel=0.01)


# -- judging a take as it is recorded -------------------------------------


def test_a_clipped_take_is_rejected():
    """A headset mic in front of somebody's mouth clips on every plosive, and
    the peaks are gone before the sound card ever saw them. Finding that out
    after forty takes is a session wasted."""
    import record_voice as rec

    told, keep = rec.verdict(np.ones(1000, dtype=np.float32))
    assert keep is False and "CLIPPED" in told


def test_a_take_nobody_can_hear_is_rejected():
    import record_voice as rec

    told, keep = rec.verdict(speech(1.0, level=0.01))
    assert keep is False and "quiet" in told


def test_a_good_take_is_kept_and_its_peak_reported():
    import record_voice as rec

    told, keep = rec.verdict(speech(1.0, level=0.5))
    assert keep is True and "0.50" in told


def test_silence_is_not_mistaken_for_a_take():
    import record_voice as rec

    assert rec.verdict(np.zeros(0, dtype=np.float32))[1] is False


def test_every_reference_prompt_is_long_enough_to_fill_a_clip():
    """Ten seconds each. A prompt somebody finishes in four leaves six seconds
    of room noise, which is the one thing the README is most insistent about."""
    import record_voice as rec

    for prompt in rec.PROMPTS:
        # Reading aloud runs about 2.5 words a second.
        assert len(prompt.split()) >= rec.REFERENCE_SECONDS * 2.0, prompt
