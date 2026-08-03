"""Driver-name matching and mention markup.

Pure text processing, so this is the one part of the session feature that can
be tested properly rather than only in a race.
"""

import pytest

from pitradio import mentions

FIELD = ["Geoff Taylor", "Max Verstappen", "José María López", "Pato O'Ward",
         "Kamui Kobayashi", "Nick Tandy"]


# -- finding names -------------------------------------------------------


def test_full_name_is_found():
    assert mentions.find_mentions("tell Max Verstappen to box", FIELD) == ["Max Verstappen"]


def test_surname_alone_is_found():
    """Surnames are how people actually refer to drivers over the radio."""
    assert mentions.find_mentions("Verstappen is catching", FIELD) == ["Max Verstappen"]


def test_an_ambiguous_first_name_alone_is_not_enough():
    """"max attack" and "nick the inside line" are ordinary racing speech.

    Those first names are still matched as part of a full name; it is only
    saying them alone that means nothing.
    """
    assert mentions.find_mentions("max attack on this lap", FIELD) == []
    assert mentions.find_mentions("nick the inside line", FIELD) == []
    assert mentions.find_mentions("Max Verstappen is quick", FIELD) == ["Max Verstappen"]


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


def test_surname_becomes_the_display_form():
    assert mentions.apply_mentions("Verstappen is catching", FIELD) == (
        "@M.Verstappen is catching")


def test_the_display_form_replaces_what_was_said():
    """Other drivers see "N.Tandy" on their HUD, so that is what to send."""
    assert mentions.apply_mentions("tell Tandy to box", FIELD) == "tell @N.Tandy to box"


def test_prefix_is_configurable():
    assert mentions.apply_mentions("Verstappen out", FIELD, prefix="~") == (
        "~M.Verstappen out")


def test_already_prefixed_names_are_left_alone():
    assert mentions.apply_mentions("@M.Verstappen out", FIELD) == "@M.Verstappen out"


def test_text_without_names_is_untouched():
    text = "box this lap, tyres are gone"
    assert mentions.apply_mentions(text, FIELD) == text


def test_no_drivers_means_no_change():
    assert mentions.apply_mentions("Verstappen out", []) == "Verstappen out"


def test_two_drivers_named_both_get_marked():
    assert mentions.apply_mentions("Verstappen and Tandy are racing", FIELD) == (
        "@M.Verstappen and @N.Tandy are racing")


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
    from pitradio import speech

    joined = speech._join_prompt("racing terms here", "Nick Tandy, Geoff Taylor")
    assert joined.index("Tandy") < joined.index("racing terms")


@pytest.mark.parametrize(
    ("base", "extra", "expected"),
    [("a", "", "a"), ("", "b", "b"), ("", "", None), ("a", "b", "b. a")],
)
def test_prompt_joining_handles_missing_parts(base, extra, expected):
    from pitradio import speech

    assert speech._join_prompt(base, extra) == expected


# -- where the prefix lands ----------------------------------------------


def test_a_full_name_becomes_the_display_form():
    assert mentions.apply_mentions("Geoff Taylor is quick", FIELD) == (
        "@G.Taylor is quick")


def test_an_accented_name_said_plainly_is_still_marked():
    """Matching normalises accents; the markup has to agree with it.

    A regex built from the stored name would find nothing in "lopez", so the
    driver was recognised and then not marked — the worst of both.
    """
    assert mentions.apply_mentions("lopez is on old tyres", FIELD) == (
        "@J.López is on old tyres")


def test_a_single_token_name_has_no_initial_to_take():
    """Plenty of sim drivers race under a handle rather than a real name."""
    assert mentions.apply_mentions("kidunot89 is quick", ["kidunot89"]) == (
        "@kidunot89 is quick")


def test_your_own_name_is_marked_like_anyone_else():
    """LMU lists every vehicle including the player, so a solo session works."""
    assert mentions.apply_mentions("Taylor boxing this lap", ["Geoff Taylor"]) == (
        "@G.Taylor boxing this lap")


def test_the_prefix_is_not_doubled():
    assert mentions.apply_mentions("@G.Taylor already marked", FIELD) == (
        "@G.Taylor already marked")


def test_two_names_are_both_marked_at_the_right_offsets():
    """Editing left to right would shift every later offset."""
    assert mentions.apply_mentions("Taylor and Tandy are racing", FIELD) == (
        "@G.Taylor and @N.Tandy are racing")


def test_punctuation_around_a_name_survives():
    assert mentions.apply_mentions("box, Tandy, now", FIELD) == "box, @N.Tandy, now"


# -- multi-word surnames -------------------------------------------------

DUTCH = ["Nyck de Vries", "Kelvin van der Linde", "Antonio Felix da Costa"]


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("de Vries is quick", "@N.de Vries is quick"),
        ("van der Linde went long", "@K.van der Linde went long"),
        ("tell da Costa to box", "tell @A.da Costa to box"),
    ],
)
def test_a_multi_word_surname_is_marked_from_its_start(said, expected):
    """Sportscar grids are full of these.

    Matching only the final token produced "de @Vries" and "van der @Linde",
    marking someone mid-surname.
    """
    assert mentions.apply_mentions(said, DUTCH) == expected


