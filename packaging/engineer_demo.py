"""Drive the engineer from a fake sim, so it can be heard without racing.

    python packaging/engineer_demo.py             # speak out loud
    python packaging/engineer_demo.py --silent    # print the calls instead
    python packaging/engineer_demo.py --laps 6

The engineer is the hardest part of this app to try: every behaviour needs a
car on a track, a rival with a lap on the board, and somebody alongside at the
right moment. Getting all of that to happen on purpose in a real session takes
a stint, and half of it cannot be arranged at all.

So this drives the **real** `EngineerService** — real notifications, real
routines, real speech — against a made-up session where those things happen on
schedule. What comes out of the speakers is what would come out of them in a
race.

It is a development tool and is deliberately not part of the app: nothing in
`src/` imports it, and it ships with the repository rather than the installer.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "src", ROOT / "vendor"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from pitradio import config as config_mod  # noqa: E402
from pitradio.engineer import notifications, service  # noqa: E402
from pitradio.plugins.base import Car, SessionInfo, Standings  # noqa: E402

TRACK = 4000.0
#: Where the corners are. Two slow ones and a chicane, so corner detection has
#: something to find and something to merge.
CORNERS = (700.0, 1500.0, 1560.0, 2600.0, 3300.0)
TOP_SPEED = 65.0
APEX_SPEED = 22.0
RAMP = 160.0
STEP = 20.0

#: Where the sector boundaries are. The engineer learns these by watching cars
#: cross them, exactly as it would in a real session.
SECTORS = ((1, 0.0), (2, TRACK / 3), (3, TRACK * 2 / 3))


def speed_at(distance: float, skill: float) -> float:
    """A speed profile with corners in it. `skill` scales the whole lap."""
    nearest = min(abs(distance - corner) for corner in CORNERS)
    dip = max(0.0, 1.0 - nearest / RAMP)
    return (TOP_SPEED - (TOP_SPEED - APEX_SPEED) * dip) * skill


def sector_of(distance: float) -> int:
    """The sim's own numbering, in which 0 is sector three."""
    if distance < SECTORS[1][1]:
        return 1
    if distance < SECTORS[2][1]:
        return 2
    return 0


class Driver:
    """One car going round, with a consistent pace."""

    def __init__(self, name: str, skill: float, *, player: bool = False,
                 vehicle_class: str = "LMGT3", place: int = 1) -> None:
        self.name = name
        self.skill = skill
        self.player = player
        self.vehicle_class = vehicle_class
        self.place = place
        self.distance = 0.0
        self.elapsed = 0.0
        self.laps = 0
        self.last_lap = 0.0
        self.best_lap = 0.0
        self.lap_started = 0.0
        self.splits: dict[int, float] = {}
        self.last_splits: dict[int, float] = {}
        self.offset = 0.0

    def advance(self, seconds: float) -> None:
        speed = speed_at(self.distance, self.skill)
        before = sector_of(self.distance)
        self.distance += speed * seconds
        self.elapsed += seconds

        if self.distance >= TRACK:
            self.distance -= TRACK
            self.laps += 1
            self.last_lap = self.elapsed - self.lap_started
            self.lap_started = self.elapsed
            self.last_splits = dict(self.splits)
            self.splits = {}
            if not self.best_lap or self.last_lap < self.best_lap:
                self.best_lap = self.last_lap

        after = sector_of(self.distance)
        if after != before:
            # Cumulative, exactly as the sim publishes them.
            self.splits[before] = self.elapsed - self.lap_started

    @property
    def speed(self) -> float:
        return speed_at(self.distance, self.skill)

    def car(self) -> Car:
        return Car(
            slot=abs(hash(self.name)) % 1000,
            driver=self.name,
            place=self.place,
            vehicle_class=self.vehicle_class,
            control=0 if self.player else 2,
            is_player=self.player,
            # Strung out along a straight line beside the track, so the spotter
            # has something to find when they are put alongside.
            position=(self.offset, 0.0, self.distance),
            lap_dist=self.distance,
            speed=self.speed,
            laps=self.laps,
            last_lap=self.last_lap,
            best_lap=self.best_lap,
            sector=sector_of(self.distance),
            cur_sector1=self.splits.get(1, 0.0),
            cur_sector2=self.splits.get(2, 0.0),
            last_sector1=self.last_splits.get(1, 0.0),
            last_sector2=self.last_splits.get(2, 0.0),
        )


class Store:
    """Just enough of a ConfigStore for the engineer."""

    def __init__(self, cfg) -> None:
        self.config = cfg


