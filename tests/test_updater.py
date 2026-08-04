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



class Shim(str):
    """The generated script, carrying the paths as the script spells them.

    Assertions compare against `str(Path(...))`, never a literal — a
    forward-slash literal passes on Linux and macOS and fails on the one
    platform that ships. That mistake cost three CI runs before this existed.
    """

    installer: str
    app: str
    log: str


def _script(installer="C:/updates/setup.exe", pid=4321,
            app="C:/app/pitradio.exe", log="C:/logs/update-shim.log") -> Shim:
    from pathlib import Path

    parts = [Path(installer), Path(app), Path(log)]
    shim = Shim(updater.shim_script(parts[0], pid, parts[1], parts[2]))
    shim.installer, shim.app, shim.log = (str(part) for part in parts)
    return shim


def test_the_shim_waits_for_this_process_before_installing():
    """Setup cannot replace a file this process still holds open.

    /CLOSEAPPLICATIONS would be the obvious alternative and does not work: it
    drives the Restart Manager, which needs the app to register and answer, and
    a tkinter app does neither. v0.1.13 shipped that and stalled on a dialog.
    """
    script = _script(pid=4321)
    assert "Wait-Process -Id 4321" in script
    assert script.index("Wait-Process") < script.index("Start-Process -FilePath")


def test_the_shim_does_not_ask_setup_to_close_anything():
    script = _script()
    assert "/CLOSEAPPLICATIONS" not in script
    assert "/RESTARTAPPLICATIONS" not in script


def test_the_shim_relaunches_whatever_the_outcome():
    """A failed install leaves the previous build installed and working.

    Relaunching only on success — which is what this used to do — turns "the
    update failed" into "the app vanished", which is strictly worse and
    indistinguishable to whoever is looking at an empty screen.
    """
    script = _script()
    assert f"Start-Process -FilePath '{script.app}'" in script
    assert "$p.ExitCode -eq 0" not in script


def test_the_shim_suppresses_dialogs():
    script = _script()
    for flag in ("/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"):
        assert flag in script


def test_the_shim_waits_for_the_installer_to_finish():
    """Without -Wait the relaunch would race the file copy."""
    assert "-Wait" in _script()


def test_the_shim_records_what_it_did():
    """Its output went to DEVNULL, so a failed handoff looked exactly like the
    app closing by itself — which is precisely how it was first reported."""
    script = _script()
    assert f"Start-Transcript -Path '{script.log}'" in script
    assert "Stop-Transcript" in script
    assert "installer exit code:" in script


def test_the_shim_checks_the_installer_is_still_there():
    """Downloads are cleaned up between runs; a missing one must say so
    rather than failing silently at the point of no return."""
    script = _script()
    assert f"Test-Path -LiteralPath '{script.installer}'" in script


def test_the_shim_waits_for_the_exe_to_reappear():
    """The installer replaces it, so for a moment it is not there."""
    assert "Start-Sleep" in _script()


def test_the_handoff_refuses_a_missing_installer(tmp_path):
    """Better a visible error than exiting the app for nothing."""
    from pathlib import Path

    import pytest

    with pytest.raises(FileNotFoundError):
        updater.launch_installer(tmp_path / "not-there.exe", Path("app.exe"))


def test_the_shim_runs_from_a_file_not_a_command_string(tmp_path, monkeypatch):
    """A command string is flattened by subprocess, quoted by Windows, then
    re-parsed by PowerShell — three chances to mangle a path, with the output
    discarded so nothing says it happened."""
    from pathlib import Path

    from pitradio import paths

    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path)
    installer = tmp_path / "setup.exe"
    installer.write_text("stub")

    spawned = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(cmd=cmd, kw=kw))

    updater.launch_installer(installer, Path("C:/app/pitradio.exe"))

    assert "-File" in spawned["cmd"]
    assert "-Command" not in spawned["cmd"]
    assert (tmp_path / "update-shim.ps1").exists()
    # Restricted is the default policy and would refuse to run a .ps1.
    assert "Bypass" in spawned["cmd"]
    # The GUI has invalid standard handles when launched from a shortcut.
    assert spawned["kw"]["stdin"] is updater.subprocess.DEVNULL
