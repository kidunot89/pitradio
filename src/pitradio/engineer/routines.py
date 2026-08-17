"""On-track routines: started by voice, running until they are done.

A routine is **a set of notifications with its own conditions and messages**.
That is not a description, it is the implementation: a routine hands back
`Notification` objects and the same runner that drives the always-on behaviours
drives those too, with the same repeat rules. There is no second mechanism, and
a routine cannot say anything a behaviour could not.

The difference between the two is only how they are switched on. A behaviour is
a tick-box in Settings and runs for as long as the engineer does. A routine is
started by saying something, may take parameters, and stands down when it is
finished, when its own end phrase is said, or when the global stop is.

**Registration is static**, exactly as for sim plugins and for the same
reason — Nuitka cannot follow an import it never sees, so a build that scanned
a directory would ship with no routines and no error.

**Trigger phrases live on the config, not on the routine.** The routine's own
are only defaults. Somebody who wants theirs to start on "initiate build
procedures" types that in the Engineer tab, and a translated build gets its
phrases through the catalogue. What a routine is called is not the routine.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pitradio import mentions
from pitradio.engineer import (
    coaching,
    lines,
    notifications,
    phrases,
    sectors,
    spotter,
)
from pitradio.plugins import base
from pitradio.plugins.base import SessionInfo, Standings

log = logging.getLogger(__name__)

#: How much time in half a corner is worth interrupting a driver for. Below
#: this it is sampling noise as much as driving, and an engineer that talks
#: about noise is one nobody listens to.
DEFAULT_THRESHOLD = 0.08

#: Ids are stored in config; never rename one in place.
HOT_LAP_TRAINER = "hot_lap_trainer"
SECTOR_TRAINER = "sector_trainer"


@dataclass
class Context:
    """What a notification or routine can see and do this tick.

    Rebuilt every tick rather than held, so nothing here can keep a stale
    session — which is the mistake that makes one of these confidently describe
    a race that finished ten minutes ago.
    """

    script: lines.Script
    book: coaching.LapBook
    sectors: sectors.SectorBook = field(default_factory=sectors.SectorBook)
    session: SessionInfo = field(default_factory=SessionInfo)
    standings: Standings = field(default_factory=Standings)
    #: The lap the player finished on this tick, if any.
    finished_lap: coaching.LapTrace | None = None
    #: Sectors anybody finished on this tick.
    finished_sectors: tuple[sectors.SectorTime, ...] = ()
    # -- the spotter's geometry, from the sim's own plugin settings --------
    #
    # All three are on the profile rather than in the engineer's config,
    # because they depend on the sim and the cars in it rather than on the
    # driver's taste. A number that suits one game is wrong in the next.

    #: Which side is which, per the sim's axes — see spotter.py.
    swap_sides: bool = False
    #: How far apart along the track two cars can be and still be alongside,
    #: once they already are.
    alongside_metres: float = spotter.DEFAULT_ALONGSIDE_METRES
    #: And how close they must come before the call is made at all.
    overlap_metres: float = spotter.DEFAULT_OVERLAP_METRES
    #: How far to the side still counts as beside you rather than elsewhere.
    width_metres: float = spotter.DEFAULT_WIDTH_METRES

    #: Whether calls about other cars are limited to the player's own class.
    own_class_only: bool = True

    #: Corner and sector deltas below this are not worth a call.
    threshold: float = DEFAULT_THRESHOLD
    sector_threshold: float = DEFAULT_THRESHOLD
    say: object = None

    def speak(self, utterance: list[str], *, urgent: bool = False) -> None:
        if self.say is not None and utterance:
            self.say(utterance, urgent=urgent)

    def driver_names(self) -> list[str]:
        return [car.driver for car in self.session.cars if car.driver]

    def my_class(self) -> str:
        """The class to judge other cars against, or "" for the whole field.

        Empty when the sim has no classes, when the player's car is not in the
        block, or when the driver has asked for the overall picture — all of
        which mean the same thing to a caller: do not filter.
        """
        if not self.own_class_only:
            return ""
        own = self.session.player()
        return str(getattr(own, "vehicle_class", "") or "") if own else ""

    def own_car(self):
        """The car being driven from this machine, or None.

        Not `listener()`: that is the car on *screen*, which is right for
        deciding who you can hear and wrong for deciding whose driving to
        comment on. Coaching somebody on a lap they are watching rather than
        driving would be nonsense.
        """
        own = self.session.player()
        return own if own is not None and own.control == 0 else None


# -- naming a target ------------------------------------------------------

_PLACE = re.compile(r"\bp\s*-?\s*(\d{1,2})\b")
_SECTOR = re.compile(r"\b(?:sector|sec|s)\s*-?\s*([123])\b")
_LEADER = ("leader", "the leader", "p1", "first")
_FASTEST = ("fastest", "the fastest", "fastest lap", "the fastest lap", "best")

_ORDINAL_SECTORS = {"first": 1, "second": 2, "third": 3, "one": 1, "two": 2,
                    "three": 3}


@dataclass(frozen=True)
class Target:
    """What a routine was pointed at.

    Both parts are optional and a routine decides which it needs. Parsed from
    one blob of words rather than positionally, so "GT3 P1 sector 3" and
    "sector 3, GT3 P1" are the same instruction — which matters, because
    Whisper's commas are not to be relied on and nobody says a command the same
    way twice.
    """

    driver: str = ""
    sector: int = 0


def parse_sector(argument: str) -> tuple[int, str]:
    """(sector, what is left of the words). Zero when none was named."""
    words = " ".join(phrases.words(argument))
    match = _SECTOR.search(words)
    if match:
        return int(match.group(1)), (words[:match.start()] + words[match.end():]).strip()

    # "sector three", which is what Whisper produces at least as often.
    spelled = re.search(
        r"\b(?:sector|sec)\s+(" + "|".join(_ORDINAL_SECTORS) + r")\b", words)
    if spelled:
        return (_ORDINAL_SECTORS[spelled.group(1)],
                (words[:spelled.start()] + words[spelled.end():]).strip())
    return 0, words


def _class_aliases(classes: dict[str, dict[int, str]]) -> dict[str, str]:
    """alias -> class name, with anything two classes share dropped.

    The same rule chat mentions use, and for the same reason: an alias two
    classes answer to is a coin toss, and resolving it wrongly points the
    routine at somebody else entirely.
    """
    mapping: dict[str, str] = {}
    clashes: set[str] = set()
    for name in classes:
        for alias in mentions.class_aliases(name):
            if alias in mapping and mapping[alias] != name:
                clashes.add(alias)
            mapping[alias] = name
    for alias in clashes:
        mapping.pop(alias, None)
    return mapping


def resolve_driver(argument: str, context: Context) -> str:
    """Whoever the driver meant, or "".

    Five ways to name somebody, in the order they are unambiguous:

    * nothing at all — whoever is quickest
    * a class and a place, "GT3 P1" — the one the timing screen shows in that
      class, which on a multi-class grid is the only way "P1" has one answer
    * a bare place, "P3" — the overall order
    * "the leader"
    * a name — matched loosely, because unlike a chat mention a wrong match
      here costs a routine that is visibly coaching the wrong person rather
      than a message sent to one
    """
    # Anyone in the session, plus anyone with a lap on record. The second half
    # matters more than it looks: a driver who has left still has a reference
    # lap worth chasing, and their name is what the driver saw on the timing
    # screen before they went.
    names = list(dict.fromkeys([*context.driver_names(), *context.book.best]))
    words = phrases.words(argument)
    folded = " ".join(words)

    if not folded or folded in _FASTEST:
        fastest = context.book.fastest()
        if fastest is not None:
            return fastest.driver
        return context.standings.overall.get(1, "")

    if folded in _LEADER:
        return context.standings.overall.get(1, "")

    place = _PLACE.search(folded)
    if place:
        wanted = int(place.group(1))
        # Anything in front of the place might be a class. Checked against the
        # classes actually on this grid, never guessed at.
        spoken = folded[:place.start()].strip()
        if spoken and context.standings.by_class:
            aliases = _class_aliases(context.standings.by_class)
            squashed = "".join(spoken.split())
            for alias, name in aliases.items():
                if squashed.endswith(alias):
                    # A class that was named decides. Falling back to the
                    # overall order here would answer a question nobody asked.
                    return context.standings.by_class.get(name, {}).get(wanted, "")
        return context.standings.overall.get(wanted, "")

    found = mentions.find_mentions(argument, names, fuzzy=True, threshold=0.8)
    return found[0] if found else ""


def parse_target(argument: str, context: Context) -> Target:
    sector, rest = parse_sector(argument)
    return Target(resolve_driver(rest, context), sector)


# -- the base -------------------------------------------------------------


class Routine:
    """Base class. Subclasses override what they need and nothing else."""

    #: Stored in config; never rename one in place.
    id: str = ""
    name: str = "unnamed"
    description: str = ""
    #: What starts it. A phrase ending in a `{placeholder}` takes everything
    #: said after it as its parameters.
    phrases: tuple[str, ...] = ()
    #: What stops it, as well as the global stop phrases.
    end_phrases: tuple[str, ...] = ()
    #: Human-readable, for the tab: what it expects to be told.
    parameters: str = ""

    def __init__(self) -> None:
        self.runner = notifications.Runner(self.notifications())
        #: Everything a routine owns is on while it runs. There is no reason to
        #: give a driver a second set of tick-boxes for something they turned
        #: on by speaking.
        self.settings = notifications.Settings(default_on=True)

    def notifications(self) -> list[notifications.Notification]:
        """The behaviours this routine is made of."""
        return []

    def start(self, context: Context, argument: str = "") -> bool:
        """Begin. False if it could not, having said why.

        Say something either way: silence here is indistinguishable from the
        command not having been heard at all.
        """
        return True

    def stop(self, context: Context) -> None:
        """Stand down. Called when stopped, when another starts, and on exit."""
        self.runner.reset()

    def finished(self) -> bool:
        """Whether it has run its course and should stand itself down."""
        return False

    def running_state(self) -> str:
        """A line for the Engineer tab, so it is visibly doing something."""
        return ""

    def tick(self, context: Context, now: float,
             provided=None) -> list[notifications.Call]:
        return self.runner.run(context, now, self.settings, provided)


# -- comparing corners against somebody's lap ------------------------------


class CornerComparison(notifications.Notification):
    """Corner by corner against a target's best lap.

    The reference is somebody else's *best* lap rather than a theoretical or an
    average, because it is the only lap anybody can argue with: it happened, on
    this track, in this session, and the driver can see who set it.

    What it says is a comparison, never an instruction. The app knows one lap
    was quicker between two points; it does not know whether that was braking,
    line, tyres or a tow, and "brake later" would be a guess wearing the
    clothes of coaching.
    """

    id = "corner_comparison"
    name = "Corner comparison"
    description = "Entry and exit deltas against the target's best lap."
    #: A lap trace is lap distance, speed and lap times per car. Without them
    #: there is no reference lap and nothing to compare against.
    requires = (base.PROVIDES_LAPS,)

    def __init__(self, sector: int = 0) -> None:
        #: Restrict to one sector, for the sector trainer. Zero is the whole lap.
        self.only_sector = sector
        self.target = ""
        self.corners: list[coaching.Corner] = []
        #: The trace the corners came from, so they are recomputed when the
        #: target improves and not on every tick.
        self._reference: coaching.LapTrace | None = None
        self._called: set[int] = set()
        self._distance = 0.0

    def reset(self) -> None:
        self.corners = []
        self._reference = None
        self._called = set()
        self._distance = 0.0

    def check(self, context) -> list[notifications.Call]:
        if not self.target:
            return []

        own = context.own_car()
        if own is None or own.in_pits:
            return []

        reference = context.book.best_for(self.target)
        if reference is None:
            return []
        if reference is not self._reference:
            # A new best lap from the target: the corners move, so everything
            # already called this lap is against a lap that no longer exists.
            self._reference = reference
            self.corners = self._corners_in(coaching.find_corners(reference), context)
            self._called = set()
            log.info("%s: %d corner(s) from %s's %.3fs lap",
                     self.id, len(self.corners), self.target, reference.lap_time)

        mine = context.book.current.get(own.driver)
        if mine is None or not self.corners:
            return []

        distance = float(own.lap_dist)
        if distance < self._distance:
            # Crossed the line. The corners are the same; the lap being
            # compared against them is not.
            self._called = set()
        previous, self._distance = self._distance, distance

        calls: list[notifications.Call] = []
        for corner in self.corners:
            if corner.number in self._called:
                continue
            # Called once the car is clear of the corner, not at the apex: the
            # exit cannot be compared until it has been driven.
            if not previous < corner.exit <= distance:
                continue
            self._called.add(corner.number)
            call = self._call(context, corner, mine, reference)
            if call is not None:
                calls.append(call)
        return calls

    def _corners_in(self, corners, context) -> list[coaching.Corner]:
        """Only the corners in the sector being worked on, if one was named."""
        if not self.only_sector:
            return corners
        kept = [corner for corner in corners
                if context.sectors.sector_at(corner.apex) == self.only_sector]
        # Boundaries not learned yet: keep them all rather than silently
        # coaching nothing. `sector_at` returns None until a car has been seen
        # crossing each one, and that is a lap away at most.
        return kept if kept else corners

    def _call(self, context, corner, mine, reference):
        deltas = coaching.compare_corner(mine, reference, corner)
        verdict = coaching.worst(deltas, context.threshold)
        if verdict is None:
            # The two laps agree through this corner. Saying so every time is
            # what makes an engineer background noise.
            return None
        return notifications.Call(
            f"corner:{corner.number}:{verdict.phase}",
            context.script.corner_call(
                self.target, corner.number, verdict.phase, verdict.seconds),
        )


class SectorComparison(notifications.Notification):
    """Your time in one sector against the target's best in it."""

    id = "sector_comparison"
    name = "Sector comparison"
    description = "Your sector time against the target's best."
    requires = (base.PROVIDES_SECTORS,)

    def __init__(self, sector: int = 0) -> None:
        self.only_sector = sector
        self.target = ""

    def check(self, context) -> list[notifications.Call]:
        own = context.own_car()
        if own is None or not self.target:
            return []

        calls: list[notifications.Call] = []
        for finished in context.finished_sectors:
            if finished.driver != own.driver:
                continue
            if self.only_sector and finished.sector != self.only_sector:
                continue
            theirs = context.sectors.best_for(self.target, finished.sector)
            if theirs <= 0:
                continue
            delta = finished.seconds - theirs
            if abs(delta) < context.sector_threshold:
                continue
            calls.append(notifications.Call(
                f"sector:{finished.sector}:{finished.seconds:.3f}",
                context.script.sector_target_call(
                    self.target, finished.sector, delta),
            ))
        return calls


