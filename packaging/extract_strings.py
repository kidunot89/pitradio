"""Collect every translatable string into the catalogue template.

    python packaging/extract_strings.py           # rewrite the template
    python packaging/extract_strings.py --check   # fail if it is out of date

Finds every `t("…")` call in the app and writes the source strings to
`src/pitradio/locale/template.json` with empty values. A contributor copies
that to `<code>.json` and fills it in.

`--check` runs in CI, so adding a string without regenerating fails there
rather than shipping a catalogue that silently lacks it.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pitradio"
TEMPLATE = SRC / "locale" / "template.json"


def _strings_in(path: Path) -> list[str]:
    """Source strings passed to t(), in the order they appear."""
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute)
                else None)
        if name != "t" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append(first.value)
        else:
            # A computed string cannot be extracted, so it would never be
            # translated — worth failing over rather than silently skipping.
            raise SystemExit(
                f"{path.relative_to(ROOT)}:{first.lineno}: t() needs a literal "
                f"string so it can be extracted"
            )
    return found


#: i18n.py *is* the mechanism, so its own `t()` takes a variable by definition.
#: Scanning it would fail the literal check on the implementation of the check.
SKIP = {SRC / "i18n.py"}


def collect() -> dict[str, str]:
    strings: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in SKIP:
            continue
        strings.extend(_strings_in(path))
    # dict.fromkeys keeps first-seen order and drops duplicates, so the
    # template reads roughly in the order a user meets the strings.
    return dict.fromkeys(strings, "")


def main() -> int:
    catalogue = collect()
    rendered = json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n"

    if "--check" in sys.argv:
        current = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else ""
        if current != rendered:
            raise SystemExit(
                f"{TEMPLATE.relative_to(ROOT)} is out of date "
                f"({len(catalogue)} strings found).\n"
                f"Run: python packaging/extract_strings.py"
            )
        print(f"{TEMPLATE.relative_to(ROOT)} is up to date ({len(catalogue)} strings)")
        return 0

    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE.write_text(rendered, encoding="utf-8")
    print(f"wrote {TEMPLATE.relative_to(ROOT)} ({len(catalogue)} strings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
