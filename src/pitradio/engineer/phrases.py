"""What the engineer answers to.

Pure text, like [mentions.py](../mentions.py), and for the same reason: whether
"initiate build procedures" is heard as a command decides whether somebody's
words reach the chat box or vanish into the engineer instead. Getting that
wrong is not a mistake you want to discover mid-race, so all of it is testable
without Windows, audio or a sim.

**A command is recognised conservatively.** The trigger key is the same one
used for talking to the other drivers, so every utterance is a candidate, and a
false positive silently swallows a message that was meant for the chat box.
Two ways in, both narrow:

* **Addressed.** The sentence opens with the engineer's name — "Chief, target
  P3" — and the phrase follows immediately.
* **Bare.** The whole sentence *is* the phrase. "initiate build procedures"
  works with nothing in front of it, but "tell them to initiate build
  procedures" does not, and goes to the chat box where it belongs.

A phrase that takes an argument is only ever recognised on the addressed path,
because its argument has no end — see `_try_phrase` for the message that
disappeared before that rule existed.

Nothing here does fuzzy matching. A phrase the driver chose is a phrase they
can say the same way twice, and a near-miss that grabbed the wrong message
would be far worse than one that let it through.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: A trigger phrase may end with a `{placeholder}`, and whatever follows
#: becomes the command's argument: "begin hot lap trainer {target}" turns
#: "begin hot lap trainer GT3 P1" into Command(argument="GT3 P1"). The name
#: inside the braces is documentation — what a routine expects — and only its
#: presence is read here. Only ever at the end: a placeholder in the middle
#: would need to guess where the parameter stopped.
PLACEHOLDER = re.compile(r"\{[a-z_]*\}")

#: How many words a phrase must have in front of its placeholder to be
#: recognised without the engineer's name. **This is the whole false-positive
#: defence**, so the reasoning is worth keeping:
#:
#: A phrase that takes a parameter has no end — everything after it is the
#: parameter. Unaddressed, a one-word phrase like "target {driver}" swallowed
#: "target time is a twenty three", a perfectly ordinary thing to say about a
#: lap time, and the message never reached the chat box.
#:
#: Two or more words removes that. "begin hot lap trainer" and "sector trainer"
#: are not things anybody says by accident, and demanding the engineer's name
#: for them would be pedantry. One word still needs addressing.
MIN_BARE_WORDS = 2

#: Routine ids that are not routines. They are handled by the service itself,
#: and no routine may claim these names.
ACKNOWLEDGE = "acknowledge"
STOP = "stop"

#: Always understood, whatever routine is running and whatever it was started
#: with. A driver who wants a routine to shut up should not have to remember
#: which words started it.
STOP_PHRASES = ("stop", "stand down", "cancel", "cancel that", "that's enough",
                "forget it")

#: Words allowed in front of the name without being part of it.
ADDRESS_FILLERS = frozenset({"hey", "ok", "okay", "yo", "hi", "hello", "right"})

#: Words allowed to trail a phrase without breaking the match. Politeness is
#: not a reason for a command to go unrecognised.
TRAILING_FILLERS = frozenset({"please", "now", "thanks", "thank", "you", "mate",
                              "buddy"})

# Apostrophes and hyphens stay inside a word, so "that's" is one token and
# "Pérez-Companc" is not three. The curly apostrophe is escaped rather than
# written, as it is in mentions.py: the two are visually identical in most
# editors, and a typo between them is invisible.
_WORD = re.compile("[\\w'\u2019-]+")


def _fold(text: str) -> str:
    """Casefold and strip accents, so "Perez" matches "Pérez"."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("\u2019", "'")


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    text: str


def tokens(text: str) -> list[Token]:
    """Every word, with the offsets that map it back to the original.

    Offsets rather than words alone, because an argument has to be handed back
    *as it was said* — matching folds accents away, and a driver called
    "Sébastien" must not come back as "Sebastien" for the name lookup.
    """
    return [Token(m.start(), m.end(), _fold(m.group())) for m in _WORD.finditer(text)]


def words(text: str) -> list[str]:
    return [token.text for token in tokens(text)]


@dataclass(frozen=True)
class Command:
    """A recognised instruction. `routine` is a routine id, or one of the
    built-ins above."""

    routine: str
    argument: str = ""
    phrase: str = ""
    #: Whether the engineer's name was used. Worth keeping: an unaddressed
    #: command matched the whole sentence, which is the stricter path, and the
    #: log line is much easier to read when it says which one fired.
    addressed: bool = False
    #: Whether this was a routine's *end* phrase rather than its start. Both
    #: name the same routine, and telling them apart by which list the phrase
    #: came from beats encoding it in the id.
    ending: bool = False


