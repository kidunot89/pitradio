import hashlib
import json

import pytest

from pitradio import updater
from pitradio.state import UpdateInfo

# -- version comparison --------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v1.2.3", (1, 2, 3)), ("1.10.0", (1, 10, 0)), ("v2", (2,)),
     ("v1.0.0-beta.1", (1, 0, 0)), ("v1.0.0+build9", (1, 0, 0))],
)
def test_parse_version(tag, expected):
    assert updater.parse_version(tag) == expected


def test_is_newer_compares_numerically_not_lexically():
    assert updater.is_newer("v1.10.0", "v1.9.9") is True
    assert updater.is_newer("v0.1.1", "v0.1.0") is True
    assert updater.is_newer("v1.0.0", "1.0.0") is False
    assert updater.is_newer("v0.9.0", "v1.0.0") is False


# -- host allowlist ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://api.github.com/repos/x/y/releases/latest",
     "https://github.com/x/y/releases/download/v1/setup.exe",
     "https://objects.githubusercontent.com/blob"],
)
def test_github_hosts_are_allowed(url):
    updater._check_host(url)


@pytest.mark.parametrize(
    "url",
    ["http://github.com/x",                   # not https
     "https://evil.example.com/setup.exe",
     "https://github.com.evil.example/x",     # suffix confusion
     "https://notgithub.com/x"],
)
def test_other_hosts_are_refused(url):
    with pytest.raises(ValueError, match="unexpected host"):
        updater._check_host(url)


# -- release checking ----------------------------------------------------


def _release(tag="v0.2.0", assets=None):
    if assets is None:
        assets = [
            {"name": "pitradio-setup-0.2.0.exe",
             "browser_download_url": "https://github.com/k/p/releases/download/v0.2.0/pitradio-setup-0.2.0.exe"},
            {"name": "SHA256SUMS",
             "browser_download_url": "https://github.com/k/p/releases/download/v0.2.0/SHA256SUMS"},
        ]
    return {"tag_name": tag, "body": "notes here", "assets": assets}


class _Config:
    repo = "kidunot89/pitradio"


def test_check_returns_info_for_a_newer_release(monkeypatch):
    monkeypatch.setattr(updater, "_get", lambda *a, **k: json.dumps(_release()).encode())
    info = updater.check(_Config(), "0.1.0")
    assert info is not None
    assert info.version == "v0.2.0"
    assert info.asset_name == "pitradio-setup-0.2.0.exe"
    assert info.notes == "notes here"


def test_check_returns_none_when_already_current(monkeypatch):
    monkeypatch.setattr(updater, "_get", lambda *a, **k: json.dumps(_release()).encode())
    assert updater.check(_Config(), "0.2.0") is None


def test_check_refuses_a_release_with_no_checksums(monkeypatch):
    """Without SHA256SUMS there is nothing to verify against, so don't offer it."""
    assets = [{"name": "pitradio-setup-0.2.0.exe",
               "browser_download_url": "https://github.com/k/p/x.exe"}]
    monkeypatch.setattr(
        updater, "_get", lambda *a, **k: json.dumps(_release(assets=assets)).encode())
    assert updater.check(_Config(), "0.1.0") is None


def test_check_refuses_a_release_with_no_installer(monkeypatch):
    assets = [{"name": "SHA256SUMS", "browser_download_url": "https://github.com/k/p/s"}]
    monkeypatch.setattr(
        updater, "_get", lambda *a, **k: json.dumps(_release(assets=assets)).encode())
    assert updater.check(_Config(), "0.1.0") is None


