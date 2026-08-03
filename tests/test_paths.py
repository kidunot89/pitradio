"""Where state lives.

Installed builds must not write beside the executable: under Program Files
that either fails or gets redirected into UAC's VirtualStore, where the app's
own mtime check never sees the file it just wrote and hot-reload silently
stops working.
"""

from pathlib import Path

from pitradio import paths


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


def test_a_source_run_uses_the_repository_root_not_the_package():
    """The package moved to src/pitradio/ and this silently followed it.

    Config, logs and the model cache all hang off install_dir(), and
    config.default.json sits at the repository root — so resolving to the
    package directory meant a source run looked for its config inside the
    source tree and found no seed to copy. Nothing failed; it just reported
    "not found; using built-in defaults" and carried on.
    """
    root = Path(__file__).parent.parent

    assert paths.install_dir() == root
    assert paths.config_path() == root / "config.json"
    assert paths.default_config_path() == root / "config.default.json"
    assert paths.default_config_path().exists(), (
        "the shipped seed must be where install_dir() looks for it"
    )
