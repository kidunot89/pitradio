"""Behaviours: the things the engineer says without being asked.

A notification watches one condition and says one thing when it is met. They
are switched on and off individually in Settings → Engineer, and they run for
as long as the engineer does — unlike a routine, which is started by voice and
stands down again.

**Every notification has a repeat interval**, and it is the part worth
understanding. A call is identified by a *key*, not by the moment it happened:

* A key that has not been said before is said immediately.
* A key already said is said again once the repeat interval has passed — which
  is what makes the spotter keep telling you there is still a car there.
* A **different** key jumps the interval, because it is new information. A car
  arriving on your left while one sits on your right is not a repeat.

Set the interval to zero and a call is made once per change and never repeated.
That is right for a lap time, which does not become more true, and wrong for a
car alongside, which stops being true without anything happening.

Routines are built out of these — see [routines.py](routines.py). A routine
running is a set of notifications with the routine's own conditions and
messages, which is why this file has no idea routines exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pitradio.engineer import spotter
from pitradio.plugins import base

log = logging.getLogger(__name__)

#: Ids are stored in config; never rename one in place.
SPOTTER = "spotter"
LAP_TIME = "lap_time"
FASTEST_LAP = "fastest_lap"
FASTEST_SECTOR = "fastest_sector"
SECTOR_PERFORMANCE = "sector_performance"


@dataclass(frozen=True)
class Call:
    """Something to say, and what would count as saying it again.

    `key` is the identity of the call rather than of the event. Two arrivals of
    a car on the left are the same key and are subject to the repeat interval;
    a car on the left and a car on the right are different keys and neither
    waits for the other.
    """

    key: str
    utterance: list[str]
    urgent: bool = False


class Notification:
    """One thing the engineer watches. Subclasses override `check`."""

    #: Stored in config; never rename one in place.
    id: str = ""
    name: str = "unnamed"
    description: str = ""
    #: Seconds before the same call is worth making again. Zero means never —
    #: say it once per change and then stay quiet.
    default_repeat: float = 0.0
    #: Whether it is on for a fresh install.
    default_enabled: bool = True
    #: Shown in the tab next to the repeat box, so the number has a meaning.
    repeat_help: str = ""
    #: What the sim has to supply for this to work at all — the PROVIDES_*
    #: names from `plugins.base`. A behaviour whose data is missing is skipped,
    #: because a tick-box that is on and permanently silent looks exactly like
    #: a bug.
    requires: tuple[str, ...] = ()

    def supported(self, provided) -> bool:
        """Whether the current sim can supply what this needs.

        `provided` of None means "nobody said", which is treated as yes — that
        is a plugin written before capabilities existed, and switching its
        behaviours off would be a silent regression.
        """
        if provided is None:
            return True
        return all(need in provided for need in self.requires)

    def reset(self) -> None:
        """Forget everything. A new session, or the notification being switched
        off and on again."""

    def check(self, context) -> list[Call]:
        """What to say right now, if anything. Must be quick and must not raise."""
        return []


class Runner:
    """Holds the notifications and decides which of their calls actually go out.

    The repeat logic lives here rather than in each notification, so a new one
    gets it for free and cannot get it subtly wrong. `now` is passed in rather
    than read, so the timing is testable without sleeping.
    """

    def __init__(self, notifications: list[Notification] | None = None) -> None:
        self.notifications = list(notifications or [])
        #: (notification id, call key) -> when it was last said.
        self._said: dict[tuple[str, str], float] = {}
        #: Behaviours already reported as unsupported, so the log says it once
        #: per session rather than ten times a second.
        self._unsupported: set[str] = set()

    def reset(self) -> None:
        self._said.clear()
        self._unsupported.clear()
        for notification in self.notifications:
            try:
                notification.reset()
            except Exception:
                log.exception("resetting %s failed", notification.id)

    def due(self, notification: Notification, call: Call, now: float,
            repeat: float) -> bool:
        """Whether this call is worth making at this moment."""
        stamp = self._said.get((notification.id, call.key))
        if stamp is None:
            return True
        if repeat <= 0:
            return False
        return now - stamp >= repeat

    def run(self, context, now: float, settings, provided=None) -> list[Call]:
        """Every call that should go out this tick, in order.

        `settings` answers `enabled(id)` and `repeat(id)`, so the runner does
        not have to know what a config looks like — which is what lets a
        routine supply its own without inventing a config section.

        `provided` is what the current sim can actually supply. A behaviour
        needing something absent is skipped **and said so once**, because the
        alternative is a tick-box that is on, looks fine and is permanently
        silent — which is indistinguishable from a bug, and is how somebody
        ends up filing one.
        """
        due: list[Call] = []
        for notification in self.notifications:
            if not settings.enabled(notification.id):
                continue
            if not notification.supported(provided):
                if notification.id not in self._unsupported:
                    self._unsupported.add(notification.id)
                    log.info(
                        "%s is on but this sim does not publish %s; it will "
                        "stay quiet", notification.name,
                        ", ".join(sorted(set(notification.requires) - set(provided or ()))))
                continue
            try:
                calls = notification.check(context)
            except Exception:
                # One notification failing must not silence the others, and
                # certainly must not take the poll thread down.
                log.exception("notification %s failed", notification.id)
                continue

            repeat = settings.repeat(notification.id)
            for call in calls or []:
                if not call.utterance or not self.due(notification, call, now, repeat):
                    continue
                self._said[(notification.id, call.key)] = now
                due.append(call)
        return due

    def forget(self, notification_id: str) -> None:
        """Drop a notification's history, so its next call is immediate."""
        for key in [k for k in self._said if k[0] == notification_id]:
            del self._said[key]


