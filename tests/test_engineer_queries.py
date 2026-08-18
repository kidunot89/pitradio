"""Asking the engineer a question and getting one answer.

The parameter follows the keyword and is never part of the phrase: what a
driver can ask about depends on the sim they are in -- the classes on this
grid, the sectors this circuit has -- and none of that belongs in a phrase
somebody typed into a settings box.
"""

from __future__ import annotations

from pitradio.engineer import queries

CLASSES = {"LMGT3", "LMP2", "Hypercar"}


# -- what follows the keyword ---------------------------------------------


def test_nothing_after_the_keyword_is_a_bare_question():
    ask = queries.parse("", CLASSES)
    assert ask == queries.Ask()


def test_a_sector_is_understood_however_it_is_said():
    for said in ("3", "three", "sector 3", "sector three", "the third sector"):
        assert queries.parse(said, CLASSES).sector == 3, said


def test_a_class_answers_to_what_people_call_it():
    """LMU calls it LMGT3 and every driver on the radio calls it GT3."""
    assert queries.parse("GT3", CLASSES).vehicle_class == "LMGT3"
    assert queries.parse("LMGT3", CLASSES).vehicle_class == "LMGT3"


def test_a_class_and_a_sector_together():
    ask = queries.parse("three in the GT3 class", CLASSES)
    assert (ask.vehicle_class, ask.sector) == ("LMGT3", 3)


def test_lmp2_does_not_answer_to_p2():
    """"P2" is a position. Answering to it would resolve the wrong thing, and
    `mentions.class_aliases` refuses for that reason."""
    assert queries.parse("P2", CLASSES).vehicle_class != "LMP2"


def test_a_class_nobody_is_in_is_said_so_rather_than_guessed_at():
    """Falling back to the overall answer would be a wrong answer stated
    confidently, and the driver would have no way to tell."""
    assert queries.parse("LMP1", CLASSES).unknown_class is True


def test_a_trailing_pleasantry_is_not_a_class():
    assert queries.parse("in the class please", CLASSES).unknown_class is False


def test_a_sector_out_of_range_is_no_sector():
    assert queries.parse("sector 7", CLASSES).sector == 0


def test_a_grid_with_no_classes_never_reports_an_unknown_one():
    """A single-class sim has no class to name and nothing to be wrong about."""
    assert queries.parse("anything at all", set()).unknown_class is False
