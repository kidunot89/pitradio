"""Where config, logs and the Whisper model live.

Installed builds and source checkouts store their state in different places, and
getting this wrong is silent: writing under Program Files either fails outright
or gets redirected into UAC's VirtualStore, where the app's own mtime check
never sees the file it just wrote and hot-reload quietly stops working.

Installed:  config in %APPDATA%, logs and models in %LOCALAPPDATA%.
Source:     everything beside the checkout, so development never scribbles in
            the real user directories.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "pitradio"


def is_frozen() -> bool:
    """True when running from a Nuitka standalone build rather than source."""
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))


def install_dir() -> Path:
    """Directory holding the executable (frozen) or the source checkout.

    From source this is the repository root, not the package directory. The
    package lives at `src/pitradio/`, and resolving to it would put a source
    run's config and logs inside the source tree — and `config.default.json`,
    which sits at the root, would never be found to seed from.
    """
    if is_frozen():
        return Path(sys.executable).parent
    # src/pitradio/paths.py -> src/pitradio -> src -> repository root
    return Path(__file__).resolve().parent.parent.parent


def _windows_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    if base:
        return Path(base) / APP_NAME
    # Non-Windows (macOS development) or a stripped environment.
    return Path.home() / fallback / APP_NAME


def _roaming() -> Path:
    return _windows_dir("APPDATA", ".config")


def _local() -> Path:
    return _windows_dir("LOCALAPPDATA", ".local/share")


def config_path() -> Path:
    """The live, user-editable config."""
    if is_frozen():
        return _roaming() / "config.json"
    return install_dir() / "config.json"


def icon_path() -> Path | None:
    """The bundled .ico, or None when it is not beside us.

    Windows wants a real multi-resolution ICO for the taskbar; see
    `gui.App._set_window_icon`. Frozen builds get it from the dist root, a
    source run from `packaging/`. Returns None rather than a missing path so
    the caller can fall back instead of handling an exception.
    """
    for candidate in (install_dir() / "icon.ico",
                      install_dir() / "packaging" / "icon.ico"):
        if candidate.is_file():
            return candidate
    return None


def default_config_path() -> Path:
    """The read-only seed shipped alongside the app."""
    return install_dir() / "config.default.json"


def log_dir() -> Path:
    if is_frozen():
        return _local() / "logs"
    return install_dir() / "logs"


def model_dir() -> Path:
    """Whisper model cache.

    Deliberately outside the install directory: an update replaces the install
    directory wholesale, and re-downloading 250MB on every release would be a
    poor trade for a directory we can just as easily keep elsewhere.
    """
    if is_frozen():
        return _local() / "models"
    return install_dir() / "models"


def ensure_dirs() -> None:
    for path in (config_path().parent, log_dir(), model_dir()):
        path.mkdir(parents=True, exist_ok=True)


def describe() -> str:
    """One-line summary for the log and the GUI's about/status panel."""
    mode = "installed" if is_frozen() else "source"
    return (
        f"mode={mode} config={config_path()} logs={log_dir()} models={model_dir()}"
    )