# -- the routines that ship -----------------------------------------------


class HotLapTrainer(Routine):
    """Chase one driver's quickest lap, corner by corner, for as long as you like.

    Runs until it is stopped: there is no lap count at which somebody has
    finished learning a circuit, and a routine that switched itself off after
    three laps would be switching off in the middle of the run that was working.
    """

    id = HOT_LAP_TRAINER
    name = "Hot lap trainer"
    description = (
        "Targets one driver's best lap and, at each corner, says whether they "
        "were quicker on the entry or the exit and by how much."
    )
    phrases = (
        "begin hot lap trainer {target}",
        "start hot lap trainer {target}",
        "hot lap trainer {target}",
    )
    end_phrases = ("end hot lap trainer", "stop hot lap trainer")
    parameters = "a driver — a name, \"P3\", \"GT3 P1\", or nothing for the quickest"

    def __init__(self) -> None:
        self.corners = CornerComparison()
        super().__init__()

    def notifications(self):
        return [self.corners]

    @property
    def target(self) -> str:
        return self.corners.target

    def start(self, context: Context, argument: str = "") -> bool:
        driver = parse_target(argument, context).driver
        if not driver:
            context.speak(context.script.no_such_driver(argument or "anyone"))
            return False

        self.corners.reset()
        self.corners.target = driver
        self.runner.reset()

        best = context.book.best_for(driver)
        if best is None:
            # Named somebody real who has not set a lap yet. Worth taking the
            # target anyway: they probably will, and the routine starts working
            # the moment they do without being asked again.
            context.speak(context.script.no_lap_for(driver))
            return True
        context.speak(context.script.targeting(driver, best.lap_time))
        return True

    def stop(self, context: Context) -> None:
        self.corners.target = ""
        self.corners.reset()
        super().stop(context)

    def running_state(self) -> str:
        if not self.target:
            return ""
        return f"chasing {self.target}, {len(self.corners.corners)} corner(s) mapped"


