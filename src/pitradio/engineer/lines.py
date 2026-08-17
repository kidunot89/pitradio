"""Everything the engineer says, in the driver's own language.

Two things live here because they are the same problem. What the engineer says
is a set of sentences, and *how it says a number* is part of the sentence — a
lap time read as "one twenty three point four six" and one read as "83.456" are
not the same call, and only one of them is what a race engineer sounds like.

**Sentences are fragments, not strings.** `["turn four", "Verstappen", "was
quicker on the exit", "two tenths"]` rather than one line of text. A
synthesiser does not care, but a voice pack does: each fragment is a folder of
recorded takes (see [packs.py](packs.py)), and a driver's name — which no pack
will ever have — is the one fragment that falls through to the synthesiser
while the rest still sound like a person.

**Numbers are spelled out in English and read as digits everywhere else.** Not
laziness, and worth being plain about: number grammar is genuinely per-language
— German inverts the tens and units, Spanish fuses the twenties — and a
half-done implementation would produce confident nonsense in somebody's own
language. Digits hand the problem to the speech voice for that language, which
already solves it. The cost is that a non-English voice pack cannot cover
numbers and they come out synthesised; that is a real limit and it is written
down in [docs/engineer.md](../../../docs/engineer.md) rather than discovered.

Every string goes through a `Catalogue`, which is a *held* language rather than
the global one the window uses — the engineer speaks whatever the driver is
talking to Whisper in, and that is not necessarily what the tabs are in.
"""

from __future__ import annotations

from dataclasses import dataclass

from pitradio import i18n
from pitradio.engineer import coaching

#: A sentence, as the fragments it is built from. Each is looked up in a voice
#: pack on its own and synthesised only if the pack has never heard it.
Utterance = list[str]

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")