# -- the behaviours that ship ---------------------------------------------


class SpotterNotification(Notification):
    """Cars alongside, and the sides that have gone clear again.

    Repeats by default, because a car alongside stops being there without
    anything happening — and a driver holding a line for somebody who left two
    corners ago is worse off than one who was never told.
    """

    id = SPOTTER
    name = "Spotter"
    description = ("Calls cars alongside, and calls each side clear once they "
                   "have gone.")
    default_repeat = 3.0
    default_enabled = False
    repeat_help = "how often it repeats while a car is still there"
    # Every car's world position. A sim that only says how far round the lap
    # each car is — iRacing — cannot answer "who is beside me" from that, and
    # guessing from lap distance alone would put cars on the wrong side of the
    # track on any circuit that doubles back.
    requires = (base.PROVIDES_POSITIONS,)

    def __init__(self) -> None:
        self._previous: tuple[float, float, float] | None = None
        self._sides: frozenset[str] = frozenset()

    def reset(self) -> None:
        self._previous = None
        self._sides = frozenset()

    def check(self, context) -> list[Call]:
        own = context.own_car()
        if own is None:
            self.reset()
            return []

        previous, self._previous = self._previous, own.position
        facing = spotter.heading(previous, own.position)
        if facing is None:
            return []

        others = {name: position
                  for name, position in context.session.positions().items()
                  if name != own.driver}
        neighbours = spotter.alongside(
            own.position, facing, others,
            # Per-sim, from the plugin's settings: a prototype and a GT car are
            # different lengths, and sims disagree about where a car's origin
            # sits, so a number that suits one game is wrong in the next.
            metres=context.alongside_metres,
            width=context.width_metres,
            swap=context.swap_sides)

        now = spotter.occupied(neighbours)
        changes = spotter.calls(now, self._sides)
        self._sides = now

        calls = [
            Call(f"{side}:{text}", context.script.spotter_call(text), urgent)
            for side, text, urgent in changes
        ]
        # Sides that have not changed still get a standing call, which the
        # repeat interval decides the fate of. Without this the spotter says
        # "car left" once and then nothing for as long as they sit there.
        for side in sorted(now):
            if any(call.key.startswith(f"{side}:") for call in calls):
                continue
            text = spotter.warning(side, neighbours)
            calls.append(Call(f"{side}:{text}",
                              context.script.spotter_call(text), urgent=True))
        return calls


class LapTimeNotification(Notification):
    """The lap you have just completed."""

    id = LAP_TIME
    name = "Lap time"
    description = "Reads your lap out at the line, and says when it was your best."
    default_repeat = 0.0
    repeat_help = "a lap time does not become more true; leave this at 0"
    requires = (base.PROVIDES_LAPS,)

    def check(self, context) -> list[Call]:
        finished = context.finished_lap
        if finished is None:
            return []
        best = context.book.best_for(finished.driver)
        return [Call(
            f"lap:{finished.driver}:{finished.lap_time:.3f}",
            context.script.lap_time_call(
                finished.lap_time,
                personal_best=best is not None and best is finished),
        )]


class FastestLapNotification(Notification):
    """Somebody has taken the fastest lap of the session.

    Anybody's, not only yours. Who is quickest and by how much is the thing a
    driver most often asks the pit wall about, and it is the one number that
    changes what a stint is for.
    """

    id = FASTEST_LAP
    name = "New fastest lap"
    description = ("Says when anybody sets the fastest lap of the session, and "
                   "what it was.")
    default_repeat = 0.0
    repeat_help = "each new fastest lap is said once"
    requires = (base.PROVIDES_LAPS,)

    def __init__(self) -> None:
        self._holder = ""
        self._time = 0.0

    def reset(self) -> None:
        self._holder, self._time = "", 0.0

    def check(self, context) -> list[Call]:
        fastest = context.book.fastest()
        if fastest is None or fastest.lap_time <= 0:
            return []
        if self._time and fastest.lap_time >= self._time:
            return []

        # The very first lap of a session is not somebody "taking" the fastest
        # lap, it is the only lap. Recorded so the next one is a comparison,
        # but not announced as an event.
        first = not self._time
        self._holder, self._time = fastest.driver, fastest.lap_time
        if first:
            return []

        own = context.own_car()
        mine = own is not None and own.driver == fastest.driver
        return [Call(
            f"fastest:{fastest.driver}:{fastest.lap_time:.3f}",
            context.script.fastest_lap_call(
                fastest.driver, fastest.lap_time, mine=mine),
        )]


