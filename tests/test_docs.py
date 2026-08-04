"""The documentation's own links.

A broken image renders as an icon and a filename on GitHub, which is the first
thing a visitor sees. Nothing else checks it, and the README references images
that are generated rather than committed — so the failure is a normal part of
the workflow rather than an unlikely mistake.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = ["README.md", "CONTRIBUTING.md", "DEVELOPING.md"]

COMMENT = re.compile(r"<!--.*?-->", re.S)
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE = re.compile(r'<img[^>]+src="([^"]+)"')
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)#][^)]*)\)")


def _rendered(name: str) -> str:
    """The document with HTML comments stripped, since they do not render."""
    return COMMENT.sub("", (ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DOCS)
def test_every_image_that_renders_exists(name):
    text = _rendered(name)
    referenced = IMAGE.findall(text) + HTML_IMAGE.findall(text)

    missing = [path for path in referenced
               if not path.startswith(("http://", "https://"))
               and not (ROOT / path).exists()]
    assert not missing, f"{name} shows a broken image for: {missing}"


@pytest.mark.parametrize("name", DOCS)
def test_every_local_link_resolves(name):
    text = _rendered(name)

    missing = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path = target.split("#")[0]
        if path and not (ROOT / path).exists():
            missing.append(target)
    assert not missing, f"{name} links to files that do not exist: {missing}"


def test_the_readme_shows_the_logo():
    """It is the first thing anyone sees, and it is generated — so it can vanish."""
    text = _rendered("README.md")
    assert "docs/images/logo.png" in text
    assert (ROOT / "docs" / "images" / "logo.png").exists()


def test_the_screenshot_markers_name_shots_the_tool_can_take():
    """A marker for a shot that packaging/screenshots.py cannot produce would
    sit in the README forever waiting for an image nobody can generate."""
    import sys

    sys.path.insert(0, str(ROOT / "packaging"))
    import screenshots

    raw = (ROOT / "README.md").read_text(encoding="utf-8")
    markers = set(re.findall(r"<!-- screenshot: (\S+)", raw))
    assert markers, "the usage section has no screenshot markers"

    unknown = sorted(markers - set(screenshots.SHOTS))
    assert not unknown, f"no such shot in screenshots.py: {unknown}"
