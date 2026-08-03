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


# -- vendored modules ----------------------------------------------------


def test_nuitka_env_makes_vendored_modules_importable():
    """Nuitka runs as a subprocess and does not inherit this script's sys.path.

    v0.1.9 failed to build with "failed to locate package 'pylmusharedmemory'"
    because the vendor directory was added to build.py's own sys.path, which
    the subprocess never sees. PYTHONPATH is what actually carries it across.
    """
    import subprocess
    import sys

    env = build.nuitka_env()
    assert "PYTHONPATH" in env

    result = subprocess.run(
        [sys.executable, "-c", "import pylmusharedmemory; print('ok')"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"a subprocess with build.nuitka_env() cannot import the vendored "
        f"package, so Nuitka will not find it either:\n{result.stderr}"
    )


def test_nuitka_env_preserves_an_existing_pythonpath():
    """Clobbering PYTHONPATH would break any environment that relies on it."""
    import os

    original = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = "/somewhere/else"
    try:
        env = build.nuitka_env()
        assert "/somewhere/else" in env["PYTHONPATH"]
        assert "vendor" in env["PYTHONPATH"]
    finally:
        if original is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = original


def test_every_included_package_is_importable(args):
    """--include-package on something Nuitka cannot find is a fatal error.

    Checked against the environment the build actually uses, which is the part
    that was wrong.
    """
    import importlib.util
    import subprocess
    import sys

    names = [a.split("=", 1)[1] for a in args if a.startswith("--include-package=")]
    assert names

    unavailable = []
    for name in names:
        if importlib.util.find_spec(name) is not None:
            continue
        # Might still be reachable through the build's PYTHONPATH.
        result = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            env=build.nuitka_env(), capture_output=True, check=False,
        )
        if result.returncode != 0:
            unavailable.append(name)

    assert not unavailable, (
        f"the build asks Nuitka to include {unavailable}, which cannot be "
        f"imported even with its PYTHONPATH. That is a fatal build error."
    )


def test_every_data_file_source_exists(args):
    """--include-data-files pointing at a missing file fails the build.

    Cheap to check here; otherwise it costs a full Windows build to find out.
    """
    from pathlib import Path

    missing = []
    for arg in args:
        if not arg.startswith("--include-data-files="):
            continue
        source = arg.split("=", 1)[1].rsplit("=", 1)[0]
        if not Path(source).exists():
            missing.append(source)

    assert not missing, f"the build references files that do not exist: {missing}"


def test_sdl2_is_shipped_when_available():
    """SDL2 must land beside the exe, not inside the package.

    Relying on --include-package-data put it where sdlinput could not find it
    at runtime, and the built binary reported "SDL2 library not found" — which
    only showed up after a full build.
    """
    import sys

    library = build.sdl2_library()
    if sys.platform != "win32":
        # macOS ships a framework rather than SDL2.dll, so there is nothing to
        # assert here beyond the lookup not raising.
        return

    assert library is not None, "sdl2dll is installed but SDL2.dll was not found"
    assert any(
        arg.startswith("--include-data-files=") and arg.endswith("=SDL2.dll")
        for arg in build.nuitka_args("0.0.0")
    ), "SDL2.dll must be shipped to the dist root"