class Plugins:
    """One made-up sim, publishing whatever the loop sets on it."""

    def __init__(self) -> None:
        self.session = SessionInfo()
        self.standings = Standings()

    def any_telemetry(self):
        return ("demo", self.session) if self.session.has_data else ("", SessionInfo())

    def standings_for(self, plugin_id):
        return self.standings

    def settings_for(self, plugin_id, stored=None):
        return dict(stored or {})


class Printer:
    """Prints what would be said, for --silent."""

    def start(self):
        pass

    def stop(self, timeout=None):
        pass

    def configure(self, settings):
        pass

    def clear(self):
        pass

    def say(self, utterance, *, urgent=False):
        mark = "!" if urgent else " "
        print(f"  {mark} {' '.join(utterance)}")


def build_config(args) -> config_mod.Config:
    cfg = config_mod.Config()
    engineer = cfg.engineer
    engineer.enabled = True
    engineer.persona = args.persona
    engineer.volume = args.volume
    # Everything on, so one run demonstrates the lot.
    engineer.notifications = {
        identifier: config_mod.NotificationConfig(True, repeat)
        for identifier, _n, _d, _on, repeat, _h in notifications.describe()
    }
    engineer.notifications[notifications.SPOTTER].repeat_seconds = args.spotter_repeat
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--laps", type=int, default=4,
                        help="how many laps to simulate (default 4)")
    parser.add_argument("--silent", action="store_true",
                        help="print the calls instead of speaking them")
    parser.add_argument("--persona", default="chief",
                        help="chief, ada, marshall or vic")
    parser.add_argument("--volume", type=float, default=0.9)
    parser.add_argument("--spotter-repeat", type=float, default=3.0)
    parser.add_argument("--speed", type=float, default=20.0,
                        help="how many times faster than real time (default 20)")
    parser.add_argument("--command", action="append", default=[],
                        help="a phrase to say to the engineer, as if spoken. "
                             "Repeatable; fires one per lap from lap 2.")
    args = parser.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("pitradio.engineer.speaking", "pitradio.engineer.tts"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = build_config(args)
    plugins = Plugins()
    speaker = Printer() if args.silent else None
    engineer = service.EngineerService(Store(cfg), plugins, speaker=speaker)
    if not args.silent:
        # Echo what is spoken as well as speaking it, so a run is readable
        # afterwards without listening to it again.
        spoken = engineer.speaker.say

        def announce(utterance, *, urgent=False):
            print(f"  {'!' if urgent else ' '} {' '.join(utterance)}")
            spoken(utterance, urgent=urgent)

        engineer.speaker.say = announce

    engineer.start()

    me = Driver("G.Taylor", 0.96, player=True, place=2)
    rival = Driver("N.Tandy", 1.0, place=1)
    slower = Driver("A.Rookie", 0.92, place=3)
    field = [me, rival, slower]

    # The rival starts ahead and the slower car alongside, so the spotter has
    # something to say in the first minute rather than the tenth.
    rival.distance = 400.0
    slower.distance = 8.0
    slower.offset = 3.2

    commands = list(args.command) or [
        "begin hot lap trainer N.Tandy",
        "begin sector trainer N.Tandy sector 2",
    ]

    print(f"\nA made-up session at a {TRACK:.0f}m circuit, "
          f"{len(CORNERS)} corners, {args.laps} lap(s).")
    print(f"Engineer: {engineer.display_name()} ({args.persona})\n")

    tick = 0.1
    lap_seen = 0
    issued = 0
    started = time.monotonic()

    while me.laps < args.laps:
        for driver in field:
            driver.advance(tick)

        plugins.session = SessionInfo(
            track="Demo Park", track_length=TRACK, elapsed=me.elapsed,
            cars=tuple(driver.car() for driver in field),
            focus_slot=None,
        )
        plugins.standings = Standings(
            overall={1: rival.name, 2: me.name, 3: slower.name},
            by_class={"LMGT3": {1: rival.name, 2: me.name, 3: slower.name}},
        )
        engineer._tick()

        if me.laps > lap_seen:
            lap_seen = me.laps
            print(f"\n--- lap {me.laps} ---")
            # A command a lap, from the second, so the reference lap exists.
            if lap_seen >= 1 and issued < len(commands):
                said = commands[issued]
                issued += 1
                print(f"  > {said}")
                taken = engineer.handle(said)
                print(f"    ({'engineer' if taken else 'chat box'})")

        # Let the speech actually play rather than racing past it.
        time.sleep(tick / max(0.1, args.speed))

    print(f"\ndone in {time.monotonic() - started:.1f}s of wall clock")
    if not args.silent:
        print("waiting for the last calls to finish speaking...")
        time.sleep(3.0)
    engineer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
