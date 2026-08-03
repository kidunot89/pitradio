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


def trailing_runs(parts: list[str]) -> list[list[str]]:
    """The full name, then each shorter run ending at the surname.

    Surnames are how drivers are referred to on the radio and how sims label
    them on screen — and plenty of them are more than one word. Matching only
    the final token turns "de Vries" into "de @Vries" and "van der Linde" into
    "van der @Linde", marking someone mid-surname.

    Longest first, so the most complete match wins.
    """
    return [parts[start:] for start in range(len(parts))]


def _matches(words: list[str], parts: list[str], *, fuzzy: bool, threshold: float) -> bool:
    """A driver is named if the full name, or any trailing run of it, appears.

    A first name alone is deliberately not enough: "Max" and "Nick" are
    ordinary words, and marking them would be worse than missing them.
    """
    return any(
        _contains_sequence(words, run, fuzzy=fuzzy, threshold=threshold)
        for run in trailing_runs(parts)
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
) -> int | None:
    """Character offset where a run of tokens matches `parts`, or None."""
    span = len(parts)
    for start in range(len(tokens) - span + 1):
        window = tokens[start:start + span]
        if all(_word_matches(t[2], p, fuzzy=fuzzy, threshold=threshold)
               for t, p in zip(window, parts, strict=True)):
            return window[0][0]
    return None


def apply_mentions(
    text: str,
    drivers: list[str],
    *,
    prefix: str = "@",
    fuzzy: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> str:
    """Prefix any driver named in the text, e.g. 'box Smith' -> 'box @Smith'.

    Works on token positions rather than a regex over the raw text, for two
    reasons. Matching normalises accents — "lopez" finds "José María López" —
    and a regex built from the stored name would then fail to mark up what was
    actually written. And when a full name appears, the prefix belongs at the
    start of it: substituting on the longest part alone produced
    "Geoff @Taylor", marking someone mid-name.

    Rewrites what was said rather than substituting the stored name: if someone
    says a surname, replacing it with the full name changes their words for no
    benefit.
    """
    if not text or not drivers:
        return text

    tokens = _tokens(text)
    if not tokens:
        return text

    inserts: list[int] = []
    for name in find_mentions(text, drivers, fuzzy=fuzzy, threshold=threshold):
        parts = [_normalise(p) for p in name_parts(name)]
        if not parts:
            continue

        # Longest run first, so the prefix lands at the start of whatever was
        # actually said — "@de Vries", not "de @Vries".
        for run in trailing_runs(parts):
            at = _find_span(tokens, run, fuzzy=fuzzy, threshold=threshold)
            if at is not None:
                if at not in inserts:
                    inserts.append(at)
                break

    # Right to left, so earlier offsets stay valid as the string grows.
    result = text
    for at in sorted(inserts, reverse=True):
        if result[max(0, at - len(prefix)):at] == prefix:
            continue  # already marked
        result = result[:at] + prefix + result[at:]
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
