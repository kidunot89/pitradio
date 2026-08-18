"""Asking the engineer a question, and getting one answer.

Distinct from a routine, and the distinction is not bookkeeping. A routine is
something the engineer *starts doing* and goes on doing until it is stood down;
a query is a question with an answer, and when the answer has been given there
is nothing running. Modelling one as the other would put questions in the
routines list on the Engineer tab, where every entry has a stop phrase and a
tick-box, and neither means anything for "who has the fastest lap".

**The parameter follows the keyword and is never part of the phrase.** What a
driver can ask about depends on the sim they are in — the classes on this grid,
the sectors this circuit has — and none of that belongs in a phrase somebody
typed into a settings box. So "who has the fastest sector" is the phrase, and
"three in GT3" is what came after it, parsed here against what the session
actually contains.

That parsing is deliberately forgiving, because this arrives through Whisper.
"sector three", "sector 3", "3", "the third sector" and "three in the GT3 class"
all reach the same place, and a class is matched through `mentions.class_aliases`
so LMU's "LMGT3" answers to "GT3" exactly as it does everywhere else.

Pure: given a session's classes and the books, it returns what to say.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pitradio import mentions
from pitradio.engineer import phrases

FASTEST_LAP = "fastest_lap"
FASTEST_SECTOR = "fastest_sector"
MY_BEST_LAP = "my_best_lap"

#: What each query answers to out of the box, before anybody edits them.
#:
#: Several forms each, because people ask the same thing several ways and
#: remembering which one you configured is not a thing to do at 200km/h. Every
#: one of them is a phrase somebody would actually say into a radio.
#: **Every one of them is interrogative**, and the bare forms are deliberately
#: absent. "fastest lap" is a thing somebody says into a chat box about the lap
#: they have just done, and a question that swallows it costs a message.
DEFAULT_PHRASES: dict[str, tuple[str, ...]] = {
    FASTEST_LAP: ("who has the fastest lap", "what's the fastest lap",
                  "who's got the fastest lap"),
    FASTEST_SECTOR: ("who has the fastest sector", "what's the fastest sector",
                     "who's got the fastest sector"),
    MY_BEST_LAP: ("what's my best lap", "how am I doing"),
}

#: Whether a query takes an argument at all. `MY_BEST_LAP` does not — there is
#: only one answer and no class or sector to name — and saying so here keeps it
#: off the addressed-only path that argument-taking phrases are confined to.
TAKES_ARGUMENT = frozenset({FASTEST_LAP, FASTEST_SECTOR})

#: Spoken numbers, for the sector. Whisper writes "sector three" as often as
#: "sector 3" and both have to land in the same place. One to three, because
#: that is how many sectors a circuit has.
_SPOKEN = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3}

_SECTOR = re.compile(
    r"\b(?:sector\s*)?(\d|one|two|three|first|second|third)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Ask:
    """What was actually asked: which class, and which sector."""

    #: The class name as the *session* spells it, not as it was said. Empty
    #: means "whatever the default is", which is the driver's own class.
    vehicle_class: str = ""
    #: 1-3, or 0 when none was named.
    sector: int = 0
    #: Whether a class was named that no car on this grid is in. Distinct from
    #: naming none: "the fastest lap in LMP1" at a race with no LMP1 entry has
    #: no answer, and inventing the overall one instead would be a wrong answer
    #: stated confidently.
    unknown_class: bool = False


def understood(ask: Ask) -> bool:
    """Whether this argument was something the engineer can act on.

    **The false-positive defence for questions**, and a better one than
    counting words. An argument has no end — everything after the phrase is
    the argument — so unaddressed, "who has the fastest lap of my life that
    one" would be taken as a question about a class called "of my life that
    one" and the message would never reach the chat box.

    The argument space here is closed, which is what makes this possible: a
    class on *this* grid, a sector between one and three, or nothing. Anything
    else was not a question, whatever it started with.
    """
    return not ask.unknown_class


def parse(argument: str, classes) -> Ask:
    """What follows the keyword, against what this session contains.

    `classes` is every class name on the grid, which is why this cannot be done
    when the phrase is configured: the answer depends on the session.
    """
    text = (argument or "").strip()
    if not text:
        return Ask()

    sector = 0
    match = _SECTOR.search(text)
    if match:
        found = match[1].lower()
        sector = _SPOKEN.get(found, 0) or (int(found) if found.isdigit() else 0)
        if not 1 <= sector <= 3:
            sector = 0

    named = _match_class(text, classes)
    if named is None:
        # Something was said that is not a sector and not a class on this grid.
        # Only a problem if it was the whole of it — "three" alone is a sector,
        # and a trailing "please" is not a class.
        return Ask("", sector, unknown_class=_is_a_name(text))
    return Ask(named, sector)


#: Words that carry no meaning of their own in an argument. The grammar ones
#: are here; the politenesses come from `phrases.TRAILING_FILLERS`, so "please"
#: is dropped in one place rather than two.
_GRAMMAR = frozenset({"in", "the", "for", "class", "of", "a", "an", "and",
                      "sector", "turn"})


def _is_a_name(text: str) -> bool:
    """Whether what is left, once the sector and the grammar are taken out, was
    somebody trying to name a class.

    The distinction the caller needs: "three please" named no class and
    "LMP1" named one nobody is in, and only the second is worth saying so
    about.
    """
    rest = _SECTOR.sub(" ", text)
    return any(word not in _GRAMMAR and word not in phrases.TRAILING_FILLERS
               for word in phrases.words(rest))


def _match_class(text: str, classes) -> str | None:
    """The class named in this text, "" for none, or None for one that is not
    on the grid.

    Three answers rather than two, because "no class was named" and "a class
    was named that nobody is in" want different things said.
    """
    available = {name for name in (classes or ()) if name}
    if not available:
        return ""

    aliases: dict[str, str] = {}
    clashes: set[str] = set()
    for name in available:
        for alias in mentions.class_aliases(name):
            if alias in aliases and aliases[alias] != name:
                # Two classes answering to the same spoken form is a coin toss.
                clashes.add(alias)
            aliases[alias] = name
    for alias in clashes:
        aliases.pop(alias, None)

    # Longest first, so "LMGT3" is tried before "GT3" when both are present.
    for alias in sorted(aliases, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
            return aliases[alias]

    # Something was said. Whether it was meant as a class is decided by the
    # caller, which knows whether anything else in it parsed.
    return None