def test_check_survives_a_network_failure(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(updater, "_get", boom)
    assert updater.check(_Config(), "0.1.0") is None


# -- download verification -----------------------------------------------


def _info():
    return UpdateInfo(
        version="v0.2.0", notes="",
        asset_url="https://github.com/k/p/releases/download/v0.2.0/setup.exe",
        checksum_url="https://github.com/k/p/releases/download/v0.2.0/SHA256SUMS",
        asset_name="setup.exe",
    )


def _fake_get(payload: bytes, sums: str):
    def get(url, **kwargs):
        return sums.encode() if url.endswith("SHA256SUMS") else payload
    return get


def test_download_writes_the_file_when_the_hash_matches(monkeypatch, tmp_path):
    payload = b"installer bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater, "_get", _fake_get(payload, f"{digest}  setup.exe\n"))

    target = updater.download(_info(), tmp_path)
    assert target.read_bytes() == payload


def test_download_refuses_a_hash_mismatch(monkeypatch, tmp_path):
    """This is the whole point of the mechanism — a bad payload must not land."""
    monkeypatch.setattr(
        updater, "_get", _fake_get(b"tampered", f"{'0' * 64}  setup.exe\n"))

    with pytest.raises(ValueError, match="checksum mismatch"):
        updater.download(_info(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_when_the_asset_is_absent_from_sha256sums(monkeypatch, tmp_path):
    monkeypatch.setattr(
        updater, "_get", _fake_get(b"x", f"{'0' * 64}  somethingelse.exe\n"))

    with pytest.raises(ValueError, match="no entry for"):
        updater.download(_info(), tmp_path)


def test_checksum_line_with_binary_marker_is_accepted(monkeypatch, tmp_path):
    """sha256sum writes ' *name' for binary mode; both spellings should work."""
    payload = b"installer bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater, "_get", _fake_get(payload, f"{digest} *setup.exe\n"))

    assert updater.download(_info(), tmp_path).read_bytes() == payload


# -- the installer handoff -----------------------------------------------



def test_the_installer_is_started_directly(tmp_path, monkeypatch):
    """No shim. The PowerShell one never ran on any machine that tried it —
    its transcript, written as the script's first statement, was never
    created — while every successful self-update used a direct launch."""
    from pathlib import Path

    installer = tmp_path / "pitradio-setup-9.9.9.exe"
    installer.write_text("stub", encoding="utf-8")

    spawned = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(cmd=cmd, kw=kw))

    updater.launch_installer(installer, Path("C:/app/pitradio.exe"))

    assert spawned["cmd"][0] == str(installer)
    assert "powershell" not in " ".join(spawned["cmd"]).lower()


def test_the_installer_is_told_to_be_silent(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_text("stub", encoding="utf-8")
    spawned = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(cmd=cmd))

    updater.launch_installer(installer)

    for flag in ("/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"):
        assert flag in spawned["cmd"]


def test_setup_is_not_asked_to_close_this_app(tmp_path, monkeypatch):
    """/CLOSEAPPLICATIONS drives the Restart Manager, which needs the target to
    register and answer shutdown requests. A tkinter app does neither, so Setup
    stalls on "Closing applications" behind a dialog. v0.1.13 shipped that."""
    installer = tmp_path / "setup.exe"
    installer.write_text("stub", encoding="utf-8")
    spawned = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(cmd=cmd))

    updater.launch_installer(installer)
    assert "/CLOSEAPPLICATIONS" not in spawned["cmd"]


def test_all_three_streams_are_redirected(tmp_path, monkeypatch):
    """A GUI build launched from a shortcut has invalid standard handles, and
    inheriting them fails process creation with [WinError 6]."""
    installer = tmp_path / "setup.exe"
    installer.write_text("stub", encoding="utf-8")
    spawned = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(kw=kw))

    updater.launch_installer(installer)
    for stream in ("stdin", "stdout", "stderr"):
        assert spawned["kw"][stream] is updater.subprocess.DEVNULL


def test_a_missing_installer_is_refused(tmp_path):
    """Better a visible error than exiting the app for nothing."""
    import pytest

    with pytest.raises(FileNotFoundError):
        updater.launch_installer(tmp_path / "not-there.exe")
