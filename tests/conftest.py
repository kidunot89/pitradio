"""Shared fixtures.

`winapi` is stubbed only where a module imports it at load time and would
otherwise be unimportable off Windows. Nothing belonging to the app is faked.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture
def foreground(monkeypatch):
    """Control which executable the worker thinks is focused.

    The one Win32 call the trigger cycle makes that a test has to answer.
    Everything else in `winapi` imports off Windows now and raises if called,
    so nothing else needs standing in for.
    """
    from pitradio.input import winapi

    holder = {"exe": None}
    monkeypatch.setattr(winapi, "foreground_exe", lambda: holder["exe"])

    def set_exe(name):
        holder["exe"] = name

    return set_exe


@pytest.fixture
def engineer_context():
    """A Context around a list of cars, for driving a notification directly.

    Enough of one to check what a behaviour says, without a sim, a speaker or
    a service thread — which is the level the flag logic wants testing at.
    """
    from pitradio import i18n
    from pitradio.engineer import coaching, lines, routines, sectors
    from pitradio.plugins.base import SessionInfo

    clock = {"now": 0.0}

    def build(cars, *, me: str = "Me", caution: bool = False,
              elapsed: float | None = None):
        if elapsed is None:
            clock["now"] += 1.0
            elapsed = clock["now"]
        placed = tuple(
            replace(car, is_player=car.driver == me,
                    control=0 if car.driver == me else 2)
            for car in cars)
        return routines.Context(
            script=lines.Script(i18n.Catalogue("en")),
            book=coaching.LapBook(), sectors=sectors.SectorBook(),
            session=SessionInfo(cars=placed, track_length=5000.0,
                                elapsed=elapsed, full_course_yellow=caution))

    return build
