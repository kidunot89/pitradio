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

import re
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
    """The engineer's words. One per configured language.

    `terse` drops the lead-in: the same information, with or without it. Some
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

    def lap_time(self, seconds: float) -> Utterance:
        """83.456 -> ["one", "twenty three", "point", "four", "six"].

        The minute is bare and the seconds are not: nobody says "one minute
        twenty three", and a 65-second lap is read "one oh five". Two decimal
        places, because the third is finer than the sim's own scoring.

        **Fragments, not one string**, and that is what lets a voice pack say a
        lap time at all. Joined up, "one twenty three point four six" is a
        phrase no pack could ever contain, so every time fell through to the
        Windows synthesiser — audibly, in the middle of a sentence otherwise
        spoken by somebody else. Crew Chief composes times from a `numbers`
        folder for exactly this reason. Split, a pack needs sixty-odd number
        clips and covers every lap time there is.

        Outside English the two parts are handed over as digits — "1 23.46" —
        which every speech voice reads correctly for its language and none of
        them mistake for a time of day, which is what happens to "1:23.46".
        """
        if not seconds or seconds <= 0:
            return []

        hundredths = int(seconds * 100 + 0.5)
        minutes, rest = divmod(hundredths, 6000)
        whole, fraction = divmod(rest, 100)

        if not self.catalogue.english:
            body = f"{whole}.{fraction:02d}"
            return [f"{minutes} {body}"] if minutes else [body]

        parts: Utterance = []
        if minutes:
            parts.append(self.number(minutes))
            # "oh five", not "five" — the tens place is spoken even when it is
            # zero, or "one five" sounds like fifteen.
            if whole < 10:
                parts.extend([self.t("oh"), self.number(whole)])
            else:
                parts.append(self.number(whole))
        else:
            parts.append(self.number(whole))
        parts.append(self.t("point"))
        parts.extend(self.number(int(digit)) for digit in f"{fraction:02d}")
        return parts

    def delta(self, seconds: float) -> Utterance:
        """A gap, said the way it is on the radio rather than printed.

        Tenths up to a second, because a tenth is the unit a driver can act on.
        Below five hundredths there is no call to make and it says so.
        """
        gap = abs(float(seconds))
        if gap < 0.05:
            return [self.t("nothing in it")]
        if gap < 0.95:
            # Half up, not round(): Python rounds a bare .5 to even, so the
            # first gap worth naming — 0.05 — would come back as "zero tenths".
            tenths = int(gap * 10 + 0.5)
            if tenths == 5:
                return [self.t("half a second")]
            if tenths == 1:
                return [self.t("a tenth")]
            if not self.catalogue.english:
                return [self.t("{count} tenths", count=self.number(tenths))]
            return [self.number(tenths), self.t("tenths")]
        if gap < 10:
            whole = int(gap)
            tenths = int((gap - whole) * 10 + 0.5)
            if tenths == 10:
                whole, tenths = whole + 1, 0
            if not tenths:
                if whole == 1:
                    return [self.t("one second")]
                return self._seconds(self.number(whole))
            return self._seconds(
                f"{self.number(whole)} {self.t('point')} {self.number(tenths)}"
                if not self.catalogue.english else None,
                self.number(whole), self.t("point"), self.number(tenths))
        return self._seconds(self.number(int(gap + 0.5)))

    def _seconds(self, *parts) -> Utterance:
        """"N seconds", as one translated phrase or as fragments.

        The count leads in English and a translator owns where it goes
        everywhere else — the same rule as `turn_name`.
        """
        pieces = [part for part in parts if part]
        if not self.catalogue.english:
            return [self.t("{count} seconds", count=" ".join(pieces))]
        return [*pieces, self.t("seconds")]

    def turn_name(self, corner: int) -> Utterance:
        """"turn" and the number, split only where splitting helps.

        Two fragments in English, so a pack needs "turn" once and reuses the
        numbers it already has rather than a recording per corner.

        **One fragment everywhere else**, and that is not a shortcut. Word
        order is per-language — Spanish says "curva 4" and German "Kurve 4",
        but plenty of languages do not put the number last — and a translator
        who was handed the two halves separately could not fix it. Nothing is
        lost by keeping the template: numbers are digits outside English, so a
        non-English pack cannot cover them anyway.
        """
        if not self.catalogue.english:
            return [self.t("turn {number}", number=self.number(corner))]
        return [self.t("turn"), self.number(corner)]

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
                self.t("best lap"), *self.lap_time(lap_time)]

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
            return [self.t("that's your best"), *self.lap_time(seconds)]
        return [self.t("last lap"), *self.lap_time(seconds)]

    def corner_call(
        self, driver: str, corner: int, phase: str, seconds: float
    ) -> Utterance:
        """The heart of the coaching routine.

        Named for the target rather than phrased as an instruction, because
        that is the true statement: the app knows one lap was quicker through
        one stretch of track, and it does not know why. "Brake later" would be
        a guess dressed up as coaching.
        """
        turn = self.turn_name(corner)
        gap = self.delta(seconds)
        exiting = phase == coaching.EXIT
        lost = seconds > 0

        if self.terse:
            # No corner number: the driver has just come out of it and knows
            # which one it was. What they do not know is the half and the size.
            half = (self.t("faster exit") if exiting else self.t("better entry"))
            return ([driver, half, *gap] if lost
                    else [self.t("yours"), half, *gap])

        if lost:
            half = (self.t("was faster on the exit") if exiting
                    else self.t("had a better entry"))
            return [*turn, driver, half, *gap]

        half = (self.t("you were faster on the exit") if exiting
                else self.t("you had a better entry"))
        return [*turn, half, *gap]

    def urgent_phrases(self) -> list[str]:
        """Everything the spotter can say, for pre-rendering.

        A short fixed set, and the only calls whose entire value is being on
        time. Rendered up front so the first "car left" of a session does not
        pay for a synthesiser while somebody is already alongside — see
        `speaking.Speaker.prime`.
        """
        return [
            self.t("car left"), self.t("car right"),
            self.t("three wide you're on the left"),
            self.t("three wide you're on the right"),
            self.t("in the middle"), self.t("hold your line"),
            self.t("clear left"), self.t("clear right"), self.t("clear"),
            self.t("clear all round"), self.t("still there"),
            self.t("car stopped ahead"),
        ]

    def flag_call(self, call: str) -> Utterance:
        """A flag, or something stopped on the road.

        `flags.py` returns the English form and this is where it becomes the
        driver's language. A corner or sector number arrives inside the string,
        so those two are matched rather than looked up whole — there is no
        sensible way to enumerate every turn on every circuit.
        """
        fixed = {
            "full course yellow": self.t("full course yellow"),
            "green flag": self.t("green flag"),
            "blue flag": self.t("blue flag"),
            "car stopped ahead": self.t("car stopped ahead"),
        }
        if call in fixed:
            return [fixed[call]]

        match = re.fullmatch(r"car stopped in turn (\d+)", call)
        if match:
            return [self.t("car stopped in"), *self.turn_name(int(match[1]))]
        match = re.fullmatch(r"car stopped in sector (\d+)", call)
        if match:
            return [self.t("car stopped in"), *self.sector_name(int(match[1]))]
        return [call]

    def rejoin_call(self, clear: bool) -> Utterance:
        """Whether there is room to pull out.

        Two words either way. A driver stationary on a racing line is not
        listening to a sentence, and the wrong half of a long one heard late is
        how somebody pulls out in front of a car.
        """
        return [self.t("clear to go") if clear else self.t("hold")]

    def spotter_call(self, call: str) -> Utterance:
        """Left, right, both, a hazard ahead, or a side going clear.

        Looked up rather than passed through, so every one of these is a
        literal the string extractor can find. `spotter` returns the English
        form and this is where it becomes the driver's language.
        """
        spoken = {
            "car left": self.t("car left"),
            "car right": self.t("car right"),
            "three wide you're on the left":
                self.t("three wide you're on the left"),
            "three wide you're on the right":
                self.t("three wide you're on the right"),
            "in the middle": self.t("in the middle"),
            "hold your line": self.t("hold your line"),
            "clear left": self.t("clear left"),
            "clear right": self.t("clear right"),
            "clear": self.t("clear"),
            "clear all round": self.t("clear all round"),
            "still there": self.t("still there"),
            "car stopped ahead": self.t("car stopped ahead"),
        }
        return [spoken.get(call, call)]

    # -- laps and sectors --------------------------------------------------

    def sector_name(self, sector: int) -> Utterance:
        if not self.catalogue.english:
            # Word order is the translator's, not ours — see `turn_name`.
            return [self.t("sector {number}", number=self.number(sector))]
        return [self.t("sector"), self.number(sector)]

    def fastest_lap_call(self, driver: str, seconds: float, *,
                         mine: bool) -> Utterance:
        """Somebody has taken the fastest lap of the session."""
        if mine:
            return [self.t("fastest lap of the session"), *self.lap_time(seconds)]
        return [driver, self.t("has the fastest lap"), *self.lap_time(seconds)]

    def fastest_sector_call(self, driver: str, sector: int, seconds: float, *,
                            mine: bool) -> Utterance:
        if mine:
            return [self.t("fastest"), *self.sector_name(sector),
                    *self.lap_time(seconds)]
        return [driver, self.t("has taken"), *self.sector_name(sector),
                *self.lap_time(seconds)]

    def sector_best_call(self, sector: int, seconds: float) -> Utterance:
        """Your own best in a sector, which is the one to keep repeating."""
        return [self.t("best"), *self.sector_name(sector),
                *self.lap_time(seconds)]

    def sector_delta_call(self, sector: int, delta: float) -> Utterance:
        """How a sector went against your best. Negative is quicker."""
        gap = self.delta(delta)
        if delta > 0:
            return [*self.sector_name(sector), self.t("down"), *gap]
        return [*self.sector_name(sector), self.t("up"), *gap]

    def sector_target_call(self, driver: str, sector: int,
                           delta: float) -> Utterance:
        """The sector trainer's verdict against the driver being chased."""
        gap = self.delta(delta)
        if delta > 0:
            return [*self.sector_name(sector), driver,
                    self.t("is ahead by"), *gap]
        return [*self.sector_name(sector), self.t("you are ahead by"), *gap]

    def targeting_sector(self, driver: str, sector: int,
                         seconds: float) -> Utterance:
        if not seconds:
            return [self.t("targeting"), driver, *self.sector_name(sector)]
        return [self.t("targeting"), driver, *self.sector_name(sector),
                *self.lap_time(seconds)]

    def no_sector_for(self, driver: str, sector: int) -> Utterance:
        return [driver, self.t("has no time in"), *self.sector_name(sector),
                self.t("yet")]

    # -- answers to questions ---------------------------------------------

    def fastest_lap_answer(self, driver: str, seconds: float, *,
                           vehicle_class: str = "") -> Utterance:
        """Who has the fastest lap, when asked rather than when it happens.

        The class is said back when one was involved. On an endurance grid
        "Estre, one fifty two eight" is ambiguous about which race it is an
        answer to, and the driver asked precisely because they did not know.
        """
        answer = [driver, *self.lap_time(seconds)]
        return [*answer, self.t("in"), vehicle_class] if vehicle_class else answer

    def fastest_sector_answer(self, driver: str, sector: int, seconds: float, *,
                              vehicle_class: str = "") -> Utterance:
        answer = [driver, *self.sector_name(sector), *self.lap_time(seconds)]
        return [*answer, self.t("in"), vehicle_class] if vehicle_class else answer

    def best_lap_answer(self, seconds: float) -> Utterance:
        return [self.t("your best"), *self.lap_time(seconds)]

    def fuel_answer(self, percent: float, laps: float) -> Utterance:
        """The fill for a stop, as the sim's own screen wants it.

        A percentage, because that is the number on the fuel screen and the
        driver has about four seconds to dial it in. The lap count comes after
        it so the short answer is heard first — somebody on the way to the pit
        entry who catches only the first two words has still got what they
        needed.
        """
        return [self.t("fill to"), *self.percent(percent),
                self.t("for"), self.number(int(laps)), self.t("laps")]

    def fuel_will_not_reach(self) -> Utterance:
        """A full tank is not enough, which is a different answer.

        Not "one hundred percent": that would be heard as an answer to the
        question asked, and the driver would plan a race on one stop that
        needs two.
        """
        return [self.t("fill it"), self.t("that won't reach the end")]

    def no_fuel_data_yet(self) -> Utterance:
        """Nothing has been burnt yet, so nothing can be worked out.

        Said rather than guessed at. A fuel number invented from nothing is the
        one wrong answer here that ends somebody's race.
        """
        return [self.t("no fuel data yet")]

    def percent(self, value: float) -> Utterance:
        """A tank fill, as words in English and digits elsewhere.

        The same rule as every other number here: number grammar is
        per-language, and doing it half-well produces confident nonsense in
        somebody's own language.
        """
        if not self.catalogue.english:
            return [f"{int(value)}%"]
        return [self.number(int(value)), self.t("percent")]

    def no_time_yet(self, vehicle_class: str = "") -> Utterance:
        """Nobody has set one. An answer, and not the same as saying nothing."""
        if vehicle_class:
            return [self.t("no time in"), vehicle_class, self.t("yet")]
        return [self.t("no time yet")]

    def no_such_class(self) -> Utterance:
        """A class was named that nobody on this grid is in.

        Said rather than guessed at. Falling back to the overall answer would
        be a wrong answer stated confidently, and the driver would have no way
        to tell.
        """
        return [self.t("nobody is in that class")]

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
#: **Numbers are not in here** — they come out of `number()` and are listed by
#: `vocabulary()` instead, because there are a hundred of them and enumerating
#: them by hand would be a list that silently went stale. A driver's name is
#: not in here either, and never can be.
FIXED_LINES = (
    "nothing in it", "half a second", "a tenth", "tenths", "one second",
    "seconds", "point", "oh", "percent", "turn", "sector",
    # Kept for translators: outside English these stay whole phrases, because
    # word order is theirs to decide. See `Script.turn_name`.
    "{count} tenths", "{count} seconds", "turn {number}", "sector {number}",
    "go ahead", "say again", "standing down",
    "targeting", "best lap", "has no lap on record yet", "I can't find",
    "no reference lap yet; keep going", "that's your best", "last lap",
    "was faster on the exit", "had a better entry",
    "you were faster on the exit", "you had a better entry",
    "faster exit", "better entry", "yours",
    "radio check", "car left", "car right",
    "three wide you're on the left", "three wide you're on the right",
    "in the middle", "hold your line",
    "clear", "clear left", "clear right",
    "clear all round", "still there", "car stopped ahead",
    "full course yellow", "green flag", "blue flag", "car stopped in",
    "clear to go", "hold",
    "{routine} running", "{routine} off",
    "fastest lap of the session", "has the fastest lap",
    "in", "your best", "no time in", "no time yet", "nobody is in that class",
    "fill to", "for", "laps", "fill it", "that won't reach the end",
    "no fuel data yet",
    "fastest", "has taken", "best", "down", "up", "is ahead by",
    "you are ahead by", "has no time in", "yet", "which sector?",
    "give me a lap to find the sectors",
)


#: The largest number the engineer can be asked to say.
#:
#: Ninety-nine covers everything: seconds in a lap time top out at 59, a fill
#: percentage at 99 (a hundred is "fill it" instead, which is a different call),
#: and a lap count beyond this is a race nobody is fuelling one stop for.
MAX_SPOKEN_NUMBER = 99


def vocabulary(catalogue: i18n.Catalogue) -> list[str]:
    """Every fragment the engineer can say, for a voice pack to record.

    `FIXED_LINES` plus the numbers, which are generated rather than listed —
    a hand-kept list of a hundred number words is a list that goes stale
    without anything failing.

    **Templates are excluded and that is not a gap.** A line with a `{}` in it
    is assembled from fragments that are themselves in here, so recording the
    template would record a phrase the engineer never actually says.
    """
    script = Script(catalogue)
    spoken = [catalogue.translate(line) for line in FIXED_LINES
              if "{" not in line]
    spoken.extend(script.number(value)
                  for value in range(MAX_SPOKEN_NUMBER + 1))
    # Order-preserving dedupe: "one" is both a number and part of "one second".
    return list(dict.fromkeys(phrase for phrase in spoken if phrase.strip()))