def _skip_address(found: list[Token], name: list[str]) -> tuple[int, bool]:
    """(index of the first word after the name, whether it was there).

    A leading filler is allowed before the name and nowhere else: "hey Chief"
    is how people talk, and "the chief said" is not addressing anybody.
    """
    if not name:
        return 0, False
    for offset in (0, 1):
        if offset and (not found or found[0].text not in ADDRESS_FILLERS):
            continue
        window = [token.text for token in found[offset:offset + len(name)]]
        if window == name:
            return offset + len(name), True
    return 0, False


def _trailing_is_filler(found: list[Token], index: int) -> bool:
    return all(token.text in TRAILING_FILLERS for token in found[index:])


def _try_phrase(
    found: list[Token], start: int, routine: str, phrase: str, *, addressed: bool,
    original: str, ending: bool = False,
) -> Command | None:
    match = PLACEHOLDER.search(phrase)
    takes_argument = match is not None
    head = words(phrase[:match.start()] if match else phrase)
    if not head:
        return None

    # An open-ended parameter with too little in front of it is how a message
    # gets swallowed. See MIN_BARE_WORDS.
    if takes_argument and not addressed and len(head) < MIN_BARE_WORDS:
        return None

    end = start + len(head)
    if [token.text for token in found[start:end]] != head:
        return None

    if not takes_argument:
        if not _trailing_is_filler(found, end):
            return None
        return Command(routine, "", phrase, addressed, ending)

    # Everything after the phrase, as it was actually said. Trailing courtesy
    # words are dropped: "target Verstappen please" is not a driver called
    # "Verstappen please".
    tail = list(found[end:])
    while tail and tail[-1].text in TRAILING_FILLERS:
        tail.pop()
    if not tail:
        # A phrase whose parameters are optional still matches with none —
        # "begin hot lap trainer" on its own means whoever is quickest. Only
        # unaddressed one-word phrases were rejected above, and those never
        # reach here.
        return Command(routine, "", phrase, addressed, ending)
    argument = original[tail[0].start:tail[-1].end].strip()
    return Command(routine, argument, phrase, addressed, ending)


def match_command(
    text: str,
    *,
    name: str = "",
    entries: tuple[tuple[str, tuple[str, ...]], ...] = (),
    end_entries: tuple[tuple[str, tuple[str, ...]], ...] = (),
    stop_phrases: tuple[str, ...] = STOP_PHRASES,
) -> Command | None:
    """The command this sentence is, or None if it is just something to say.

    `entries` is (routine id, its trigger phrases). The stop phrases and the
    bare-name acknowledgement are added here rather than by every caller, so a
    routine can never accidentally shadow them. They are a parameter because
    they are translated too — see `default_phrases`.

    Longest phrase first, so "target the leader" is tried before "target" and
    the more specific one wins.
    """
    if not text or not text.strip():
        return None

    found = tokens(text)
    if not found:
        return None

    spoken_name = words(name)
    start, addressed = _skip_address(found, spoken_name)

    if addressed and _trailing_is_filler(found, start):
        # Just the name. On a real radio that gets "go ahead", and here it also
        # keeps a stray "Chief" out of the chat box.
        return Command(ACKNOWLEDGE, "", name, True)

    candidates: list[tuple[str, str, bool]] = [
        # Ending phrases first at equal length: "stop hot lap trainer" must not
        # be read as the *start* phrase "hot lap trainer" with an argument of
        # "stop" — which is what happens if the two are tried the other way
        # round and the start phrase is the shorter of them.
        *((routine, phrase, True)
          for routine, phrases in end_entries for phrase in phrases),
        *((routine, phrase, False)
          for routine, phrases in entries for phrase in phrases),
        *((STOP, phrase, False) for phrase in stop_phrases),
    ]
    candidates.sort(
        key=lambda entry: -len(words(PLACEHOLDER.sub("", entry[1]))))

    for routine, phrase, ending in candidates:
        if not phrase or not phrase.strip():
            continue
        command = _try_phrase(found, start, routine, phrase, addressed=addressed,
                              original=text, ending=ending)
        if command is not None:
            return command
    return None


def default_phrases(catalogue, entries: tuple[tuple[str, tuple[str, ...]], ...]):
    """Trigger phrases in the driver's own language.

    A phrase is what somebody *says*, so an engineer that only answers to
    English is no use to a driver whose Whisper model is transcribing Spanish —
    the words would never arrive in the form the matcher is looking for. Each
    built-in phrase goes through the catalogue like any other string, and
    anything a driver types into the Engineer tab is used exactly as typed.
    """
    return tuple(
        (routine, tuple(catalogue.translate(phrase) for phrase in phrases))
        for routine, phrases in entries
    )
