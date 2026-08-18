"""Rejecting the takes a voice generator got wrong.

XTTS rambles on short text, and its own integrity check cannot tell — that
check asks whether the audio is valid speech, not whether it is the phrase that
was asked for. A pack picks between takes at random, so one bad take in three
is a one-in-three chance on every call.

Pure judgement on durations, so none of this needs an audio file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "packaging"))

import clean_pack


def test_a_take_that_rambled_is_rejected():
    """The real one that prompted this: "fifteen", generated three times."""
    verdicts = clean_pack.judge("fifteen", {
        "1.wav": 0.39, "2.wav": 2.54, "3.wav": 12.57})

    assert "1.wav" not in verdicts, "the clean take must survive"
    assert set(verdicts) == {"2.wav", "3.wav"}


def test_consistent_takes_are_all_kept():
    """Real delivery varies. A take with a breath in it is not wrong."""
    assert clean_pack.judge("car left", {
        "1.wav": 0.55, "2.wav": 0.61, "3.wav": 0.58}) == {}


def test_variation_within_reason_survives():
    """One said a little more deliberately than another is not a fault, and
    rejecting it would flatten the variation the takes exist to provide."""
    assert clean_pack.judge("hold your line", {
        "1.wav": 0.80, "2.wav": 1.35, "3.wav": 0.95}) == {}


def test_a_silent_take_is_rejected():
    verdicts = clean_pack.judge("point", {"1.wav": 0.02, "2.wav": 0.5})
    assert "1.wav" in verdicts and "silent" in verdicts["1.wav"]


def test_when_every_take_rambled_the_words_decide():
    """No good sibling to measure against, so fall back on what the phrase
    itself can account for."""
    verdicts = clean_pack.judge("one", {
        "1.wav": 4.0, "2.wav": 4.4, "3.wav": 5.0})
    assert len(verdicts) == 2, "the least-bad one is kept"


def test_a_phrase_never_loses_every_take():
    """**The rule that stops this doing harm.** A phrase with no takes falls
    back to the Windows synthesiser mid-sentence, which is worse than the
    least-bad recording of it."""
    takes = {"1.wav": 6.0, "2.wav": 7.0, "3.wav": 9.0}
    verdicts = clean_pack.judge("go ahead", takes)

    assert len(verdicts) == len(takes) - 1
    kept = set(takes) - set(verdicts)
    assert kept == {"1.wav"}, "the shortest survives"


def test_a_long_phrase_is_allowed_to_be_long():
    """The per-word allowance has to scale, or every real sentence in the pack
    is rejected for the crime of having words in it."""
    assert clean_pack.judge(
        "nobody_is_in_that_class",
        {"1.wav": 1.9, "2.wav": 2.1, "3.wav": 2.4}) == {}


def test_a_single_take_is_judged_against_the_words():
    """With no sibling there is nothing to compare to but the phrase."""
    assert clean_pack.judge("clear", {"1.wav": 0.6}) == {}
    assert clean_pack.judge("clear", {"1.wav": 8.0}) == {}, \
        "and it is kept anyway rather than leaving the phrase empty"


def test_the_expected_length_grows_with_the_phrase():
    assert clean_pack.expected("one") < clean_pack.expected(
        "nobody_is_in_that_class")
