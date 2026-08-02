"""Where state lives.

Installed builds must not write beside the executable: under Program Files
that either fails or gets redirected into UAC's VirtualStore, where the app's
own mtime check never sees the file it just wrote and hot-reload silently
stops working.
"""

import paths


def test_source_mode_keeps_everything_beside_the_checkout(monkeypatch):
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    root = paths.install_dir()

    assert paths.config_path() == root / "config.json"
    assert paths.log_dir() == root / "logs"
    assert paths.model_dir() == root / "models"


def test_installed_mode_uses_the_windows_profile_directories(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert paths.config_path() == tmp_path / "Roaming" / "pitradio" / "config.json"
    assert paths.log_dir() == tmp_path / "Local" / "pitradio" / "logs"
    assert paths.model_dir() == tmp_path / "Local" / "pitradio" / "models"


def test_model_cache_is_outside_the_install_directory(monkeypatch, tmp_path):
    """An update replaces the install directory; a 250MB re-download would not do."""
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    assert paths.install_dir() not in paths.model_dir().parents


def test_default_config_seed_ships_beside_the_app():
    assert paths.default_config_path().name == "config.default.json"
    assert paths.default_config_path().parent == paths.install_dir()


def test_falls_back_when_the_environment_is_stripped(monkeypatch):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    # Must still produce a usable absolute path rather than raising.
    assert paths.config_path().is_absolute()
    assert "pitradio" in str(paths.config_path())
