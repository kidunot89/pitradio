"""The release artifact format and the updater's parser must agree.

They are written in different places and only meet during a real release, so
this pins them together: generate SHA256SUMS exactly as the release workflow
does, then read it back with the code the app uses to verify a download.
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "packaging"))

import checksums

from pitradio import updater
from pitradio.state import UpdateInfo


def _artifacts(tmp_path: Path) -> dict[str, bytes]:
    payloads = {
        "pitradio-setup-0.1.0.exe": b"installer bytes",
        "pitradio-portable-0.1.0.zip": b"portable bytes",
    }
    for name, data in payloads.items():
        (tmp_path / name).write_bytes(data)
    return payloads


def test_writes_one_line_per_artifact(tmp_path):
    _artifacts(tmp_path)
    text = checksums.write_checksums(tmp_path).read_text()

    assert len(text.strip().splitlines()) == 2
    assert text.endswith("\n")


def test_excludes_the_checksum_file_itself(tmp_path):
    """The bug that broke the first release: hashing the file being written."""
    _artifacts(tmp_path)
    (tmp_path / "SHA256SUMS").write_text("stale content\n")

    text = checksums.write_checksums(tmp_path).read_text()
    assert "SHA256SUMS" not in text


def test_is_stable_when_run_twice(tmp_path):
    _artifacts(tmp_path)
    first = checksums.write_checksums(tmp_path).read_text()
    second = checksums.write_checksums(tmp_path).read_text()
    assert first == second


def test_hashes_are_correct(tmp_path):
    payloads = _artifacts(tmp_path)
    text = checksums.write_checksums(tmp_path).read_text()

    for name, data in payloads.items():
        assert f"{hashlib.sha256(data).hexdigest()}  {name}" in text


def test_empty_directory_is_an_error(tmp_path):
    with pytest.raises(SystemExit):
        checksums.write_checksums(tmp_path)


def test_the_updater_can_verify_what_the_release_produces(tmp_path, monkeypatch):
    """End to end across the seam: workflow writes it, the app reads it."""
    payloads = _artifacts(tmp_path)
    sums = checksums.write_checksums(tmp_path).read_text()

    name = "pitradio-setup-0.1.0.exe"
    info = UpdateInfo(
        version="v0.1.0", notes="",
        asset_url=f"https://github.com/kidunot89/pitradio/releases/download/v0.1.0/{name}",
        checksum_url="https://github.com/kidunot89/pitradio/releases/download/v0.1.0/SHA256SUMS",
        asset_name=name,
    )

    def fake_get(url, **kwargs):
        return sums.encode() if url.endswith("SHA256SUMS") else payloads[name]

    monkeypatch.setattr(updater, "_get", fake_get)

    downloaded = updater.download(info, tmp_path / "dest")
    assert downloaded.read_bytes() == payloads[name]
