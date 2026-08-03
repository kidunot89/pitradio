"""Driver-name matching and mention markup.

Pure text processing, so this is the one part of the session feature that can
be tested properly rather than only in a race.
"""

import pytest

import mentions

FIELD = ["Geoff Taylor", "Max Verstappen", "José María López", "Pato O'Ward",
         "Kamui Kobayashi", "Nick Tandy"]


# -- finding names -------------------------------------------------------


def test_full_name_is_found():
    assert mentions.find_mentions("tell Max Verstappen to box", FIELD) == ["Max Verstappen"]


def test_surname_alone_is_found():
    """Surnames are how people actually refer to drivers over the radio."""
    assert mentions.find_mentions("Verstappen is catching", FIELD) == ["Max Verstappen"]


def test_first_name_alone_is_not_enough():
    """'Nick' or 'Max' as ordinary words must not become mentions."""
    assert mentions.find_mentions("max attack on this lap", FIELD) == []
    assert mentions.find_mentions("nick the inside line", FIELD) == []


def test_accents_do_not_matter():
    """Whisper rarely produces the accents, and the driver never types them."""
    assert mentions.find_mentions("lopez is on old tyres", FIELD) == ["José María López"]


def test_apostrophes_survive():
    assert mentions.find_mentions("O'Ward went long", FIELD) == ["Pato O'Ward"]


def test_nobody_named_finds_nothing():
    assert mentions.find_mentions("box this lap, tyres are gone", FIELD) == []


def test_empty_inputs_are_safe():
    assert mentions.find_mentions("", FIELD) == []
    assert mentions.find_mentions("Verstappen", []) == []


def test_matching_is_case_insensitive():
    assert mentions.find_mentions("VERSTAPPEN", FIELD) == ["Max Verstappen"]


def test_a_substring_is_not_a_match():
    """'tandy' must not match inside another word."""
    assert mentions.find_mentions("standying start", FIELD) == []


# -- markup --------------------------------------------------------------


def test_surname_gets_the_prefix():
    assert mentions.apply_mentions("Verstappen is catching", FIELD) == (
        "@Verstappen is catching")


def test_the_spoken_words_are_kept():
    """Saying a surname must not be rewritten into the full name."""
    assert mentions.apply_mentions("tell Tandy to box", FIELD) == "tell @Tandy to box"


def test_prefix_is_configurable():
    assert mentions.apply_mentions("Verstappen out", FIELD, prefix="~") == "~Verstappen out"


def test_already_prefixed_names_are_left_alone():
    assert mentions.apply_mentions("@Verstappen out", FIELD) == "@Verstappen out"


def test_text_without_names_is_untouched():
    text = "box this lap, tyres are gone"
    assert mentions.apply_mentions(text, FIELD) == text


def test_no_drivers_means_no_change():
    assert mentions.apply_mentions("Verstappen out", []) == "Verstappen out"


def test_two_drivers_named_both_get_marked():
    result = mentions.apply_mentions("Verstappen and Tandy are racing", FIELD)
    assert "@Verstappen" in result
    assert "@Tandy" in result


# -- fuzzy matching, which is off by default -----------------------------


def test_fuzzy_is_off_by_default():
    """A near-miss must not silently become someone's name."""
    assert mentions.find_mentions("verstapen is catching", FIELD) == []


def test_fuzzy_catches_a_mangled_name_when_asked():
    assert mentions.find_mentions(
        "verstapen is catching", FIELD, fuzzy=True) == ["Max Verstappen"]


def test_fuzzy_ignores_short_words():
    """At four characters almost anything clears a ratio threshold."""
    assert mentions.find_mentions("tady", ["Nick Tandy"], fuzzy=True) == []


def test_fuzzy_still_rejects_an_unrelated_word():
    assert mentions.find_mentions(
        "gearbox is broken", FIELD, fuzzy=True) == []


# -- vocabulary hint -----------------------------------------------------


def test_vocabulary_hint_lists_names():
    hint = mentions.vocabulary_hint(["Geoff Taylor", "Nick Tandy"])
    assert "Geoff Taylor" in hint
    assert "Nick Tandy" in hint


def test_vocabulary_hint_is_capped():
    """initial_prompt truncates; a 60-car entry list would evict the racing terms."""
    hint = mentions.vocabulary_hint([f"Driver {i}" for i in range(100)], limit=5)
    assert hint.count(",") == 4


def test_vocabulary_hint_deduplicates():
    hint = mentions.vocabulary_hint(["Nick Tandy", "Nick Tandy", "Geoff Taylor"])
    assert hint.count("Nick Tandy") == 1


def test_vocabulary_hint_of_nothing_is_empty():
    assert mentions.vocabulary_hint([]) == ""
    assert mentions.vocabulary_hint(["", "   "]) == ""


# -- prompt joining ------------------------------------------------------


def test_names_precede_the_generic_vocabulary():
    """initial_prompt is truncated, so the session-specific part must come first."""
    import speech

    joined = speech._join_prompt("racing terms here", "Nick Tandy, Geoff Taylor")
    assert joined.index("Tandy") < joined.index("racing terms")


@pytest.mark.parametrize(
    ("base", "extra", "expected"),
    [("a", "", "a"), ("", "b", "b"), ("", "", None), ("a", "b", "b. a")],
)
def test_prompt_joining_handles_missing_parts(base, extra, expected):
    import speech

    assert speech._join_prompt(base, extra) == expected
