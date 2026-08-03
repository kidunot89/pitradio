"""Fetch SDL3.dll for bundling.

SDL3 is the preferred joystick backend, but unlike SDL2 there is no Python
package that ships a Windows binary — PySDL3 is a pure wrapper with no library
in it. So the DLL comes from Valve-independent upstream: the official
libsdl-org release, verified against a pinned hash.

Downloaded rather than committed because a 2.8MB binary in the repository is
something nobody reviews, and a pinned hash gives the same guarantee without
it. Run before `packaging/build.py`; CI does this automatically.

SDL3 is zlib-licensed, so redistribution is fine — its LICENSE.txt is extracted
alongside the DLL and shipped with it.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "packaging" / "runtime"

VERSION = "3.4.14"
URL = (
    "https://github.com/libsdl-org/SDL/releases/download/"
    f"release-{VERSION}/SDL3-{VERSION}-win32-x64.zip"
)
# Pinned. Without this the build would run whatever that URL happens to serve.
SHA256 = "69a4e55645651af85e6ccfe40981b5a0bc2c594d0004fe7844db680e23cfbdaf"

WANTED = {"SDL3.dll": "SDL3.dll", "LICENSE.txt": "SDL3-LICENSE.txt"}


def target() -> Path:
    return RUNTIME / "SDL3.dll"


def fetch(force: bool = False) -> Path:
    """Download, verify and extract. Returns the path to SDL3.dll."""
    if target().exists() and not force:
        print(f"SDL3 {VERSION} already present at {target()}")
        return target()

    print(f"downloading SDL3 {VERSION}")
    with urllib.request.urlopen(URL, timeout=60) as response:
        blob = response.read()

    actual = hashlib.sha256(blob).hexdigest()
    if actual != SHA256:
        raise SystemExit(
            f"checksum mismatch for {URL}\n  expected {SHA256}\n  got      {actual}"
        )

    RUNTIME.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name, destination in WANTED.items():
            (RUNTIME / destination).write_bytes(archive.read(name))

    print(f"verified and extracted SDL3 {VERSION} to {RUNTIME}")
    return target()


def main() -> int:
    fetch(force="--force" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
