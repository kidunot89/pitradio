"""Hearing a command, and saying a number.

The stakes here are asymmetric and that is what most of these cases are about.
A command the engineer *misses* costs a routine that did not start, and the
driver says it again. A command it *invents* swallows a message somebody meant
to send to twenty other people, with no error and no way to tell — the words
simply never arrived. So the matcher is deliberately narrow, and most of what
follows checks that ordinary racing speech survives it untouched.
"""

from __future__ import annotations

import pytest

from pitradio import i18n
from pitradio.engineer import coaching, lines, phrases, spotter

ENTRIES = (
    ("corner_coach", ("target {driver}", "initiate build procedures",
                      "begin hot lap trainer {target}")),
    ("fuel", ("fuel check",)),
)

END_ENTRIES = (("corner_coach", ("end hot lap trainer",)),)


def match(text, name="Chief"):
    return phrases.match_command(
        text, name=name, entries=ENTRIES, end_entries=END_ENTRIES)


# -- addressed by name ----------------------------------------------------


def test_the_name_and_a_phrase_is_a_command():
    command = match("Chief, target Verstappen")
    assert command.routine == "corner_coach"
    assert command.argument == "Verstappen"
    assert command.addressed is True


def test_a_filler_in_front_of_the_name_is_allowed():
    assert match("hey Chief, fuel check").routine == "fuel"


def test_the_name_alone_is_answered_rather_than_typed():
    """Saying just the name has to mean something, or it goes to the chat box.

    On a real radio it gets "go ahead". Here it also keeps a bare "Chief" out
    of a message to the whole session, which is the part that matters.
    """
    assert match("Chief").routine == phrases.ACKNOWLEDGE
    assert match("hey Chief").routine == phrases.ACKNOWLEDGE


def test_a_name_in_the_middle_of_a_sentence_is_not_addressing_anybody():
    assert match("tell the chief we are boxing") is None


def test_an_empty_name_still_matches_a_bare_phrase():
    """Somebody who has cleared the name keeps the phrases and loses the rest."""
    assert match("initiate build procedures", name="").routine == "corner_coach"
    assert match("Chief, fuel check", name="") is None


# -- unaddressed ----------------------------------------------------------


def test_a_phrase_on_its_own_is_a_command():
    assert match("initiate build procedures").routine == "corner_coach"


def test_a_phrase_buried_in_a_sentence_is_a_message():
    """The whole point of the strict path. This is somebody talking to the
    session about the routine, not starting it."""
    assert match("tell them to initiate build procedures") is None
    assert match("initiate build procedures when you can, mate and then box") is None


def test_ordinary_racing_speech_is_left_alone():
    for said in (
        "box this lap",
        "P3 is all over me",
        "target time is a twenty three",
        "check your mirrors",
        "that was a good stint",
    ):
        assert match(said) is None, said


def test_courtesy_does_not_break_a_match():
    assert match("initiate build procedures please").routine == "corner_coach"
    assert match("Chief, fuel check thanks").routine == "fuel"


# -- arguments ------------------------------------------------------------


def test_the_argument_keeps_the_spelling_it_was_said_with():
    """Matching folds accents; the argument must not.

    The name is handed to driver lookup, which compares against what the sim
    calls somebody — and that has the accents in it.
    """
    assert match("Chief, target Sébastien Loeb").argument == "Sébastien Loeb"


def test_courtesy_is_not_part_of_the_argument():
    assert match("Chief, target Verstappen please").argument == "Verstappen"


def test_a_one_word_phrase_taking_a_parameter_needs_the_name():
    """The message this rule exists for.

    "target time is a twenty three" is somebody telling the session what they
    are aiming for. Unaddressed, "target {driver}" swallowed it whole and
    coached against a driver called "time is a twenty three" — the message
    never reached the chat box and nothing said where it had gone.
    """
    assert match("target time is a twenty three") is None
    assert match("target Verstappen") is None
    assert match("Chief, target Verstappen").argument == "Verstappen"


