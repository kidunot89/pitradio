"""Nuitka build. Every flag that matters lives here rather than in CI.

    python packaging/build.py            # build
    python packaging/build.py --version  # print the version and exit

Two choices are deliberate and worth not "fixing" later:

* **--standalone, never --onefile.** A onefile build unpacks itself into a temp
  directory at every launch, which is the same shape as a self-extracting
  dropper and is a large part of why packed Python apps get quarantined.
* **Nuitka rather than PyInstaller.** Nuitka compiles to C and links a real
  binary instead of appending a bundle to a bootloader that malware families
  reuse. Builds are unsigned, so this does not make the app trusted — it just
  removes the most common heuristic trigger.

The native dependencies (ctranslate2, onnxruntime, PortAudio) are the fragile
part: they ship shared libraries that standalone mode does not always pick up
by itself, which is why they are named explicitly below.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BUILD_DIR = ROOT / "build"

# Vendored third-party modules live here rather than in site-packages. This is
# for av_extension_modules() and the tests; Nuitka gets it via nuitka_env().
for extra in (ROOT / "vendor", SRC):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def read_version() -> str:
    """Parse __version__ out of the entry point without importing it.

    Importing would pull in tkinter and the rest, which the build host may not
    have configured, and would make the build depend on the app running. It
    lives in the package __init__ rather than the CLI for the same reason.
    """
    text = (SRC / "pitradio" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find __version__ in src/pitradio/__init__.py")
    return match.group(1)


def _four_part(version: str) -> str:
    parts = [p for p in re.findall(r"\d+", version)][:4]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts)


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def av_extension_modules() -> list[str]:
    """Every compiled submodule of av, named explicitly.

    PyAV ships each submodule as a prebuilt extension with a `.py` typing stub
    beside it. Nuitka uses the extension, and imports made *from inside* one are
    invisible to static analysis — so following imports alone reached 47 of the
    48 modules and silently dropped `av.utils`. v0.1.0 shipped that way and died
    with "No module named 'av.utils'" the first time it loaded Whisper.

    `--include-package=av` would cover it, but crashes Nuitka's optimiser with
    an internal AssertionError. Naming the modules avoids both problems, and
    computing the list here rather than hardcoding it means a PyAV upgrade that
    adds a module doesn't quietly reintroduce the bug.
    """
    try:
        import av
    except ImportError:
        return []

    root = Path(av.__file__).parent
    names = set()
    for pattern in ("*.pyd", "*.so"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            # Strip the platform tag: utils.abi3.so / utils.cp312-win_amd64.pyd
            stem = relative.name.split(".")[0]
            names.add(".".join(["av", *relative.parent.parts, stem]))
    return sorted(names)


def nuitka_args(version: str) -> list[str]:
    """The full Nuitka command line.

    Split out from build() so the flags can be asserted on directly rather than
    by grepping this file — a packaging mistake here otherwise costs a ~30
    minute Windows build to discover.
    """
    args = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        # Compile the package, not its __main__ module. Nuitka names the dist
        # directory after whatever it was handed, so pointing at __main__.py
        # produced build\__main__.dist and every reference to
        # build\pitradio.dist — the installer script and nine CI steps —
        # silently pointed at nothing. Nuitka warns about this; the warning is
        # correct. -m makes it behave like `python -m pitradio`.
        "--python-flag=-m",
        "--assume-yes-for-downloads",
        "--enable-plugin=tk-inter",
        # "attach", not "disable": double-clicking still shows no console, but
        # running from a terminal keeps stdout, which is what makes
        # `pitradio.exe --check-config` usable and lets CI smoke-test the
        # built binary by reading its output rather than guessing.
        "--windows-console-mode=attach",
        # The app types into windows owned by processes that may be elevated;
        # without this manifest UIPI discards everything it sends, silently.
        "--windows-uac-admin",
        f"--windows-icon-from-ico={ROOT / 'packaging' / 'icon.ico'}",
        # Also as a data file, not only embedded in the exe. The window asks
        # Windows to use it directly at runtime -- iconbitmap() needs a path on
        # disk, and cannot read the exe's own resource section.
        f"--include-data-files={ROOT / 'packaging' / 'icon.ico'}=icon.ico",
        "--include-data-files="
        f"{ROOT / 'config.default.json'}=config.default.json",
        # Native payloads standalone mode misses on its own.
        # Nothing the app uses at runtime, all of it dragged in by test and
        # packaging tooling: pygments alone is 339 modules (via pytest,
        # setuptools and httpx) and pip is another 404. They were being
        # compiled — the Scons failure that broke three builds was on
        # `module.pygments.lexers.q.obj` — for code that can never run.
        "--nofollow-import-to=pygments",
        "--nofollow-import-to=pip",
        "--nofollow-import-to=setuptools",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=_pytest",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=pydoc",
        "--nofollow-import-to=doctest",
        "--include-package=faster_whisper",
        "--include-package-data=faster_whisper",
        "--include-package=ctranslate2",
        "--include-package-data=ctranslate2",
        # av's compiled submodules are added below; see av_extension_modules.
        # sounddevice is a single module, not a package — --include-package
        # is a fatal error for it. Its PortAudio DLL lives in the separate
        # _sounddevice_data package, added below.
        "--include-module=sounddevice",
        "--include-package=pystray",
        "--include-package=PIL",
        # Plugins are registered statically in plugins/__init__.py, so
        # Nuitka can follow them -- but naming the package guarantees a
        # new sim module is bundled even before anything imports it.
        "--include-package=pitradio.plugins",
        # Routines are registered statically like plugins, so Nuitka can
        # follow them -- but naming the package guarantees a new one is
        # bundled even before anything imports it.
        "--include-package=pitradio.engineer",
        # The translation catalogues are data, not code, so following imports
        # never finds them. A build without them falls back to English
        # everywhere and says nothing about why.
        "--include-package-data=pitradio",
        # The vendored LMU struct definitions are loaded via a sys.path
        # entry at runtime, which Nuitka cannot see.
        "--include-package=pylmusharedmemory",
        "--product-name=PitRadio",
        f"--product-version={_four_part(version)}",
        f"--file-version={_four_part(version)}",
        "--file-description=Push-to-talk dictation for sim racing",
        "--copyright=MIT licensed",
        f"--output-dir={BUILD_DIR}",
        "--output-filename=pitradio.exe",
        str(SRC / "pitradio"),
    ]

    for name in av_extension_modules():
        args.insert(-1, f"--include-module={name}")


    # PortAudio's DLL lives in a separate data package on Windows wheels.
    if _have("_sounddevice_data"):
        args.insert(-1, "--include-package-data=_sounddevice_data")

    # faster-whisper uses onnxruntime for voice-activity detection. Which
    # versions need it varies, so it is included only when actually installed.
    if _have("onnxruntime"):
        args.insert(-1, "--include-package=onnxruntime")
        args.insert(-1, "--include-package-data=onnxruntime")

    # Voice imports websocket-client *inside a function*, so that a build
    # without it still runs with voice simply unavailable. Nuitka cannot follow
    # an import it never sees at module scope, so naming it here is what puts
    # it in the binary — the same trap that dropped onnxruntime and av.utils.
    if _have("websocket"):
        args.insert(-1, "--include-package=websocket")

    return args


def nuitka_env() -> dict:
    """Environment for the Nuitka subprocess.

    Nuitka runs as a separate process, so adding `src/` and `vendor/` to this
    script's sys.path does nothing for it — it resolves --include-package
    against its own import path. PYTHONPATH is how the app package and the
    vendored modules become findable.
    """
    env = dict(os.environ)
    extra = os.pathsep.join((str(SRC), str(ROOT / "vendor")))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{extra}{os.pathsep}{existing}" if existing else extra
    return env


# Scons reporting a cache entry whose object file is missing. The manifest
# survived and the object did not, so Scons believes it has nothing to do and
# fails outright rather than recompiling it.
#
# GitHub Actions caches saved from an interrupted run restore in exactly this
# state — the tarball was snapshotted mid-write. One such entry killed three
# separate runs before it was tracked down, because the failure looks like a
# compiler problem and moves to whichever build restores the cache next.
_CORRUPT_CACHE = re.compile(
    r"^scons: \*\*\* \[[^\]]*\] (?P<root>.+?[\\/](?:cl|c)cache)[\\/]objects[\\/]"
    r"[^\n]*: No such file or directory",
    re.MULTILINE | re.IGNORECASE,
)


def corrupt_cache_dir(output: str) -> str | None:
    """The compilation cache to delete, if that is why the build died.

    Returns None for every other failure. A genuine compile error must surface
    on the first attempt — retrying one costs a second full build, and at ~20
    minutes warm that is not a cost to pay on a guess.

    A plain string, not a Path: the path is whatever Scons printed, and on
    Windows that is a Windows path. Wrapping it in Path on a Linux test runner
    yields a single opaque component rather than a path, so the parsing could
    only be verified on the platform it already works on.
    """
    match = _CORRUPT_CACHE.search(output)
    return match.group("root") if match else None


def _run_nuitka(args: list[str]) -> tuple[int, str]:
    """Run Nuitka, echoing output live while keeping a copy to inspect.

    Streamed rather than captured wholesale so a CI log still shows progress
    during the twenty minutes this takes.
    """
    process = subprocess.Popen(
        args,
        cwd=ROOT,
        env=nuitka_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    captured: list[str] = []
    with process:
        for line in process.stdout:
            print(line, end="", flush=True)
            captured.append(line)
    return process.returncode, "".join(captured)


def build() -> int:
    version = read_version()
    BUILD_DIR.mkdir(exist_ok=True)

    args = nuitka_args(version)
    print(f"building PitRadio {version}")
    print(" ".join(args))

    returncode, output = _run_nuitka(args)
    if returncode != 0:
        cache = corrupt_cache_dir(output)
        if cache is None:
            return returncode

        # Only the compiler cache is removed. Nuitka's other caches under the
        # same root hold downloaded tools, and refetching those adds minutes
        # for no reason — the corruption is never in them.
        print(
            f"\nthe compilation cache at {cache} is corrupt: it has an entry "
            f"whose object file is missing.\nremoving it and building again.",
            file=sys.stderr,
        )
        shutil.rmtree(cache, ignore_errors=True)

        returncode, _ = _run_nuitka(args)
        if returncode != 0:
            return returncode

    dist = BUILD_DIR / "pitradio.dist"
    exe = dist / "pitradio.exe"
    if not exe.exists():
        # Name what *did* get built. Nuitka derives this directory from the
        # entry point, so when it moves the failure is "expected X to exist"
        # with no hint that Y is sitting right beside it.
        built = sorted(path.name for path in BUILD_DIR.glob("*.dist"))
        print(f"error: expected {exe} to exist after the build", file=sys.stderr)
        if built:
            print(f"       the build produced: {', '.join(built)}", file=sys.stderr)
            print("       Nuitka names this after the entry point — check "
                  "nuitka_args()", file=sys.stderr)
        return 1

    print(f"built {exe}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows binary with Nuitka.")
    parser.add_argument("--version", action="store_true",
                        help="print the version from the package and exit")
    args = parser.parse_args()

    if args.version:
        print(read_version())
        return 0
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
