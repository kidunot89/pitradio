"""What a sim plugin provides.

Some things PitRadio can do are specific to one sim, because they need that
sim's own data interface. Reading who is in the session is the first: Le Mans
Ultimate exposes it in shared memory, and nothing about that generalises.

A plugin answers "what is happening in this session" for the executables it
claims. Everything else — the hook, injection, transcription — stays sim-
agnostic and never learns which plugin, if any, is active.

Plugins must degrade quietly. The sim not running, the interface being absent,
the layout having changed after a game update: all of these are normal, and
none of them may raise into the trigger cycle. Returning nothing is always a
valid answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# -- what a sim can tell us ----------------------------------------------
#
# Sims do not expose the same things, and the differences are not small. LMU
# publishes every car's world position, which is what makes a spotter possible;
# iRacing publishes how far round the lap each car is and nothing about where
# that is in space, so the same calculation cannot be done at all — it has its
# own left/right field instead.
#
# A plugin therefore says what it can supply, and a behaviour that needs
# something absent is **skipped rather than run**. The alternative is a
# tick-box that is on, looks fine, and never says anything — which is
# indistinguishable from the feature being broken.

#: Every car's world position, in metres. Needed by the spotter's geometry and
#: by proximity voice.
PROVIDES_POSITIONS = "positions"
#: Lap counts and lap times per car.
PROVIDES_LAPS = "laps"
#: Sector index and the cumulative splits, so sector times can be derived.
PROVIDES_SECTORS = "sectors"
#: A left/right call the sim makes itself, for sims that do that rather than
#: handing over positions.
PROVIDES_SPOTTER = "spotter"
#: Lap data for cars *other than the player's*, and names to attach it to.
#:
#: The distinction is not pedantry. Assetto Corsa publishes every car's world
#: position but lap times for the player alone, and no driver names at all — so
#: "somebody has taken the fastest lap of the session" would fire when you beat
#: your own, naming a field of one. Anything comparing you to the rest of the
#: grid needs this as well as `PROVIDES_LAPS`.
PROVIDES_FIELD = "field"

#: Yellow flags, full-course cautions and blue flags.
#:
#: Separate from `PROVIDES_POSITIONS` because a sim can publish where every car
#: is and still say nothing about whether the marshals have a flag out — and
#: the difference matters: a stopped car is a fact about one car, a yellow is a
#: fact about a piece of track, and only the sim knows the second one.
PROVIDES_FLAGS = "flags"


@dataclass(frozen=True)
class PluginSetting:
    """One option a plugin exposes, rendered in the profile editor.

    Settings are stored per profile rather than per plugin: a plugin can serve
    several games, and what makes sense in one may not in another.
    """

    key: str
    label: str
    kind: str = "bool"          # bool | int | text
    default: Any = False
    help: str = ""


@dataclass(frozen=True)
class Car:
    """One car in the session, as the sim currently sees it.

    `slot` is the sim's own id for the entry and may be reused after someone
    leaves, so it identifies a car within a snapshot and nothing longer-lived.
    Who someone *is* across a session is their driver name, which is also the
    only thing other drivers ever see.
    """

    slot: int
    driver: str
    place: int = 0
    vehicle_class: str = ""
    #: Who is driving: 0 local player, 1 local AI, 2 remote, 3 replay, -1 none.
    #: The player's own car reads 1 while somebody is spectating, which is the
    #: only way to tell that they are.
    control: int = -1
    #: World position in metres. Every car's, not just the player's — which is
    #: what lets proximity be decided locally, without publishing anything.
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    is_player: bool = False

    # -- where they are in the lap ---------------------------------------
    #
    # Added for the engineer, which needs to know how a lap is going rather
    # than only who is in it. All of it comes from the same read as the fields
    # above, because it has to: a car's distance round the lap and its speed
    # are only comparable if they were true at the same instant, and a second
    # read is a different instant.

    #: Metres round the lap. The x-axis of everything the coaching routine does.
    lap_dist: float = 0.0
    #: Metres per second.
    speed: float = 0.0
    #: Laps completed, which is how the end of one is detected.
    laps: int = 0
    #: Seconds. Zero means "no valid lap", not "instant lap" — an out lap, a
    #: lap that was still running, or a car that has just joined.
    last_lap: float = 0.0
    best_lap: float = 0.0
    #: Between pit entry and pit exit. A lap with any of this in it is not a
    #: lap anybody should be measured against.
    in_pits: bool = False

    # -- sectors ----------------------------------------------------------
    #
    # Sector times are what a driver actually compares themselves on, and a
    # sector is short enough to remember what you did in it. The sim publishes
    # them cumulatively, so sector 2 is "sector 1 plus sector 2" and sector 3
    # is only knowable once the lap is done — `engineer/sectors.py` untangles
    # that, and nothing else should have to.

    #: Which sector the car is in. The sim's own numbering, which is not the
    #: obvious one: **0 is sector 3**, 1 is sector 1, 2 is sector 2.
    sector: int = 0
    #: Cumulative splits for the lap in progress. Zero means "not set yet".
    cur_sector1: float = 0.0
    cur_sector2: float = 0.0
    #: Cumulative splits for the last completed lap.
    last_sector1: float = 0.0
    last_sector2: float = 0.0

    #: The flag being shown to *this* car. Blue is the one that is per-car
    #: rather than per-track: it means somebody quicker is about to arrive,
    #: and only the sim knows who it is being shown to.
    blue_flag: bool = False


@dataclass(frozen=True)
class SessionInfo:
    """Which session this is, and who is in it.

    `key` identifies the *game server*, so two PitRadio users on the same server
    agree on it without either of them publishing which server that is. Empty
    when there is no server — offline and single-player have no room to join,
    and that is an answer rather than a failure.
    """

    key: str = ""
    track: str = ""
    cars: tuple[Car, ...] = ()
    #: Slot of the car the camera is on, when the sim will say. Not the same as
    #: the player's own car — see `listener`.
    focus_slot: int | None = None
    #: Metres in a lap. The engineer uses it to tell a full recorded lap from a
    #: partial one; zero means the sim did not say, and a lap is then kept on
    #: the sim's own word alone.
    track_length: float = 0.0
    #: The session clock, in seconds. **Not** wall time: lap traces are
    #: compared against each other, so what matters is that every sample in a
    #: session came off the same clock, and the sim's own is the only one that
    #: is true for the cars as well as for us.
    elapsed: float = 0.0
    #: Which sides have cars on them right now, as **the sim itself says**:
    #: side name -> how many. None means "the sim does not do this", and the
    #: spotter works it out from world positions instead.
    #:
    #: This exists for iRacing, which publishes no other-car world positions at
    #: all — only how far round the lap each one is — and so cannot be given a
    #: geometric spotter. It does publish `CarLeftRight`, which is a better
    #: answer than any geometry: it is the sim's own, computed from the real
    #: car bodies rather than from a point and a guessed width.
    alongside: dict[str, int] | None = None

    #: Which sectors have a local yellow out, indexed **the way a person counts
    #: them**: 1, 2, 3. Empty when the sim does not publish flags at all, which
    #: is not the same as an entry that is False — that means the sector is
    #: known to be clear.
    sector_yellow: dict[int, bool] = field(default_factory=dict)
    #: Whether the whole circuit is under caution. Distinct from a local
    #: yellow in every sector: it changes what the driver may do, not merely
    #: where they must be careful.
    full_course_yellow: bool = False

    def player(self) -> Car | None:
        """The car belonging to this installation, driven or not."""
        return next((car for car in self.cars if car.is_player), None)

    def positions(self) -> dict[str, tuple[float, float, float]]:
        """Driver name -> where they are *now*, for judging an arriving clip.

        Fresher than the position the clip carries, which was true when its
        speaker pressed the button and is a hundred metres old by the time
        anyone hears it. See `voice.locate`.
        """
        return {car.driver: car.position for car in self.cars if car.driver}

    def driving(self) -> bool:
        """Whether the person here is actually driving their car.

        `mControl` reads 0 for the local player and 1 once the AI has it, which
        is what happens when somebody hands over and watches. It is the only
        spectating signal the block has.
        """
        own = self.player()
        return own is not None and own.control == 0

    def listener(self) -> Car | None:
        """The car to measure proximity from: the one on screen.

        Spectating a team mate, that is *their* car — watching a battle you are
        in the middle of while hearing the radio from four kilometres away,
        where your own car is parked, is not proximity in any sense a viewer
        would recognise. It has to be detected rather than asked about: someone
        racing cannot reach a dropdown, and someone spectating should not have
        to.

        Falls back to the driven car, and then to None, which callers read as
        "cannot tell" and therefore as audible. Silently keeping a parked car as
        the reference would filter the session by a place nobody is looking at,
        and no listener could tell that apart from the feature being broken.
        """
        if self.focus_slot is not None:
            watched = next(
                (car for car in self.cars if car.slot == self.focus_slot), None)
            if watched is not None:
                return watched
        return self.player() if self.driving() else None

    @property
    def has_data(self) -> bool:
        """Whether the sim is telling us about cars at all.

        Not the same question as `__bool__`, which asks whether there is a
        *room* to be in. Voice needs a server; the engineer does not — a
        practice session on your own is the most likely place to want a
        coaching routine, and it has no server and never will.
        """
        return bool(self.cars)

    def __bool__(self) -> bool:
        return bool(self.key)


@dataclass(frozen=True)
class Standings:
    """Who is where, overall and within each class.

    Endurance racing is multi-class, so "P3" has more than one answer: there is
    a P3 in Hypercar, a P3 in LMP2 and a P3 in LMGT3, and they are three
    different people. Both orders are needed — a bare "P3" means the overall
    one, because that is the column the timing screen shows.

    One object rather than two calls, so both come from the same read of the
    sim. Taken separately they would be two snapshots of a block that updates
    many times a second, and a name could resolve from a frame the other half
    never saw.
    """

    #: overall place -> driver name
    overall: dict[int, str] = field(default_factory=dict)
    #: class name -> place within that class -> driver name
    by_class: dict[str, dict[int, str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.overall or self.by_class)


# -- what a sim is able to tell us ---------------------------------------
#
# Sims do not publish the same things, and the differences are not small. LMU
# hands over every car's world position; iRacing hands over how far round the
# lap each car is and nothing about where that is in space. A spotter can be
# built from the first and not from the second.
#
# So a plugin says what it has, and the engineer asks before relying on it.
# The alternative — every behaviour reading zeros and quietly saying nothing —
# is indistinguishable from the feature being broken, which is the failure mode
# this whole codebase keeps trying to avoid.

class SessionPlugin:
    """Base for a per-sim data source. Subclasses override what they can."""

    #: Stable identifier stored in profiles. Never rename one in place —
    #: existing configs reference it.
    id: str = ""
    #: Shown in the GUI.
    name: str = "unnamed"
    #: Executables this plugin is assumed to suit, used only to pick a default
    #: for a profile that has not chosen. The profile is always authoritative,
    #: so one plugin can serve as many games as work with it.
    executables: tuple[str, ...] = ()
    #: One line explaining what it adds, for the plugin list.
    description: str = ""
    #: Options shown in the profile editor when this plugin is assigned.
    settings: tuple[PluginSetting, ...] = ()
    #: What this sim can tell the engineer — see the PROVIDES_* constants.
    #: Empty by default, so a plugin that says nothing gets no behaviours
    #: rather than behaviours that quietly do nothing.
    provides: frozenset[str] = frozenset()
    #: Whether nobody has been able to run this against the game it reads.
    #:
    #: A stronger statement than "not tested yet". Every reader here is checked
    #: against a block built by hand, which catches a wrong width or a bad
    #: sentinel and cannot catch a wrong *assumption* about what a sim puts in
    #: a field — and only a copy of the game settles that. A plugin nobody
    #: working on this owns is one where that will not happen on its own, so it
    #: says so in the picker rather than looking like the others.
    experimental: bool = False
    #: Why, in one line, shown beside the label.
    experimental_note: str = ""

    def defaults(self) -> dict[str, Any]:
        return {setting.key: setting.default for setting in self.settings}

    def label(self) -> str:
        """How the plugin is named in the profile editor."""
        return f"{self.name} (experimental)" if self.experimental else self.name

    def serves(self, executable: str | None) -> bool:
        return (executable or "").strip().lower() in self.executables

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Attach to the sim. Called once; must not raise."""

    def stop(self) -> None:
        """Detach. Called on shutdown; must not raise."""

    # -- data ------------------------------------------------------------

    def is_connected(self) -> bool:
        """Whether the sim's data is readable right now."""
        return False

    def drivers(self) -> list[str]:
        """Driver names in the current session, or an empty list.

        Empty is not an error: the sim may be closed, in a menu, or the
        interface may have moved. Callers must treat it as "no information".
        """
        return []

    def vocabulary(self) -> list[str]:
        """Terms to feed Whisper's initial_prompt for this session.

        Separate from drivers() because the two answer different questions.
        Mentions need *names* to match against; the prompt just needs words
        Whisper would otherwise have no reason to expect — which for another
        sim might be car names, teams, tracks or commentators rather than
        people. Defaults to the driver list, which is the common case.
        """
        return self.drivers()

    def session(self) -> SessionInfo:
        """Which session this is and who is in it, or empty.

        What voice needs and standings do not: an id every client on the same
        server agrees on, and where each car is. A sim with no server interface
        returns nothing and voice stays off for it, which is correct — there is
        nobody to be in a room with.
        """
        return SessionInfo()

    def standings(self) -> Standings:
        """Who is in which place, overall and per class, or empty.

        Lets someone say "P3" instead of a name they cannot pronounce or did
        not catch — which on a full grid is most of them — and "GT3 P3" when
        the grid has more than one class in it.
        """
        return Standings()

    def positions(self) -> dict[int, str]:
        """The overall order alone.

        Kept because it is the simple case and plenty of sims have nothing
        else; implement `standings` and this follows.
        """
        return self.standings().overall

    def status(self) -> str:
        """A line for the GUI's plugin list — connected, idle, or why not."""
        return "not connected"
