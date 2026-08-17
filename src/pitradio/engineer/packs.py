"""Voice packs: pre-recorded clips instead of a synthesiser.

A pack is a folder of WAV files, one folder per phrase, several takes per
phrase. The engineer plays a take at random, so hearing the same call twice in
a stint does not sound like a machine repeating itself — which is the single
biggest reason Crew Chief's packs sound like a person and a text-to-speech
voice does not.

**The folder layout is Crew Chief's**, on purpose. `crew-chief-autovoicepack`
already generates thousands of takes of a cloned voice into
`<pack>/voice/<category>/<phrase>/*.wav`, and there is no reason for this app
to invent a second shape that the same tool cannot fill. A flat
`<pack>/<phrase>/*.wav` works too, because that is what somebody recording
their own will produce.

**Phrase ids are derived from the words, not assigned.** `slug("two tenths")`
is `two_tenths`, and that is the folder name. Nothing has to maintain a table
mapping ids to text, a pack built for an older version keeps working, and
anything the engineer says that a pack has never heard of — a driver's name —
simply misses and falls through to the synthesiser. See `inventory`, which
writes the list a generator needs.

Nothing here plays audio or touches Windows: it is a directory listing and a
dictionary, so it can be exercised with a temporary folder anywhere.
"""

from __future__ import annotations

import csv
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: What a pack is made of. Only WAV, and deliberately: it is what every
#: generator emits, what `wave` in the standard library reads, and it needs no
#: decoder in a build that already fights native dependencies.
SUFFIXES = (".wav",)

#: A pack laid out the way `crew-chief-autovoicepack` writes one. Its own
#: folder, so a pack can also carry a licence and its source recordings
#: without them being mistaken for phrases.
VOICE_SUBDIR = "voice"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
#: Dropped rather than turned into a separator, so "that's enough" and "thats
#: enough" are the same clip. Turning them into one would give `that_s_enough`,
#: and a pack recorded against either spelling would silently miss the other.
_SLUG_DROP = re.compile("['\u2019`]")


def slug(text: str) -> str:
    """The folder name for a phrase: "two tenths" -> "two_tenths".

    Accents fold away, so a pack does not need to guess how the app spelled
    something, and punctuation goes entirely — "that's enough" and "thats
    enough" are the same clip and it would be absurd for them not to be.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SLUG_STRIP.sub("_", _SLUG_DROP.sub("", stripped.casefold())).strip("_")


@dataclass
class VoicePack:
    """One installed pack, and the clips it can supply."""

    name: str
    path: Path
    #: phrase id -> the takes available for it.
    clips: dict[str, list[Path]] = field(default_factory=dict)

    def has(self, phrase: str) -> bool:
        return bool(self.clips.get(slug(phrase)))

    def take(self, phrase: str, choose=random.choice) -> Path | None:
        """A clip for this phrase, or None if the pack has never heard it.

        `choose` is injected so a test can pin which take comes back without
        seeding the global random state, which anything else in the process
        would then be sharing.
        """
        takes = self.clips.get(slug(phrase))
        return choose(takes) if takes else None

    def __bool__(self) -> bool:
        return bool(self.clips)


def index(path: Path) -> dict[str, list[Path]]:
    """Every phrase in a pack folder, whatever depth it was written at.

    The **leaf folder name is the phrase id** and everything above it is
    filing. That is what makes a Crew Chief pack — where the leaf sits under a
    category like `corners` or `acknowledgements` — and a hand-made flat one
    both work without either being converted.

    A folder holding both clips and sub-folders keeps its clips: a generator
    that writes a general take beside more specific ones is doing something
    reasonable and there is no reason to lose it.
    """
    root = path / VOICE_SUBDIR if (path / VOICE_SUBDIR).is_dir() else path
    found: dict[str, list[Path]] = {}
    if not root.is_dir():
        return found

    try:
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        log.warning("could not read the voice pack at %s: %s", path, exc)
        return found

    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in SUFFIXES:
            continue
        phrase = slug(entry.parent.name)
        if not phrase:
            continue
        found.setdefault(phrase, []).append(entry)
    return found


def load(path: Path) -> VoicePack | None:
    """Read one pack, or None if there is nothing playable in it."""
    clips = index(path)
    if not clips:
        return None
    return VoicePack(name=path.name, path=path, clips=clips)


def discover(root: Path) -> list[VoicePack]:
    """Every pack installed under a folder, alphabetically.

    Swallows the folder not existing, because it usually will not: packs are
    an opt-in that most people never install, and the engineer works without
    one.
    """
    packs: list[VoicePack] = []
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return packs

    for candidate in candidates:
        pack = load(candidate)
        if pack is not None:
            packs.append(pack)
        else:
            log.debug("%s has no clips in it; not a voice pack", candidate)
    return packs


def find(root: Path, name: str) -> VoicePack | None:
    """The pack with this name, matched on the folder name."""
    if not name:
        return None
    wanted = slug(name)
    for pack in discover(root):
        if slug(pack.name) == wanted:
            return pack
    return None


# -- telling a generator what to record ----------------------------------


def inventory(phrases: list[tuple[str, str]], path: Path) -> Path:
    """Write the phrase list a voice-pack generator needs.

    Columns are the ones `crew-chief-autovoicepack` reads from its own
    `phrase_inventory.csv`: where the clip goes, what to say, and what to show.
    Generating this from the app rather than maintaining it by hand is the
    whole reason phrase ids are derived from the text — the list can never
    drift from what the engineer actually says.

    `phrases` is (phrase id, the words). Duplicates are dropped, since the same
    fragment turns up in several sentences and nobody wants it recorded twice.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    for phrase, text in phrases:
        seen.setdefault(phrase, text)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["audio_filename", "text_for_tts", "subtitle"])
        for phrase, text in sorted(seen.items()):
            writer.writerow([f"{phrase}/{phrase}.wav", text, text])
    return path
