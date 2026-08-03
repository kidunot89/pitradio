"""Guards on the Nuitka build flags.

These exist because packaging mistakes are otherwise only discoverable by
running a ~30 minute Windows build, and one of them shipped: v0.1.0 could not
transcribe because `av.utils` was missing from the bundle.

The assertions inspect the argument list `build.nuitka_args` actually produces,
not the text of the file — grepping the source would match its own comments.
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "packaging"))

import build

av = pytest.importorskip("av", reason="PyAV not installed in this environment")


@pytest.fixture
def args() -> list[str]:
    return build.nuitka_args("0.1.1")


# -- av, the one that shipped broken -------------------------------------


def test_every_compiled_av_module_is_named():
    """Nuitka cannot see imports made from inside a compiled extension.

    PyAV ships each submodule as a prebuilt extension, so following imports
    reaches some and misses others. The build names them all explicitly; this
    checks that list still matches what the installed package contains, so a
    PyAV upgrade adding a module can't quietly reintroduce the bug.
    """
    root = Path(av.__file__).parent
    expected = {
        ".".join(["av", *p.relative_to(root).parent.parts, p.name.split(".")[0]])
        for pattern in ("*.pyd", "*.so")
        for p in root.rglob(pattern)
    }

    assert expected, "found no compiled av modules; the detection is broken"
    assert build.av_extension_modules() == sorted(expected)


def test_av_utils_is_included(args):
    """The specific module whose absence broke v0.1.0."""
    assert "--include-module=av.utils" in args


def test_av_module_names_resolve():
    """Nuitka ignores a misspelled module name silently, so it must be exact."""
    for name in build.av_extension_modules():
        assert importlib.util.find_spec(name) is not None, f"{name} does not resolve"


def test_av_is_not_included_as_a_whole_package(args):
    """--include-package=av crashes Nuitka's optimiser with an AssertionError."""
    assert "--include-package=av" not in args


# -- flags that fail silently if changed ---------------------------------


def test_sounddevice_uses_include_module(args):
    """--include-package on a single-module dependency is a fatal Nuitka error."""
    assert "--include-module=sounddevice" in args
    assert "--include-package=sounddevice" not in args


def test_standalone_never_onefile(args):
    """Extract-to-temp is a large part of why packed Python apps get quarantined."""
    assert "--standalone" in args
    assert not any("onefile" in arg for arg in args)


def test_console_mode_is_attach(args):
    """'disable' leaves --check-config with no stdout, and CI unable to read it."""
    assert "--windows-console-mode=attach" in args


def test_uac_admin_manifest_is_requested(args):
    """Without it, UIPI silently discards every keystroke sent to an elevated sim."""
    assert "--windows-uac-admin" in args


def test_tkinter_plugin_is_enabled(args):
    """The GUI needs Tcl/Tk runtime data, which only the plugin collects."""
    assert "--enable-plugin=tk-inter" in args


def test_default_config_is_bundled(args):
    """First run of an installed build seeds %APPDATA% from this file."""
    assert any("config.default.json" in arg for arg in args)


def test_entry_point_is_last(args):
    """Nuitka takes the script as a positional argument."""
    assert args[-1].endswith("pitradio.py")


# -- version ------------------------------------------------------------


def test_version_matches_the_app():
    """A tag/__version__ mismatch makes the updater offer an endless update."""
    app = (Path(build.__file__).parent.parent / "pitradio.py").read_text(encoding="utf-8")
    declared = re.search(r'^__version__\s*=\s*"([^"]+)"', app, re.MULTILINE).group(1)
    assert build.read_version() == declared


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.1.0", "0.1.0.0"), ("1.2.3.4", "1.2.3.4"), ("2.0", "2.0.0.0")],
)
def test_four_part_version(version, expected):
    assert build._four_part(version) == expected
