"""The four engineers that ship with the app.

**A persona is a preset, not a recording.** It has to be said plainly, because
the obvious reading of "four voices out of the box" is four sets of audio, and
that is not a thing this installer can contain: a generated pack of one voice
is one to two gigabytes, and the whole PitRadio installer is a fraction of
that. Four of them would be an eight gigabyte download to gain what is already
sitting on every Windows machine for nothing.

So a persona is a *name*, a *voice to prefer*, a *pace*, and *how much it
talks*, resolved against whatever speech voices the machine actually has. On a
stock Windows 11 install that is enough for four engineers who sound clearly
different from each other the moment you pick one, with no download and nothing
to configure.

Anyone who does want a specific human voice has the other route, and it is the
better one: generate a pack with `crew-chief-autovoicepack` and drop it in. A
persona picks that up too — see `resolve`, which prefers a pack of the same
name over the synthesiser.

The names are deliberately not real people. A voice pack cloned from a
broadcaster and shipped in an app is somebody else's likeness, and this project
is not going to be the reason that argument happens.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Persona ids are stored in config and must never be renamed in place.
DEFAULT = "chief"


@dataclass(frozen=True)
class Persona:
    """One built-in engineer."""

    id: str
    #: What it is called by default. The driver can rename it, and that name is
    #: what the engineer answers to — see `phrases.match_command`.
    name: str
    description: str
    #: Which installed speech voice to prefer. Matched loosely: this is a hint
    #: for `pick_voice`, not a requirement, because which voices exist varies
    #: with the Windows edition and the languages installed on it.
    prefers: str = ""
    #: Windows' own -10..10. Race calls want a little quicker than a voice's
    #: natural reading pace, which is tuned for prose.
    rate: int = 1
    #: Whether it drops the lead-in. The difference between "turn four,
    #: Taylor was faster on the exit, two tenths" and "Taylor, faster exit,
    #: two tenths" — the same information, for people who want different
    #: amounts of it.
    terse: bool = False


BUILTIN: tuple[Persona, ...] = (
    Persona(
        id="chief",
        name="Chief",
        description="Steady and complete. Says which corner, who, and by how much.",
        prefers="male",
        rate=1,
    ),
    Persona(
        id="ada",
        name="Ada",
        description="Clipped. Drops the corner number and gets to the number.",
        prefers="female",
        rate=2,
        terse=True,
    ),
    Persona(
        id="marshall",
        name="Marshall",
        description="Slower and fuller, for anyone who finds the others rushed.",
        prefers="male",
        rate=-1,
    ),
    Persona(
        id="vic",
        name="Vic",
        description="Quick and short. The least talking of the four.",
        prefers="female",
        rate=3,
        terse=True,
    ),
)


def by_id(persona_id: str) -> Persona:
    """A persona by id, falling back to the default rather than failing.

    A config naming a persona that no longer exists is a config from a future
    version or a typo, and neither is worth a silent engineer.
    """
    for persona in BUILTIN:
        if persona.id == persona_id:
            return persona
    return BUILTIN[0]


def choices() -> list[tuple[str, str]]:
    """(id, label) for the Engineer tab's picker."""
    return [(persona.id, persona.name) for persona in BUILTIN]


def pick_voice(persona: Persona, voices, language: str = "") -> str:
    """The installed speech voice this persona should use, or "" for the default.

    Three preferences in order, each one narrower than the last is worth
    giving up: the right language, then the preferred gender, then anything.
    Language first because a German voice reading English is unintelligible in
    a way that the wrong gender simply is not.

    Returns a name to hand to `tts.SapiHost`, and an empty string when nothing
    is installed — which is not a failure, it is "let Windows choose".
    """
    if not voices:
        return ""

    wanted = (language or "").strip().lower()[:2]
    matching = [v for v in voices if v.culture.lower().startswith(wanted)] if wanted else []
    pool = matching or list(voices)

    gender = (persona.prefers or "").lower()
    if gender:
        preferred = [v for v in pool if v.gender.lower() == gender]
        if preferred:
            # Spread the personas across the voices that fit rather than giving
            # them all the first one: two engineers with the same name-plate
            # voice are one engineer with two names.
            index = [p.id for p in BUILTIN if p.prefers == persona.prefers].index(persona.id)
            return preferred[index % len(preferred)].name
    return pool[0].name if pool else ""
