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

ROOT = Path(__file__).parent.parent

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
    # Compared as a Path, not a string: the same assertion spelled with
    # forward slashes passes everywhere except the one platform that ships.
    assert Path(args[-1]).parts[-2:] == ("src", "pitradio")


# -- version ------------------------------------------------------------


def test_version_matches_the_app():
    """A tag/__version__ mismatch makes the updater offer an endless update."""
    app = (ROOT / "src" / "pitradio" / "__init__.py").read_text(encoding="utf-8")
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


# -- self-healing a corrupt compilation cache ----------------------------


# The exact line that killed v0.1.22's release build, and two CI runs before
# it. Kept verbatim: the recovery hinges on matching this shape, and a
# paraphrase would let the regex drift away from what Scons really prints.
CORRUPT_LINE = (
    r"scons: *** [module.pygments.lexers.q.obj] "
    r"C:\Users\RUNNER~1\AppData\Local\Nuitka\Nuitka\Cache\clcache\objects"
    r"\88f\88f3f5baaac25063c1e0597c4044e75d\object: No such file or directory"
)


def test_a_corrupt_cache_entry_is_recognised():
    output = f"Nuitka-Scons: Backend C compiler: cl (cl 14.5).\n{CORRUPT_LINE}\n" \
             "FATAL: Failed unexpectedly in Scons C backend compilation.\n"
    cache = build.corrupt_cache_dir(output)
    assert cache == r"C:\Users\RUNNER~1\AppData\Local\Nuitka\Nuitka\Cache\clcache"


def test_only_the_compiler_cache_is_targeted():
    """Nuitka's other caches hold downloaded tools; refetching them is minutes."""
    cache = build.corrupt_cache_dir(CORRUPT_LINE)
    assert "objects" not in cache
    assert cache.endswith("clcache")


def test_the_ccache_spelling_is_recognised_too():
    line = (
        "scons: *** [module.foo.obj] "
        "/home/runner/.cache/Nuitka/ccache/objects/1a/2b/object"
        ": No such file or directory"
    )
    assert build.corrupt_cache_dir(line) == "/home/runner/.cache/Nuitka/ccache"


@pytest.mark.parametrize("output", [
    "",
    "error: 'foo' undeclared (first use in this function)\n",
    "FATAL: Failed unexpectedly in Scons C backend compilation.\n",
    "LINK : fatal error LNK1181: cannot open input file 'foo.obj'\n",
    # A missing *source* file is a real problem, not a stale cache entry.
    "scons: *** [module.foo.obj] src/foo.c: No such file or directory\n",
])
def test_other_failures_are_not_retried(output):
    """A genuine compile error must surface on the first attempt.

    Retrying costs a second full build — about twenty minutes warm — so the
    match has to be narrow enough that only a corrupt cache triggers it.
    """
    assert build.corrupt_cache_dir(output) is None


def _stub_build(monkeypatch, tmp_path, outcomes):
    """build() with Nuitka replaced by a scripted sequence of outcomes."""
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path)
    monkeypatch.setattr(build, "nuitka_args", lambda version: ["nuitka", "stub"])
    (tmp_path / "pitradio.dist").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pitradio.dist" / "pitradio.exe").write_text("stub")

    calls = []
    results = iter(outcomes)

    def fake_run(args):
        calls.append(args)
        return next(results)

    removed = []
    monkeypatch.setattr(build, "_run_nuitka", fake_run)
    monkeypatch.setattr(build.shutil, "rmtree", lambda p, **kw: removed.append(p))
    return calls, removed


def test_a_corrupt_cache_is_removed_and_the_build_retried(monkeypatch, tmp_path):
    calls, removed = _stub_build(monkeypatch, tmp_path, [
        (1, f"{CORRUPT_LINE}\nFATAL: Failed unexpectedly in Scons C backend.\n"),
        (0, "built fine the second time\n"),
    ])

    assert build.build() == 0
    assert len(calls) == 2, "the build should have been retried"
    assert removed == [r"C:\Users\RUNNER~1\AppData\Local\Nuitka\Nuitka\Cache\clcache"]


def test_a_genuine_failure_is_not_retried(monkeypatch, tmp_path):
    """Twenty minutes is too long to spend re-proving a real compile error."""
    calls, removed = _stub_build(monkeypatch, tmp_path, [
        (1, "error: 'foo' undeclared (first use in this function)\n"),
    ])

    assert build.build() == 1
    assert len(calls) == 1
    assert removed == []


def test_a_second_failure_after_clearing_the_cache_gives_up(monkeypatch, tmp_path):
    """One retry, not a loop — a cache that stays broken is a different problem."""
    calls, _ = _stub_build(monkeypatch, tmp_path, [
        (1, CORRUPT_LINE),
        (1, CORRUPT_LINE),
    ])

    assert build.build() == 1
    assert len(calls) == 2


# -- the SDL3 binding must match the DLL we ship -------------------------


