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
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"


def read_version() -> str:
    """Parse __version__ out of the entry point without importing it.

    Importing would pull in tkinter and the rest, which the build host may not
    have configured, and would make the build depend on the app running.
    """
    text = (ROOT / "pitradio.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find __version__ in pitradio.py")
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
        "--include-data-files="
        f"{ROOT / 'config.default.json'}=config.default.json",
        # Native payloads standalone mode misses on its own.
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
        "--product-name=PitRadio",
        f"--product-version={_four_part(version)}",
        f"--file-version={_four_part(version)}",
        "--file-description=Push-to-talk dictation for sim racing",
        "--copyright=MIT licensed",
        f"--output-dir={BUILD_DIR}",
        "--output-filename=pitradio.exe",
        str(ROOT / "pitradio.py"),
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

    return args


def build() -> int:
    version = read_version()
    BUILD_DIR.mkdir(exist_ok=True)

    args = nuitka_args(version)
    print(f"building PitRadio {version}")
    print(" ".join(args))
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode

    dist = BUILD_DIR / "pitradio.dist"
    exe = dist / "pitradio.exe"
    if not exe.exists():
        print(f"error: expected {exe} to exist after the build", file=sys.stderr)
        return 1

    print(f"built {exe}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows binary with Nuitka.")
    parser.add_argument("--version", action="store_true",
                        help="print the version from pitradio.py and exit")
    args = parser.parse_args()

    if args.version:
        print(read_version())
        return 0
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