@dataclass
class Script:
    """The engineer's words. One per configured language and persona.

    `terse` is what separates one built-in engineer from another beyond the
    voice itself: the same information, with or without the lead-in. Some
    people want "exit, two tenths" and some want a sentence, and it is not a
    difference worth two sets of translations — the terse form is a subset of
    the same fragments.
    """

    catalogue: i18n.Catalogue
    terse: bool = False

    @property
    def t(self):
        return self.catalogue.t

    # -- numbers ---------------------------------------------------------

    def number(self, value: int) -> str:
        """A count, as words in English and digits in every other language."""
        value = int(value)
        if not self.catalogue.english:
            return str(value)
        if value < 0:
            return f"minus {self.number(-value)}"
        if value < 20:
            return _ONES[value]
        if value < 100:
            tens, ones = divmod(value, 10)
            return _TENS[tens] if not ones else f"{_TENS[tens]} {_ONES[ones]}"
        return " ".join(_ONES[int(digit)] for digit in str(value))

    def lap_time(self, seconds: float) -> str:
        """83.456 -> "one twenty three point four six".

        The minute is bare and the seconds are not: nobody says "one minute
        twenty three", and a 65-second lap is read "one oh five". Two decimal
        places, because the third is finer than the sim's own scoring.

        Outside English the two parts are handed over as separate numbers —
        "1 23.46" — which every speech voice reads correctly for its language
        and none of them mistake for a time of day, which is what happens to
        "1:23.46".
        """
        if not seconds or seconds <= 0:
            return ""

        hundredths = int(seconds * 100 + 0.5)
        minutes, rest = divmod(hundredths, 6000)
        whole, fraction = divmod(rest, 100)

        if not self.catalogue.english:
            body = f"{whole}.{fraction:02d}"
            return f"{minutes} {body}" if minutes else body

        parts: list[str] = []
        if minutes:
            parts.append(self.number(minutes))
            # "oh five", not "five" — the tens place is spoken even when it is
            # zero, or "one five" sounds like fifteen.
            parts.append(f"oh {self.number(whole)}" if whole < 10 else self.number(whole))
        else:
            parts.append(self.number(whole))
        parts.append("point")
        parts.append(" ".join(self.number(int(digit)) for digit in f"{fraction:02d}"))
        return " ".join(parts)

    def delta(self, seconds: float) -> str:
        """A gap, said the way it is on the radio rather than printed.

        Tenths up to a second, because a tenth is the unit a driver can act on.
        Below five hundredths there is no call to make and it says so.
        """
        gap = abs(float(seconds))
        if gap < 0.05:
            return self.t("nothing in it")
        if gap < 0.95:
            # Half up, not round(): Python rounds a bare .5 to even, so the
            # first gap worth naming — 0.05 — would come back as "zero tenths".
            tenths = int(gap * 10 + 0.5)
            if tenths == 5:
                return self.t("half a second")
            if tenths == 1:
                return self.t("a tenth")
            return self.t("{count} tenths", count=self.number(tenths))
        if gap < 10:
            whole = int(gap)
            tenths = int((gap - whole) * 10 + 0.5)
            if tenths == 10:
                whole, tenths = whole + 1, 0
            if not tenths:
                if whole == 1:
                    return self.t("one second")
                return self.t("{count} seconds", count=self.number(whole))
            return self.t(
                "{count} seconds",
                count=f"{self.number(whole)} {self.t('point')} {self.number(tenths)}")
        return self.t("{count} seconds", count=self.number(int(gap + 0.5)))

    # -- what it actually says --------------------------------------------

    def acknowledge(self) -> Utterance:
        return [self.t("go ahead")]

    def not_understood(self) -> Utterance:
        return [self.t("say again")]

    def stopped(self) -> Utterance:
        return [self.t("standing down")]

    def targeting(self, driver: str, lap_time: float) -> Utterance:
        """Confirming a target, and what there is to beat."""
        if not lap_time:
            return [self.t("targeting"), driver]
        return [self.t("targeting"), driver,
                self.t("best lap"), self.lap_time(lap_time)]

    def no_lap_for(self, driver: str) -> Utterance:
        """The target exists but has not set a lap worth chasing yet."""
        return [driver, self.t("has no lap on record yet")]

    def no_such_driver(self, driver: str) -> Utterance:
        return [self.t("I can't find"), driver]

    def waiting_for_a_lap(self) -> Utterance:
        return [self.t("no reference lap yet; keep going")]

    def lap_time_call(self, seconds: float, *, personal_best: bool) -> Utterance:
        """The lap that just ended.

        The best-lap case leads rather than trails: a driver hearing their own
        time wants to know straight away whether it was the good one.
        """
        if personal_best:
            return [self.t("that's your best"), self.lap_time(seconds)]
        return [self.t("last lap"), self.lap_time(seconds)]

    def corner_call(
        self, driver: str, corner: int, phase: str, seconds: float
    ) -> Utterance:
        """The heart of the coaching routine.

        Named for the target rather than phrased as an instruction, because
        that is the true statement: the app knows one lap was quicker through
        one stretch of track, and it does not know why. "Brake later" would be
        a guess dressed up as coaching.
        """
        turn = self.t("turn {number}", number=self.number(corner))
        gap = self.delta(seconds)
        exiting = phase == coaching.EXIT
        lost = seconds > 0

        if self.terse:
            # No corner number: the driver has just come out of it and knows
            # which one it was. What they do not know is the half and the size.
            half = (self.t("faster exit") if exiting else self.t("better entry"))
            return [driver, half, gap] if lost else [self.t("yours"), half, gap]

        if lost:
            half = (self.t("was faster on the exit") if exiting
                    else self.t("had a better entry"))
            return [turn, driver, half, gap]

        half = (self.t("you were faster on the exit") if exiting
                else self.t("you had a better entry"))
        return [turn, half, gap]

    def spotter_call(self, call: str) -> Utterance:
        """Left, right, both, or one of them going clear.

        Looked up rather than passed through, so every one of these is a
        literal the string extractor can find. `spotter` returns the English
        form and this is where it becomes the driver's language.
        """
        spoken = {
            "car left": self.t("car left"),
            "car right": self.t("car right"),
            "two cars left": self.t("two cars left"),
            "two cars right": self.t("two cars right"),
            "cars both sides": self.t("cars both sides"),
            "clear left": self.t("clear left"),
            "clear right": self.t("clear right"),
            "clear": self.t("clear"),
        }
        return [spoken.get(call, call)]

    # -- laps and sectors --------------------------------------------------

    def sector_name(self, sector: int) -> str:
        return self.t("sector {number}", number=self.number(sector))

    def fastest_lap_call(self, driver: str, seconds: float, *,
                         mine: bool) -> Utterance:
        """Somebody has taken the fastest lap of the session."""
        if mine:
            return [self.t("fastest lap of the session"), self.lap_time(seconds)]
        return [driver, self.t("has the fastest lap"), self.lap_time(seconds)]

    def fastest_sector_call(self, driver: str, sector: int, seconds: float, *,
                            mine: bool) -> Utterance:
        if mine:
            return [self.t("fastest"), self.sector_name(sector),
                    self.lap_time(seconds)]
        return [driver, self.t("has taken"), self.sector_name(sector),
                self.lap_time(seconds)]

    def sector_best_call(self, sector: int, seconds: float) -> Utterance:
        """Your own best in a sector, which is the one to keep repeating."""
        return [self.t("best"), self.sector_name(sector), self.lap_time(seconds)]

    def sector_delta_call(self, sector: int, delta: float) -> Utterance:
        """How a sector went against your best. Negative is quicker."""
        gap = self.delta(delta)
        if delta > 0:
            return [self.sector_name(sector), self.t("down"), gap]
        return [self.sector_name(sector), self.t("up"), gap]

    def sector_target_call(self, driver: str, sector: int,
                           delta: float) -> Utterance:
        """The sector trainer's verdict against the driver being chased."""
        gap = self.delta(delta)
        if delta > 0:
            return [self.sector_name(sector), driver, self.t("is ahead by"), gap]
        return [self.sector_name(sector), self.t("you are ahead by"), gap]

    def targeting_sector(self, driver: str, sector: int,
                         seconds: float) -> Utterance:
        if not seconds:
            return [self.t("targeting"), driver, self.sector_name(sector)]
        return [self.t("targeting"), driver, self.sector_name(sector),
                self.lap_time(seconds)]

    def no_sector_for(self, driver: str, sector: int) -> Utterance:
        return [driver, self.t("has no time in"), self.sector_name(sector),
                self.t("yet")]

    def which_sector(self) -> Utterance:
        """The sector trainer started without being told which sector."""
        return [self.t("which sector?")]

    def waiting_for_sectors(self) -> Utterance:
        """The boundaries have not been seen yet, so nothing can be judged."""
        return [self.t("give me a lap to find the sectors")]

    def routine_started(self, name: str) -> Utterance:
        return [self.t("{routine} running", routine=name)]

    def routine_stopped(self, name: str) -> Utterance:
        return [self.t("{routine} off", routine=name)]


#: Everything above that is a fixed fragment, for `packs.inventory`. Anything
#: not in here is a driver's name or a number, and no generated pack can hold
#: those — see the module docstring.
FIXED_LINES = (
    "nothing in it", "half a second", "a tenth", "{count} tenths", "one second",
    "{count} seconds", "point", "go ahead", "say again", "standing down",
    "targeting", "best lap", "has no lap on record yet", "I can't find",
    "no reference lap yet; keep going", "that's your best", "last lap",
    "turn {number}", "was faster on the exit", "had a better entry",
    "you were faster on the exit", "you had a better entry",
    "faster exit", "better entry", "yours",
    "radio check", "car left", "car right", "two cars left", "two cars right",
    "cars both sides", "clear", "clear left", "clear right",
    "{routine} running", "{routine} off",
    "sector {number}", "fastest lap of the session", "has the fastest lap",
    "fastest", "has taken", "best", "down", "up", "is ahead by",
    "you are ahead by", "has no time in", "yet", "which sector?",
    "give me a lap to find the sectors",
)