def test_a_distinctive_phrase_taking_a_parameter_does_not():
    """Nobody says "begin hot lap trainer" by accident, so demanding the name
    in front of it would be pedantry rather than safety."""
    command = match("begin hot lap trainer GT3 P1")
    assert command.routine == "corner_coach"
    assert command.argument == "GT3 P1"


def test_a_phrase_taking_a_parameter_still_matches_without_one():
    """"begin hot lap trainer" on its own means whoever is quickest."""
    command = match("begin hot lap trainer")
    assert command is not None
    assert command.argument == ""


def test_an_end_phrase_names_the_same_routine_and_says_it_is_an_end():
    command = match("end hot lap trainer")
    assert command.routine == "corner_coach"
    assert command.ending is True


def test_an_end_phrase_is_not_read_as_a_start_with_a_parameter():
    """"end hot lap trainer" must not become "hot lap trainer" with an
    argument of "end", which is what happens if the two are tried the wrong
    way round."""
    assert match("end hot lap trainer").ending is True


def test_a_longer_phrase_wins_over_a_shorter_one():
    entries = (("a", ("target",)), ("b", ("target the leader",)))
    command = phrases.match_command("target the leader", name="", entries=entries)
    assert command.routine == "b"


# -- stopping -------------------------------------------------------------


def test_stop_is_always_understood():
    assert match("stop").routine == phrases.STOP
    assert match("Chief, that's enough").routine == phrases.STOP


def test_stop_phrases_can_be_replaced_for_another_language():
    command = phrases.match_command(
        "basta", name="", entries=(), stop_phrases=("basta",))
    assert command.routine == phrases.STOP


# -- default phrases ------------------------------------------------------


def test_default_phrases_go_through_the_catalogue():
    catalogue = i18n.Catalogue("xx", {"target {driver}": "objetivo {driver}"})
    translated = phrases.default_phrases(
        catalogue, (("corner_coach", ("target {driver}",)),))
    assert translated == (("corner_coach", ("objetivo {driver}",)),)


def test_a_translated_phrase_matches_in_that_language():
    entries = (("corner_coach", ("objetivo {driver}",)),)
    command = phrases.match_command(
        "Jefe, objetivo Verstappen", name="Jefe", entries=entries)
    assert command.argument == "Verstappen"


# -- saying numbers -------------------------------------------------------


@pytest.fixture
def script():
    return lines.Script(i18n.Catalogue())


def test_numbers_are_words_in_english(script):
    assert script.number(3) == "three"
    assert script.number(23) == "twenty three"
    assert script.number(40) == "forty"


def test_a_lap_time_reads_the_way_it_is_said(script):
    assert " ".join(script.lap_time(83.456)) == "one twenty three point four six"


def test_a_lap_under_a_minute_drops_the_minute(script):
    assert " ".join(script.lap_time(45.2)) == "forty five point two zero"


def test_the_tens_place_is_spoken_even_when_it_is_zero(script):
    """"one five" is fifteen. "one oh five" is sixty five seconds."""
    assert " ".join(script.lap_time(65.0)).startswith("one oh five")


def test_no_lap_time_says_nothing(script):
    assert script.lap_time(0) == []
    assert script.lap_time(-1) == []


def test_the_smallest_gap_worth_naming_is_a_tenth(script):
    """0.05 rounds to a tenth. Python's round() would make it zero, and the
    engineer would announce "zero tenths" as its first useful call."""
    assert script.delta(0.05) == ["a tenth"]
    assert script.delta(0.04) == ["nothing in it"]


def test_gaps_are_said_in_tenths_then_seconds(script):
    assert script.delta(0.2) == ["two", "tenths"]
    assert script.delta(0.5) == ["half a second"]
    assert script.delta(1.0) == ["one second"]
    assert script.delta(2.4) == ["two", "point", "four", "seconds"]


def test_a_gap_is_the_same_either_way_round(script):
    assert script.delta(-0.3) == script.delta(0.3)


