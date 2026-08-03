"""Every subprocess call must survive having no console.

Launched from a shortcut, the app is a GUI-subsystem process with no console
and invalid standard handles. `capture_output=True` redirects stdout and stderr
but leaves stdin alone, and Windows then fails process creation outright with
[WinError 6] The handle is invalid.

That killed the app during GUI construction in every release through 0.1.2. It
was invisible because there was no console for the traceback to reach, and it
never reproduced from a terminal, where the handles are valid.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
# Every module that can run inside the GUI process. updater.py belongs here:
# launch_installer fires from the window, so a missing redirect would break
# self-update for exactly the people who installed normally.
GUI_MODULES = [
    "src/pitradio/ui/gui.py",
    "src/pitradio/ui/gui_settings.py",
    "src/pitradio/ui/gui_language.py",
    "src/pitradio/ui/tray.py",
    "src/pitradio/__main__.py",
    "src/pitradio/worker.py",
    "src/pitradio/updater.py",
]


def _subprocess_calls(source: str):
    """Yield (function_name, call_node) for every subprocess.run/Popen call."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in ("run", "Popen", "call", "check_output")
        ):
            yield func.attr, node


def _kwarg_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


@pytest.mark.parametrize("module", GUI_MODULES)
def test_subprocess_calls_redirect_stdin(module):
    """stdin must be redirected explicitly; capture_output does not cover it."""
    path = ROOT / module
    source = path.read_text(encoding="utf-8")

    for name, call in _subprocess_calls(source):
        kwargs = _kwarg_names(call)
        # A call that unpacks a shared kwargs dict is checked separately below.
        if any(kw.arg is None for kw in call.keywords):
            continue
        assert "stdin" in kwargs, (
            f"{module}: subprocess.{name} does not redirect stdin. With no "
            f"console the parent's stdin handle is invalid and process creation "
            f"fails with [WinError 6]."
        )


def test_shared_subprocess_kwargs_redirect_stdin():
    """The dict the GUI's schtasks calls unpack must itself redirect stdin."""
    from pitradio.ui import gui_settings

    assert gui_settings._SUBPROCESS_KWARGS["stdin"] is not None
    assert "stdin" in gui_settings._SUBPROCESS_KWARGS
    assert gui_settings._SUBPROCESS_KWARGS["capture_output"] is True


def test_task_query_survives_a_broken_environment(monkeypatch):
    """A failing schtasks decides a checkbox's state. It must not be fatal."""
    import subprocess

    from pitradio.ui import gui_settings

    def boom(*args, **kwargs):
        raise OSError(6, "The handle is invalid")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(gui_settings.sys, "platform", "win32")

    assert gui_settings._task_exists() is False


def test_crash_logging_is_installed():
    """An unhandled exception in a GUI build has nowhere to go but the log."""
    import sys
    import threading

    from pitradio import __main__ as pitradio

    original_hook, original_thread_hook = sys.excepthook, threading.excepthook
    try:
        pitradio._install_crash_logging()
        assert sys.excepthook is not original_hook
        assert threading.excepthook is not original_thread_hook
    finally:
        sys.excepthook = original_hook
        threading.excepthook = original_thread_hook


def test_crash_logging_records_the_traceback(caplog):
    import sys

    from pitradio import __main__ as pitradio

    original = sys.excepthook
    try:
        pitradio._install_crash_logging()
        with caplog.at_level("CRITICAL"):
            try:
                raise ValueError("boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        assert "unhandled exception" in caplog.text
        assert "boom" in caplog.text
    finally:
        sys.excepthook = original
