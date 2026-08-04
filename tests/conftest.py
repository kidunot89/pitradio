"""Shared fixtures.

`winapi` is stubbed only where a module imports it at load time and would
otherwise be unimportable off Windows. Nothing belonging to the app is faked.
"""

from __future__ import annotations

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