def _pe_exports(path):
    """Exported symbol names from a PE file, without loading it.

    Parsed rather than dlopen'd because the tests run on Linux and macOS and
    the DLL is Windows-only. The alternative is finding out on a user's machine
    that a name was wrong.
    """
    import struct

    data = Path(path).read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0", "not a PE file"

    sections = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    # The export directory is data directory 0; PE32+ puts it 16 bytes later.
    directories = optional + (112 if magic == 0x20B else 96)
    export_rva = struct.unpack_from("<I", data, directories)[0]

    table = []
    for index in range(sections):
        header = optional + optional_size + 40 * index
        vsize, vaddr, _rawsize, rawptr = struct.unpack_from("<IIII", data, header + 8)
        table.append((vaddr, max(vsize, 1), rawptr))

    def offset(rva):
        for vaddr, vsize, rawptr in table:
            if vaddr <= rva < vaddr + vsize:
                return rawptr + (rva - vaddr)
        raise AssertionError(f"RVA {rva:#x} is outside every section")

    directory = offset(export_rva)
    count = struct.unpack_from("<I", data, directory + 24)[0]
    names_rva = struct.unpack_from("<I", data, directory + 32)[0]
    names_base = offset(names_rva)

    found = set()
    for index in range(count):
        name_rva = struct.unpack_from("<I", data, names_base + 4 * index)[0]
        start = offset(name_rva)
        found.add(data[start:data.index(b"\0", start)].decode("ascii", "replace"))
    return found


SDL3_DLL = ROOT / "packaging" / "runtime" / "SDL3.dll"


@pytest.mark.skipif(not SDL3_DLL.exists(),
                    reason="run packaging/fetch_sdl3.py first")
def test_every_sdl3_symbol_we_declare_exists():
    """A misspelled symbol is the worst failure this binding can have.

    `_declare` raises AttributeError on the first missing name, `start()`
    catches it and reports the backend unavailable, and the app quietly drops
    to SDL2 — which is exactly the state SDL3 was added to escape. SDL3 renamed
    most of the SDL2 joystick calls (`SDL_JoystickUpdate` became
    `SDL_UpdateJoysticks`, and so on), so this is a live hazard rather than a
    theoretical one.
    """
    import re

    declared = set(re.findall(
        r"lib\.(SDL_[A-Za-z0-9_]+)", (ROOT / "src/pitradio/input/sdl3input.py").read_text()))
    assert declared, "no SDL3 symbols found in sdl3input.py"

    missing = sorted(declared - _pe_exports(SDL3_DLL))
    assert not missing, f"SDL3.dll does not export: {', '.join(missing)}"


@pytest.mark.skipif(not SDL3_DLL.exists(),
                    reason="run packaging/fetch_sdl3.py first")
def test_the_pinned_sdl3_download_is_what_we_extracted():
    """The hash covers the archive; this covers what came out of it."""
    import fetch_sdl3

    assert fetch_sdl3.target().exists()
    assert (fetch_sdl3.RUNTIME / "SDL3-LICENSE.txt").exists(), (
        "SDL3 is zlib-licensed and its licence must ship with the binary"
    )


def test_the_package_is_compiled_rather_than_its_main_module(args):
    """Nuitka names the dist directory after whatever it is handed.

    Pointing it at `__main__.py` built `build\\__main__.dist`, so the Inno
    script and nine CI steps all referenced a `build\\pitradio.dist` that no
    longer existed — and the only symptom was "expected ... to exist after the
    build", after a fifty-minute compile that had actually succeeded.
    """
    assert "--python-flag=-m" in args, (
        "compiling a package without -m does not give it __main__ semantics"
    )
    assert not args[-1].endswith(".py"), (
        "hand Nuitka the package directory, or the dist directory is renamed"
    )


def test_the_self_test_checks_modules_that_exist():
    """`--self-test` is the check that a build actually works.

    Its module list still held the pre-restructure names — "winapi", "gui",
    "plugins" — so on the built binary every one reported ModuleNotFoundError
    and the real modules were never imported at all. The check that exists to
    catch a broken bundle was itself broken, and it reported failures loudly
    enough that they read as a packaging problem rather than a stale list.
    """
    import ast
    import importlib.util

    tree = ast.parse((ROOT / "src" / "pitradio" / "__main__.py").read_text())
    listed = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "modules" not in targets or not isinstance(node.value, ast.List):
            continue
        listed = [element.value for element in node.value.elts
                  if isinstance(element, ast.Constant)]
    assert listed, "no module list found in cmd_self_test"

    # Third-party names may legitimately be absent from a dev environment;
    # the app's own modules may not.
    missing = [name for name in listed
               if name.startswith("pitradio")
               and importlib.util.find_spec(name) is None]
    assert not missing, f"--self-test imports modules that do not exist: {missing}"


def test_the_self_test_covers_every_module_in_the_package():
    """A module nobody imports is a module packaging can silently drop.

    That is how v0.1.0 shipped without av.utils. Anything added under
    src/pitradio/ has to be named here or the built binary is never asked
    whether it survived the bundle.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "pitradio" / "__main__.py").read_text())
    listed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "modules" in targets:
                listed = {e.value for e in node.value.elts
                          if isinstance(e, ast.Constant)}

    package = ROOT / "src" / "pitradio"
    actual = set()
    for path in package.rglob("*.py"):
        parts = path.relative_to(package.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts[-1] == "__main__":
            continue                      # it is the entry point
        actual.add(".".join(parts))

    missing = sorted(actual - listed)
    assert not missing, (
        f"--self-test does not import: {missing}. Add them, or packaging will "
        f"not notice when one goes missing from the build."
    )