class SectorTrainer(Routine):
    """One sector, one driver to chase, until you have it.

    A whole lap is a lot to hold in your head. Working one sector is how
    somebody actually learns a circuit, and it is the case the hot lap trainer
    handles badly — its calls are spread over a lap and by the time you are
    back at the corner you got wrong you have driven fifteen others.
    """

    id = SECTOR_TRAINER
    name = "Sector trainer"
    description = (
        "Works one sector against one driver: their sector time to beat, and "
        "the corners inside it."
    )
    phrases = (
        "begin sector trainer {target}",
        "start sector trainer {target}",
        "sector trainer {target}",
    )
    end_phrases = ("end sector trainer", "stop sector trainer")
    parameters = "a driver and a sector — \"GT3 P1, sector 3\""

    def __init__(self) -> None:
        self.corners = CornerComparison()
        self.split = SectorComparison()
        super().__init__()

    def notifications(self):
        return [self.split, self.corners]

    @property
    def target(self) -> str:
        return self.split.target

    @property
    def sector(self) -> int:
        return self.split.only_sector

    def start(self, context: Context, argument: str = "") -> bool:
        target = parse_target(argument, context)
        if not target.sector:
            # A sector trainer with no sector is a hot lap trainer, and quietly
            # becoming one would be a surprise. Ask.
            context.speak(context.script.which_sector())
            return False
        if not target.driver:
            context.speak(context.script.no_such_driver(argument or "anyone"))
            return False

        self.corners.reset()
        self.split.only_sector = target.sector
        self.split.target = target.driver
        self.corners.only_sector = target.sector
        self.corners.target = target.driver
        self.runner.reset()

        theirs = context.sectors.best_for(target.driver, target.sector)
        if theirs <= 0:
            context.speak(
                context.script.no_sector_for(target.driver, target.sector))
            return True
        context.speak(
            context.script.targeting_sector(target.driver, target.sector, theirs))
        return True

    def stop(self, context: Context) -> None:
        self.split.target = ""
        self.split.only_sector = 0
        self.corners.target = ""
        self.corners.only_sector = 0
        self.corners.reset()
        super().stop(context)

    def running_state(self) -> str:
        if not self.target:
            return ""
        return f"sector {self.sector} against {self.target}"


#: Every routine that ships. Static, so a compiled build has them.
BUILTIN: tuple[type[Routine], ...] = (HotLapTrainer, SectorTrainer)


def build() -> list[Routine]:
    """One instance of each, skipping any that will not construct."""
    made: list[Routine] = []
    for cls in BUILTIN:
        try:
            made.append(cls())
        except Exception:
            log.exception("could not create routine %s", getattr(cls, "id", cls))
    return made


def describe() -> list[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str]]:
    """(id, name, description, phrases, end phrases, parameters) for the tab."""
    return [(cls.id, cls.name, cls.description, cls.phrases, cls.end_phrases,
             cls.parameters) for cls in BUILTIN]