class FastestSectorNotification(Notification):
    """Somebody has taken a sector.

    Three times a lap rather than once, which is what makes it worth having:
    it tells you where the lap is being won while there is still a lap left to
    use it on.
    """

    id = FASTEST_SECTOR
    name = "New fastest sector"
    description = "Says when anybody takes the fastest time in a sector."
    default_repeat = 0.0
    default_enabled = False
    repeat_help = "each new fastest sector is said once"
    requires = (base.PROVIDES_SECTORS,)

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def reset(self) -> None:
        self._seen.clear()

    def check(self, context) -> list[Call]:
        calls: list[Call] = []
        for finished in context.finished_sectors:
            if not finished.session_best:
                continue
            # As with the fastest lap: the first time anybody sets a sector it
            # is the only time, not a record being taken.
            if finished.sector not in self._seen:
                self._seen.add(finished.sector)
                continue
            own = context.own_car()
            mine = own is not None and own.driver == finished.driver
            calls.append(Call(
                f"sector:{finished.sector}:{finished.driver}:{finished.seconds:.3f}",
                context.script.fastest_sector_call(
                    finished.driver, finished.sector, finished.seconds, mine=mine),
            ))
        return calls


class SectorPerformanceNotification(Notification):
    """How your sector went, against your own best.

    Against your own rather than the session's, because that is the comparison
    a driver can do something with on the next lap. Somebody else being three
    seconds quicker in sector one is information; being a tenth off your own is
    an instruction.
    """

    id = SECTOR_PERFORMANCE
    name = "Sector performance"
    description = ("At each sector, says how it compared with your best time in "
                   "that sector.")
    default_repeat = 0.0
    default_enabled = False
    repeat_help = "each sector is judged once, as you leave it"
    requires = (base.PROVIDES_SECTORS,)

    def check(self, context) -> list[Call]:
        own = context.own_car()
        if own is None:
            return []

        calls: list[Call] = []
        for finished in context.finished_sectors:
            if finished.driver != own.driver:
                continue
            if finished.personal_best:
                calls.append(Call(
                    f"pb:{finished.sector}:{finished.seconds:.3f}",
                    context.script.sector_best_call(
                        finished.sector, finished.seconds),
                ))
                continue
            # Nothing to compare against yet, so nothing worth saying.
            if not finished.previous:
                continue
            if abs(finished.delta) < context.sector_threshold:
                continue
            calls.append(Call(
                f"sector:{finished.sector}:{finished.seconds:.3f}",
                context.script.sector_delta_call(finished.sector, finished.delta),
            ))
        return calls


#: Every behaviour that ships, in the order the tab shows them. Static, like
#: plugins and routines, because a compiled build cannot discover a class it
#: never imports.
BUILTIN: tuple[type[Notification], ...] = (
    LapTimeNotification,
    FastestLapNotification,
    FastestSectorNotification,
    SectorPerformanceNotification,
    SpotterNotification,
)


def build() -> list[Notification]:
    """One instance of each, skipping any that will not construct."""
    made: list[Notification] = []
    for cls in BUILTIN:
        try:
            made.append(cls())
        except Exception:
            log.exception("could not create notification %s", getattr(cls, "id", cls))
    return made


def describe() -> list[tuple[str, str, str, bool, float, str]]:
    """(id, name, description, default enabled, default repeat, repeat help)."""
    return [(cls.id, cls.name, cls.description, cls.default_enabled,
             cls.default_repeat, cls.repeat_help) for cls in BUILTIN]


@dataclass
class Settings:
    """What the runner asks about each notification.

    An object rather than the config itself, so a routine can supply its own
    without there being a config section for it — which is what makes a routine
    "a set of notifications" rather than a second mechanism.
    """

    on: dict[str, bool] = field(default_factory=dict)
    intervals: dict[str, float] = field(default_factory=dict)
    #: Used for any id not named above. A routine turns everything it owns on.
    default_on: bool = False
    default_interval: float = 0.0

    def enabled(self, notification_id: str) -> bool:
        return bool(self.on.get(notification_id, self.default_on))

    def repeat(self, notification_id: str) -> float:
        return float(self.intervals.get(notification_id, self.default_interval))

    @classmethod
    def from_config(cls, engineer_cfg) -> Settings:
        """The Behaviours section of the Engineer tab, as the runner wants it.

        Defaults come from the notification classes, so a behaviour added in a
        later version appears without every existing config being rewritten —
        the same rule plugin settings follow.
        """
        on: dict[str, bool] = {}
        intervals: dict[str, float] = {}
        stored = getattr(engineer_cfg, "notifications", None) or {}
        for identifier, _name, _help, default_on, default_repeat, _hint in describe():
            settings = stored.get(identifier)
            on[identifier] = (default_on if settings is None
                              else bool(settings.enabled))
            intervals[identifier] = (default_repeat if settings is None
                                     else float(settings.repeat_seconds))
        return cls(on, intervals)