def test_the_full_name_still_wins_over_the_surname():
    assert mentions.apply_mentions("Nyck de Vries is quick", DUTCH) == (
        "@N.de Vries is quick")


def test_the_final_token_alone_still_matches():
    """Someone may well just say "Vries"."""
    assert mentions.apply_mentions("Vries is catching", DUTCH) == "@N.de Vries is catching"


def test_trailing_runs_are_longest_first():
    """Order decides which match wins, so it is worth stating."""
    assert mentions.trailing_runs(["nyck", "de", "vries"]) == [
        ["nyck", "de", "vries"], ["de", "vries"], ["vries"],
    ]


def test_a_leading_particle_alone_is_not_a_match():
    """"de" and "van" are ordinary words in their own right."""
    assert mentions.find_mentions("de la and van the", DUTCH) == []


# -- the display form ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Geoff Taylor", "G.Taylor"),
        ("Max Verstappen", "M.Verstappen"),
        ("José María López", "J.López"),          # a middle name is dropped
        ("Nyck de Vries", "N.de Vries"),          # a particle is not
        ("Kelvin van der Linde", "K.van der Linde"),
        ("Antonio Felix da Costa", "A.da Costa"),
        ("Pato O'Ward", "P.O'Ward"),
        ("kidunot89", "kidunot89"),               # a handle has no initial
    ],
)
def test_display_name(name, expected):
    assert mentions.display_name(name) == expected


@pytest.mark.parametrize(
    "said",
    ["Geoff Taylor is quick", "Taylor is quick", "Geoff is quick"],
)
def test_every_way_of_saying_it_gives_the_same_mention(said):
    """First name, surname or both — all become what the HUD shows."""
    assert mentions.apply_mentions(said, ["Geoff Taylor"]) == "@G.Taylor is quick"


def test_an_ambiguous_first_name_is_still_matched_within_a_full_name():
    assert mentions.apply_mentions("Max Verstappen is quick", ["Max Verstappen"]) == (
        "@M.Verstappen is quick")


@pytest.mark.parametrize(
    "said",
    ["max attack on this lap", "nick the inside line", "will do", "mark the apex"],
)
def test_ambiguous_first_names_alone_stay_untouched(said):
    """These are things a driver says without meaning anybody."""
    field = ["Max Verstappen", "Nick Tandy", "Will Stevens", "Mark Webber"]
    assert mentions.apply_mentions(said, field) == said


def test_first_name_matching_can_be_turned_off():
    assert mentions.apply_mentions(
        "Geoff is quick", ["Geoff Taylor"], first_names=False) == "Geoff is quick"


def test_an_existing_mention_is_not_re_marked():
    """The surname sits behind an initial and a dot, not the prefix itself."""
    assert mentions.apply_mentions("@M.Verstappen out", ["Max Verstappen"]) == (
        "@M.Verstappen out")


def test_an_unprefixed_initial_is_treated_as_ordinary_text():
    """Documents a degenerate input rather than a desired result.

    "M.Verstappen" cannot come out of speech recognition — Whisper produces
    words, not initials — so the doubled output is accepted rather than worth
    the complexity of detecting.
    """
    assert mentions.apply_mentions("M.Verstappen out", ["Max Verstappen"]) == (
        "M.@M.Verstappen out")


# -- standings positions -------------------------------------------------

STANDINGS = {1: "Max Verstappen", 2: "Geoff Taylor", 3: "Nyck de Vries"}


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("tell P3 to move over", "tell @N.de Vries to move over"),
        ("P1 is pulling away", "@M.Verstappen is pulling away"),
        ("p 2 is catching", "@G.Taylor is catching"),
        ("P-1 pitted", "@M.Verstappen pitted"),
        ("third place is quick", "@N.de Vries is quick"),
        ("first place pitted", "@M.Verstappen pitted"),
    ],
)
def test_a_position_becomes_the_driver_in_it(said, expected):
    """On a full grid most names are ones you cannot pronounce or did not
    catch; a position is always readable off the timing screen."""
    assert mentions.apply_positions(said, STANDINGS) == expected


def test_an_empty_position_is_left_alone():
    """"P40" in a twenty-car race means nothing; deleting the words is worse."""
    assert mentions.apply_positions("P40 is nobody", STANDINGS) == "P40 is nobody"


def test_no_standings_means_no_change():
    assert mentions.apply_positions("P1 is quick", {}) == "P1 is quick"


def test_ordinary_text_is_untouched():
    for said in ["box this lap", "pit now", "purple sector"]:
        assert mentions.apply_positions(said, STANDINGS) == said


def test_a_word_starting_with_p_is_not_a_position():
    assert mentions.apply_positions("push 1 more lap", STANDINGS) == "push 1 more lap"


def test_several_positions_in_one_message():
    assert mentions.apply_positions("P1 and P2 are fighting", STANDINGS) == (
        "@M.Verstappen and @G.Taylor are fighting")
