"""Write SHA256SUMS for the release artifacts.

Not a shell one-liner, for two reasons. The PowerShell version globbed the
output directory while streaming into the very file it was creating, so it
tried to hash SHA256SUMS as it was being written. And the format has to match
what `updater._expected_hash` parses — putting it in Python means that
agreement is covered by a test instead of discovered during a release.

Format is the usual `sha256sum` one: lowercase hex, two spaces, bare filename.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

CHECKSUM_FILE = "SHA256SUMS"
CHUNK = 1 << 20


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(directory: Path) -> Path:
    """Hash every file in `directory` except the checksum file itself."""
    targets = sorted(
        item for item in directory.iterdir()
        if item.is_file() and item.name != CHECKSUM_FILE
    )
    if not targets:
        raise SystemExit(f"no files to checksum in {directory}")

    lines = [f"{sha256(item)}  {item.name}" for item in targets]
    target = directory / CHECKSUM_FILE
    target.write_text("\n".join(lines) + "\n", encoding="ascii")
    return target


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "packaging/Output")
    if not directory.is_dir():
        raise SystemExit(f"{directory} is not a directory")

    target = write_checksums(directory)
    print(target.read_text(encoding="ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