# -- other languages ------------------------------------------------------


@pytest.fixture
def spanish():
    return lines.Script(i18n.Catalogue("es", {
        "{count} tenths": "{count} décimas",
        "turn {number}": "curva {number}",
        "was faster on the exit": "fue más rápido a la salida",
    }))


def test_numbers_are_digits_outside_english(spanish):
    """Number grammar is per-language and doing it half-well would produce
    confident nonsense. Digits hand it to the speech voice, which knows."""
    assert spanish.number(23) == "23"
    assert spanish.lap_time(83.456) == ["1 23.46"]


def test_a_translated_sentence_uses_the_catalogue(spanish):
    said = spanish.corner_call("Verstappen", 4, "exit", 0.2)
    assert said[0] == "curva 4"
    assert said[1] == "Verstappen"
    assert said[2] == "fue más rápido a la salida"
    assert said[3] == "2 décimas"


def test_an_untranslated_sentence_falls_back_to_english(spanish):
    assert spanish.stopped() == ["standing down"]


# -- what a voice pack is asked to record ---------------------------------


def test_a_lap_time_is_fragments_a_pack_can_hold():
    """Joined up, "one twenty three point four six" is a phrase no pack could
    ever contain, so every lap time fell through to the Windows synthesiser —
    audibly, mid-sentence. Crew Chief composes times from a numbers folder for
    exactly this reason."""
    script = lines.Script(i18n.Catalogue("en"))
    assert script.lap_time(83.456) == [
        "one", "twenty three", "point", "four", "six"]


def test_a_lap_just_over_a_minute_keeps_its_tens_place():
    """"one five" sounds like fifteen."""
    script = lines.Script(i18n.Catalogue("en"))
    assert script.lap_time(65.04) == [
        "one", "oh", "five", "point", "zero", "four"]


def test_every_fragment_of_a_call_is_in_the_vocabulary():
    """**The check that makes a pack worth generating.** A fragment the
    inventory never asked for is a word the pack does not have, and the
    engineer drops into the Windows voice for it in the middle of a sentence.
    """
    catalogue = i18n.Catalogue("en")
    script = lines.Script(catalogue)
    known = set(lines.vocabulary(catalogue))

    calls = [
        script.lap_time_call(83.456, personal_best=True),
        script.lap_time_call(65.04, personal_best=False),
        script.fastest_sector_call("Estre", 2, 41.28, mine=False),
        script.sector_delta_call(3, 0.42),
        script.sector_best_call(1, 28.9),
        script.corner_call("Nato", 8, coaching.EXIT, 0.23),
        script.corner_call("Nato", 12, coaching.ENTRY, -1.5),
        script.fuel_answer(86, 17),
        script.flag_call("car stopped in turn 6"),
        script.flag_call("car stopped in sector 2"),
        script.flag_call("full course yellow"),
        script.spotter_call("car left"),
        script.spotter_call(spotter.HOLD_YOUR_LINE),
        script.rejoin_call(True),
        script.best_lap_answer(91.2),
        script.no_time_yet(),
    ]
    missing = {fragment for call in calls for fragment in call
               if fragment not in known}
    # Driver names are the one thing no pack can hold, by definition.
    assert missing <= {"Estre", "Nato"}, sorted(missing)


def test_the_vocabulary_covers_every_number_a_lap_time_uses():
    catalogue = i18n.Catalogue("en")
    script = lines.Script(catalogue)
    known = set(lines.vocabulary(catalogue))

    missing = set()
    for hundredths in range(0, 12000, 7):
        missing |= {part for part in script.lap_time(hundredths / 100.0)
                    if part not in known}
    assert not missing, sorted(missing)


def test_templates_are_not_offered_for_recording():
    """A line with a placeholder is assembled from fragments that are
    themselves listed, so recording the template records a phrase the engineer
    never says."""
    assert not [line for line in lines.vocabulary(i18n.Catalogue("en"))
                if "{" in line]
