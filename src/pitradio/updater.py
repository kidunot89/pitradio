"""Self-update against GitHub Releases.

Standard library only, so this adds nothing to the bundle: urllib for the API
and downloads, hashlib for verification.

Understand what this does before trusting it: it downloads an installer and
runs it with administrator rights. Because builds are unsigned, the only things
authenticating that installer are GitHub's TLS and a checksum file published in
the same release. That defends against a corrupted or truncated download, not
against someone who controls the repository. Auto-install is therefore opt-in,
and the README says so plainly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from pitradio import paths
from pitradio.state import AppState, UpdateInfo

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 20
CHECKSUM_ASSET = "SHA256SUMS"

# Asset downloads redirect to githubusercontent; anything off these hosts means
# the response was not what we think it was.
ALLOWED_HOSTS = ("github.com", "githubusercontent.com")

_VERSION_PART = re.compile(r"\d+")


def parse_version(tag: str) -> tuple[int, ...]:
    """"v1.2.3" -> (1, 2, 3). Non-numeric suffixes are ignored, not ranked."""
    core = tag.strip().lstrip("vV").split("-")[0].split("+")[0]
    return tuple(int(m.group()) for m in _VERSION_PART.finditer(core)) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _check_host(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # Exact match or a genuine subdomain — a plain endswith() would also accept
    # an attacker-registered "notgithub.com", which is the whole point of the
    # check being here.
    allowed = any(host == known or host.endswith("." + known) for known in ALLOWED_HOSTS)
    if parsed.scheme != "https" or not allowed:
        raise ValueError(f"refusing to fetch {url!r}: unexpected host")


def _get(url: str, *, accept: str = "application/vnd.github+json") -> bytes:
    _check_host(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            # GitHub rejects requests without one.
            "User-Agent": "pitradio-updater",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def installed_via_installer() -> bool:
    """True for an Inno Setup install, False for the portable zip or source.

    Inno leaves its uninstaller beside the app; the portable zip has no way to
    be updated in place, so those users are pointed at the release page.
    """
    if not paths.is_frozen():
        return False
    return any(paths.install_dir().glob("unins*.exe"))


def check(update_cfg, current_version: str) -> UpdateInfo | None:
    """Ask GitHub for the latest release. Returns None when already current."""
    url = API.format(repo=update_cfg.repo)
    try:
        payload = json.loads(_get(url))
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        log.info("update check failed (this is not fatal): %s", exc)
        return None

    tag = str(payload.get("tag_name") or "")
    if not tag or not is_newer(tag, current_version):
        log.info("no update available (latest %s, running %s)", tag or "?", current_version)
        return None

    assets = {a.get("name", ""): a.get("browser_download_url", "")
              for a in payload.get("assets") or []}

    installer = next(
        (name for name in assets
         if name.lower().endswith(".exe") and "setup" in name.lower()),
        None,
    )
    if not installer or CHECKSUM_ASSET not in assets:
        log.warning(
            "release %s has no installer or no %s asset; skipping", tag, CHECKSUM_ASSET
        )
        return None

    return UpdateInfo(
        version=tag,
        notes=str(payload.get("body") or "").strip(),
        asset_url=assets[installer],
        checksum_url=assets[CHECKSUM_ASSET],
        asset_name=installer,
    )


def download(info: UpdateInfo, dest_dir: Path) -> Path:
    """Download the installer and refuse to return it unless the hash matches."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / info.asset_name

    expected = _expected_hash(info)
    if expected is None:
        raise ValueError(f"{CHECKSUM_ASSET} has no entry for {info.asset_name}")

    log.info("downloading %s", info.asset_name)
    blob = _get(info.asset_url, accept="application/octet-stream")

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ValueError(
            f"checksum mismatch for {info.asset_name}: expected {expected}, got {actual}"
        )

    target.write_bytes(blob)
    log.info("verified %s (%.1f MB)", info.asset_name, len(blob) / 1_048_576)
    return target


def _expected_hash(info: UpdateInfo) -> str | None:
    text = _get(info.checksum_url, accept="text/plain").decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == info.asset_name:
            return parts[0].lower()
    return None


