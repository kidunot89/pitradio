"""Generate packaging/icon.ico from the same drawing the tray icon uses.

Run after changing the tray artwork:

    python packaging/make_icon.py

The .ico is committed because the Nuitka build needs it as a file, and CI
shouldn't have to render artwork before it can compile.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tray import icon_image  # noqa: E402  - needs the path set up first

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    target = ROOT / "packaging" / "icon.ico"
    image = icon_image("idle").resize((256, 256))
    image.save(target, format="ICO", sizes=SIZES)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
