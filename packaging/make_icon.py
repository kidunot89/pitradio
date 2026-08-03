"""Generate packaging/icon.ico from the same drawing the window and tray use.

Run after changing the tray artwork:

    python packaging/make_icon.py

The .ico is committed because the Nuitka build needs it as a file, and CI
shouldn't have to render artwork before it can compile.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pitradio.ui import logo  # noqa: E402  - needs the path set up first


def main() -> int:
    target = ROOT / "packaging" / "icon.ico"
    logo.draw(logo.SIZE).save(target, format="ICO", sizes=logo.ico_sizes())
    print(f"wrote {target} ({len(logo.ico_sizes())} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
