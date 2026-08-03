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
        if _matches(words, parts, fuzzy=fuzzy, threshold=threshold):
            found.append(name)
    return found


def _matches(words: list[str], parts: list[str], *, fuzzy: bool, threshold: float) -> bool:
    """A driver is named if the full name appears in order, or a surname does.

    Surnames are how people actually refer to drivers over the radio, and a
    first name alone is far too likely to be an ordinary word.
    """
    if _contains_sequence(words, parts, fuzzy=fuzzy, threshold=threshold):
        return True
    surname = parts[-1]
    return len(parts) > 1 and _contains_sequence(
        words, [surname], fuzzy=fuzzy, threshold=threshold)


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


def apply_mentions(
    text: str,
    drivers: list[str],
    *,
    prefix: str = "@",
    fuzzy: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> str:
    """Prefix any driver named in the text, e.g. 'box Smith' -> 'box @Smith'.

    Rewrites what was actually said rather than substituting the full name: if
    someone says a surname, replacing it with the driver's full name changes
    their words for no benefit.
    """
    if not text or not drivers:
        return text

    result = text
    for name in find_mentions(text, drivers, fuzzy=fuzzy, threshold=threshold):
        for part in sorted(name_parts(name), key=len, reverse=True):
            pattern = re.compile(
                rf"(?<![\w{re.escape(prefix)}]){re.escape(part)}(?![\w])",
                re.IGNORECASE,
            )
            replaced, count = pattern.subn(f"{prefix}{part}", result, count=1)
            if count:
                result = replaced
                break
    return result


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
