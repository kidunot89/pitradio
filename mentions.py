"""Turning spoken driver names into mentions.

Pure text processing, so it can be tested properly rather than only in a race.

Two things this deliberately does not do:

* **No styling.** Neither LMU nor rFactor 2 chat supports markup — no bold, no
  colour codes. Injection sends literal characters, so `@Name` arrives as those
  characters and nothing more. The prefix is a human convention, not something
  the game understands.
* **No fuzzy matching by default.** Whisper mangles unfamiliar proper nouns,
  and it is tempting to paper over that here. But a false match rewrites a word
  the driver actually said into someone else's name, which is worse than
  leaving it alone. The real fix is upstream: feeding the driver list into
  Whisper's initial_prompt so the name is heard correctly in the first place.
"""

from __future__ import annotations

import re
import unicodedata

#: How much of a name must match for a loose match to count, 0-1.
DEFAULT_THRESHOLD = 0.85

# Apostrophes matter: "O'Ward" and "D'Ambrosio" are one word, not two.
_WORD = re.compile("[\\w'\u2019-]+")


def _normalise(text: str) -> str:
    """Casefold and strip accents, so 'Jose' matches 'José'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


# Surname particles, so "de Vries" and "van der Linde" keep theirs. Lowercase
# because that is how they appear in a name; a capitalised "Van" is a first
# name in its own right.
PARTICLES = frozenset({
    "de", "del", "della", "der", "den", "di", "da", "das", "dos", "du",
    "la", "le", "van", "von", "ter", "ten", "al", "bin", "st",
})


def display_name(name: str) -> str:
    """"Geoff Taylor" -> "G.Taylor" — how sims label drivers on screen.

    Racing HUDs show an initial and a surname, so that is what other drivers
    recognise. Sending back what was said instead ("Geoff", or a surname alone)
    makes the reader work out who is meant.

    Particles stay with the surname: "Nyck de Vries" becomes "N.de Vries", not
    "N.Vries". A middle name does not — "José María López" becomes "J.López".
    """
    parts = name_parts(name)
    if not parts:
        return name.strip()
    if len(parts) == 1:
        # A handle rather than a name; there is no initial to take.
        return parts[0]

    surname = [parts[-1]]
    index = len(parts) - 2
    while index > 0 and parts[index].lower() in PARTICLES:
        surname.insert(0, parts[index])
        index -= 1

    return f"{parts[0][0].upper()}.{' '.join(surname)}"


def name_parts(name: str) -> list[str]:
    """The tokens of a driver name worth matching on.

    Single characters are dropped: an initial in "J. Smith" would otherwise
    match every stray letter in a sentence.
    """
    return [part for part in re.split(r"[\s.,_-]+", name.strip()) if len(part) > 1]


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def build_index(drivers: list[str]) -> list[tuple[str, list[str]]]:
    """(full name, searchable tokens) for each driver, longest name first.

    Longest first so "Max Verstappen" is tried before "Max" and the fuller
    match wins.
    """
    index = []
    for driver in drivers:
        name = driver.strip()
        if not name:
            continue
        index.append((name, [_normalise(part) for part in name_parts(name)]))
    index.sort(key=lambda entry: -len(entry[0]))
    return index


def find_mentions(
    text: str,
    drivers: list[str],
    *,
    fuzzy: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    first_names: bool = True,
) -> list[str]:
    """Which drivers are named in the text, most complete match first."""
    if not text or not drivers:
        return []

    words = [_normalise(word) for word in re.findall(_WORD, text)]
    if not words:
        return []

    found = []
    for name, parts in build_index(drivers):
        if not parts:
            continue
        if _matches(words, parts, fuzzy=fuzzy, threshold=threshold,
                    first_names=first_names):
            found.append(name)
    return found


def trailing_runs(parts: list[str]) -> list[list[str]]:
    """The full name, then each shorter run ending at the surname.

    Surnames are how drivers are referred to on the radio and how sims label
    them on screen — and plenty of them are more than one word. Matching only
    the final token turns "de Vries" into "de @Vries" and "van der Linde" into
    "van der @Linde", marking someone mid-surname.

    Longest first, so the most complete match wins.
    """
    return [parts[start:] for start in range(len(parts))]


# First names that are also ordinary racing speech. Matched as part of a full
# name, never on their own — "max attack", "nick the inside line", "will do"
# and "mark the apex" are all things a driver says without meaning anybody.
AMBIGUOUS_FIRST_NAMES = frozenset({
    "max", "nick", "will", "mark", "chase", "rob", "grant", "miles", "art",
    "guy", "drew", "ray", "hunter", "porter", "gale", "sunny", "rusty",
})


def match_runs(parts: list[str], *, first_names: bool = True) -> list[list[str]]:
    """Every run of a name worth matching, longest first.

    Trailing runs cover the full name and the surname — including multi-word
    surnames. The leading token is added separately so a first name alone can
    be recognised, since people do say them, but only when it is not also an
    ordinary word.
    """
    runs = trailing_runs(parts)
    if first_names and len(parts) > 1 and parts[0] not in AMBIGUOUS_FIRST_NAMES:
        runs.append([parts[0]])
    return runs


def _matches(
    words: list[str], parts: list[str], *, fuzzy: bool, threshold: float,
    first_names: bool = True,
) -> bool:
    """A driver is named if any recognisable run of their name appears."""
    return any(
        _contains_sequence(words, run, fuzzy=fuzzy, threshold=threshold)
        for run in match_runs(parts, first_names=first_names)
    )


def _contains_sequence(
    words: list[str], parts: list[str], *, fuzzy: bool, threshold: float
) -> bool:
    span = len(parts)
    for start in range(len(words) - span + 1):
        window = words[start:start + span]
        if all(_word_matches(w, p, fuzzy=fuzzy, threshold=threshold)
               for w, p in zip(window, parts, strict=True)):
            return True
    return False


def _word_matches(word: str, part: str, *, fuzzy: bool, threshold: float) -> bool:
    if word == part:
        return True
    if not fuzzy:
        return False
    # Short words are excluded from fuzzy matching entirely: at three or four
    # characters almost anything clears a ratio threshold.
    if len(part) < 5 or len(word) < 5:
        return False
    return _similarity(word, part) >= threshold


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """(start, end, normalised) for every word, so spans map back to the text."""
    return [(m.start(), m.end(), _normalise(m.group())) for m in _WORD.finditer(text)]


def _find_span(
    tokens: list[tuple[int, int, str]],
    parts: list[str],
    *,
    fuzzy: bool,
    threshold: float,
) -> tuple[int, int] | None:
    """(start, end) character offsets of a run matching `parts`, or None."""
    span = len(parts)
    for start in range(len(tokens) - span + 1):
        window = tokens[start:start + span]
        if all(_word_matches(t[2], p, fuzzy=fuzzy, threshold=threshold)
               for t, p in zip(window, parts, strict=True)):
            return window[0][0], window[-1][1]
    return None


def apply_mentions(
    text: str,
    drivers: list[str],
    *,
    prefix: str = "@",
    fuzzy: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    first_names: bool = True,
) -> str:
    """Replace a named driver with how the sim labels them: '@G.Taylor'.

    Whatever was said — "Geoff", "Taylor", or the full name — becomes the same
    canonical form, because that is what every other driver sees on their HUD.
    Echoing back the spoken words instead leaves the reader working out who was
    meant, which defeats the point of a mention.

    Works on token positions rather than a regex over the raw text: matching
    normalises accents, so "lopez" finds "José María López", and a regex built
    from the stored name would not match what was actually written.
    """
    if not text or not drivers:
        return text

    tokens = _tokens(text)
    if not tokens:
        return text

    # (start, end, replacement), collected before any edit so offsets stay
    # valid while they are being found.
    edits: list[tuple[int, int, str]] = []
    for name in find_mentions(text, drivers, fuzzy=fuzzy, threshold=threshold,
                              first_names=first_names):
        parts = [_normalise(p) for p in name_parts(name)]
        if not parts:
            continue

        # Longest run first, so "Geoff Taylor" is replaced as a whole rather
        # than leaving a stray first name behind.
        for run in match_runs(parts, first_names=first_names):
            span = _find_span(tokens, run, fuzzy=fuzzy, threshold=threshold)
            if span is None:
                continue
            begin, finish = span
            if any(begin < e and b < finish for b, e, _ in edits):
                break  # already covered by another driver's replacement
            edits.append((begin, finish, f"{prefix}{display_name(name)}"))
            break

    # Right to left, so earlier offsets stay valid as the text changes length.
    result = text
    for begin, finish, replacement in sorted(edits, reverse=True):
        if _already_mentioned(result, begin, prefix):
            continue
        result = result[:begin] + replacement + result[finish:]
    return result


def _already_mentioned(text: str, begin: int, prefix: str) -> bool:
    """Whether the match at `begin` is already inside a mention.

    Checking only the character before the match is not enough once the
    replacement is a display form: in "@M.Verstappen" the surname is preceded
    by ".", not "@", so a naive guard produced "@M.@M.Verstappen". Skip back
    over any initial-and-dot and look for the prefix behind it.
    """
    index = begin
    while index > 0 and (text[index - 1].isalnum() or text[index - 1] == "."):
        index -= 1
    return text[max(0, index - len(prefix)):index] == prefix


# "P3", "p 3", "P-3". Whisper renders a spoken "P three" inconsistently, and
# an optional space or hyphen covers most of what it produces.
_POSITION = re.compile(r"\bP\s?-?\s?(\d{1,2})\b", re.IGNORECASE)

# Spelled-out ordinals, which Whisper produces at least as often as digits.
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_ORDINAL_PLACE = re.compile(
    r"\b(" + "|".join(_ORDINALS) + r")\s+place\b", re.IGNORECASE)


def apply_positions(
    text: str,
    positions: dict[int, str],
    *,
    prefix: str = "@",
) -> str:
    """Replace a standings reference with the driver in that place.

    "tell P3 to move over" becomes "tell @N.Tandy to move over". On a full grid
    most names are ones you cannot pronounce or did not catch, and a position is
    something you can always read off the timing screen.

    A position with nobody in it is left alone rather than blanked: saying "P40"
    in a twenty-car race means nothing, and silently deleting it would be worse
    than leaving the words.
    """
    if not text or not positions:
        return text

    def replace_digits(match: re.Match) -> str:
        name = positions.get(int(match.group(1)))
        return f"{prefix}{display_name(name)}" if name else match.group(0)

    def replace_ordinal(match: re.Match) -> str:
        name = positions.get(_ORDINALS[match.group(1).lower()])
        return f"{prefix}{display_name(name)}" if name else match.group(0)

    return _ORDINAL_PLACE.sub(replace_ordinal, _POSITION.sub(replace_digits, text))


def vocabulary_hint(drivers: list[str], limit: int = 40) -> str:
    """Driver names as a fragment to append to Whisper's initial_prompt.

    This is the part that actually improves accuracy. Whisper mangles proper
    nouns it has no reason to expect; telling it who is in the session is worth
    far more than any amount of matching after the fact.

    Capped because initial_prompt is truncated around 224 tokens, and losing
    the racing vocabulary to a 60-car entry list would be a poor trade.
    """
    names = [d.strip() for d in drivers if d and d.strip()]
    if not names:
        return ""
    return ", ".join(dict.fromkeys(names[:limit]))