def shim_command(installer: Path, pid: int, app: Path) -> str:
    """PowerShell that waits for us to exit, installs, then relaunches.

    The obvious approach — start the installer with /CLOSEAPPLICATIONS and let
    it shut us down — does not work. That uses the Windows Restart Manager,
    which needs the target application to register with it and answer shutdown
    requests. A tkinter app does neither, so Setup stalls on "Closing
    applications" and asks the user what to do. That is exactly what v0.1.13
    did on the first real self-update.

    Inverting it removes the problem: PitRadio exits first, and by the time the
    installer touches a file there is nothing holding one. Waiting on the PID
    rather than sleeping a fixed interval matters — shutdown has a model to
    unload and four threads to join, and a guessed delay would be a race.

    /RESTARTAPPLICATIONS is gone with it: it only restarts what Setup itself
    closed, and Setup no longer closes anything. The shim relaunches instead,
    and only if the install succeeded.
    """
    return (
        f"Wait-Process -Id {pid} -Timeout 60 -ErrorAction SilentlyContinue; "
        f"$p = Start-Process -FilePath '{installer}' "
        f"-ArgumentList '/SILENT','/NORESTART','/SUPPRESSMSGBOXES' -Wait -PassThru; "
        f"if ($p.ExitCode -eq 0) {{ Start-Process -FilePath '{app}' }}"
    )


def launch_installer(installer: Path, app: Path | None = None) -> None:
    """Hand off to the installer and expect the caller to exit immediately.

    We already run elevated, so the shim and the installer inherit that and no
    second UAC prompt appears.
    """
    if app is None:
        app = Path(sys.executable)

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-Command", shim_command(installer, os.getpid(), app),
        ],
        creationflags=creation_flags,
        close_fds=True,
        # All three streams, explicitly. This runs from the GUI, which when
        # launched from a shortcut has no console and invalid standard handles —
        # inheriting them fails process creation with [WinError 6]. Leaving this
        # out would break self-update precisely for the users who install
        # normally, and work for anyone testing from a terminal.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log.info("update handed off; exiting so the installer can replace this build")


class UpdateChecker(threading.Thread):
    """Periodic check. Short-lived work, so it stays a daemon and never blocks exit."""

    def __init__(
        self,
        store,
        app_state: AppState,
        current_version: str,
        is_busy,
        on_ready=None,
    ):
        super().__init__(name="updater", daemon=True)
        self.store = store
        self.state = app_state
        self.current_version = current_version
        # Called before installing: True means a sim is focused and we must wait.
        self.is_busy = is_busy
        self.on_ready = on_ready
        self._wake = threading.Event()
        self._stop = False

    def check_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        cfg = self.store.config.updates
        if cfg.check_on_start:
            self._once()

        while not self._stop:
            interval = max(1, self.store.config.updates.check_interval_hours) * 3600
            self._wake.wait(interval)
            self._wake.clear()
            if self._stop:
                return
            self._once()

    def _once(self) -> None:
        cfg = self.store.config.updates
        info = check(cfg, self.current_version)
        if info is None:
            return

        log.info("update available: %s", info.version)
        self.state.set_pending_update(info)

        if not cfg.auto_install:
            return
        if not installed_via_installer():
            log.info(
                "auto-install skipped: this is a portable or source install, so "
                "there is no installer to hand off to"
            )
            return
        if self.is_busy():
            log.info("update deferred: a sim is in focus and restarting mid-session would be worse")
            return

        try:
            installer = download(info, paths.log_dir().parent / "updates")
        except Exception as exc:
            log.error("update download failed: %s", exc)
            return

        if self.is_busy():
            # Re-checked because the download takes long enough for a session
            # to have started in the meantime.
            log.info("update ready but a sim is now focused; leaving it for later")
            return

        if self.on_ready is not None:
            self.on_ready(installer)


def stale_downloads_cleanup(keep: str | None = None) -> None:
    """Drop installers left behind by previous updates."""
    folder = paths.log_dir().parent / "updates"
    if not folder.exists():
        return
    for item in folder.glob("*.exe"):
        if keep and item.name == keep:
            continue
        try:
            item.unlink()
        except OSError:
            log.debug("could not remove %s", item)
