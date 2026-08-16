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

    def defaults(self) -> dict[str, Any]:
        return {setting.key: setting.default for setting in self.settings}

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
