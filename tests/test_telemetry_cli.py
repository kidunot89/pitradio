"""`--telemetry`: does the sim's block actually change?

The interesting failure this exists to catch is not a plugin that reads
nothing — that is obvious the moment you look. It is a plugin reading a block
that is **published but frozen**: the sim is connected, every field is
plausible, cars have positions and speeds, and none of it ever moves. The
engineer is then silent for a reason no amount of staring at one snapshot
reveals.

Found on a real machine the first time this was run: five identical reads two
seconds apart, including the sim's own clock, with a car sitting at 77 m/s.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from pitradio import __main__ as cli
from pitradio.plugins.base import Car, SessionInfo, Standings


def car(driver="Me", *, distance=100.0, speed=50.0, laps=1, sector=1,
        position=(1.0, 2.0, 3.0)):
    return Car(slot=1, driver=driver, place=1, is_player=True, control=0,
               lap_dist=distance, speed=speed, laps=laps, sector=sector,
               position=position)


def session(*cars, elapsed=10.0):
    return SessionInfo(track="Spa", track_length=7000.0, elapsed=elapsed,
                       cars=tuple(cars))


# -- what counts as movement ----------------------------------------------


def test_two_identical_reads_have_the_same_signature():
    one = session(car())
    assert cli._signature(one) == cli._signature(session(car()))


def test_the_sim_clock_alone_counts_as_movement():
    """A paused game keeps publishing positions that look entirely reasonable.
    The clock is the field that gives it away."""
    stopped = session(car(), elapsed=10.0)
    running = session(car(), elapsed=10.1)
    assert cli._signature(stopped) != cli._signature(running)


def test_a_car_moving_counts_as_movement():
    assert cli._signature(session(car(distance=100.0))) != \
        cli._signature(session(car(distance=140.0)))


def test_a_sector_change_counts_as_movement():
    assert cli._signature(session(car(sector=1))) != \
        cli._signature(session(car(sector=2)))


def test_a_car_being_repositioned_counts_as_movement():
    """Positions are what the spotter is built on, so a block where they never
    change is one where the spotter can never say anything."""
    assert cli._signature(session(car(position=(1.0, 2.0, 3.0)))) != \
        cli._signature(session(car(position=(9.0, 2.0, 3.0))))


# -- the command ----------------------------------------------------------


@dataclass
class Registry:
    """A stub sim that yields a fixed run of frames."""

    frames: list = field(default_factory=list)
    stopped: bool = False

    class _Plugin:
        name = "Stub"
        provides = frozenset({"laps", "positions", "sectors"})

        def status(self):
            return "connected"

    plugins: list = field(default_factory=lambda: [Registry._Plugin()])

    def start_all(self):
        pass

    def stop_all(self):
        self.stopped = True

    def standings_for(self, plugin_id):
        return Standings()

    def any_telemetry(self):
        if not self.frames:
            return "", SessionInfo()
        return "stub", self.frames.pop(0)


def run(monkeypatch, frames, seconds=0.0):
    registry = Registry(frames=list(frames))
    monkeypatch.setattr(cli, "out", lambda line="": printed.append(line))
    import pitradio.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "PluginRegistry", lambda *a, **k: registry)
    return cli.cmd_telemetry(seconds, 0.0), registry


printed: list[str] = []


def setup_function(_function):
    printed.clear()


def test_a_frozen_block_is_reported_as_such(monkeypatch):
    """The real failure, and it must not be left to the reader to spot."""
    frozen = [session(car()) for _ in range(4)]
    code, _registry = run(monkeypatch, frozen, seconds=0.02)
    text = "\n".join(printed)

    assert code == 1
    assert "Nothing changed" in text
    assert "paused" in text


def test_a_live_block_passes(monkeypatch):
    moving = [session(car(distance=d), elapsed=d / 50.0)
              for d in (100.0, 150.0, 200.0)]
    code, _registry = run(monkeypatch, moving, seconds=0.02)
    text = "\n".join(printed)

    assert code == 0
    assert "moving" in text


def test_no_sim_at_all_is_reported_and_fails(monkeypatch):
    code, _registry = run(monkeypatch, [], seconds=0.0)
    text = "\n".join(printed)

    assert code == 1
    assert "no sim publishing" in text


def test_only_changed_frames_are_printed(monkeypatch):
    """Five identical snapshots is not a diagnostic, it is homework."""
    frames = [session(car(distance=100.0)), session(car(distance=100.0)),
              session(car(distance=200.0))]
    # Long enough for three reads: the loop sleeps a minimum of 0.1s between
    # them however small the interval asked for.
    run(monkeypatch, frames, seconds=0.25)

    rows = [line for line in printed if line.startswith(" *Me")]
    assert len(rows) == 2


def test_the_plugin_is_shut_down_afterwards(monkeypatch):
    _code, registry = run(monkeypatch, [session(car())], seconds=0.0)
    assert registry.stopped is True


def test_a_frame_with_no_cars_is_not_a_session(monkeypatch):
    """An empty block is "the game is in a menu", not "a session with nobody
    in it"."""
    code, _registry = run(monkeypatch, [replace(session(), cars=())],
                          seconds=0.0)
    assert code == 1
    assert any("no sim publishing" in line for line in printed)
