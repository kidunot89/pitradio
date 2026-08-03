"""Guards on the Inno Setup script.

Every one of these describes a failure that only appears on a real machine
after a real install, which is the most expensive place to find a bug in this
project — a build plus an install cycle per attempt.
"""

import re
from pathlib import Path

import pytest

ISS = Path(__file__).parent.parent / "packaging" / "pitradio.iss"


@pytest.fixture(scope="module")
def script() -> str:
    return ISS.read_text(encoding="utf-8")


def _directive(script: str, name: str) -> str | None:
    match = re.search(rf"^{name}=(.+)$", script, re.MULTILINE)
    return match.group(1).strip() if match else None


def _section(script: str, name: str) -> str:
    """Body of a [Section], comments stripped."""
    match = re.search(rf"^\[{name}\]\n(.*?)(?=^\[|\Z)", script, re.MULTILINE | re.DOTALL)
    assert match, f"no [{name}] section"
    return "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith(";")
    )


def test_postinstall_launch_uses_shellexec(script):
    """Inno runs postinstall entries via CreateProcess, as the originating user.

    CreateProcess refuses to start a requireAdministrator binary and fails with
    code 740 (ERROR_ELEVATION_REQUIRED) — which is exactly what v0.1.1 did on a
    real install. shellexec routes through ShellExecuteEx, which honours the
    manifest and raises the UAC prompt.
    """
    run = _section(script, "Run")
    assert "AppExe" in run, "expected a postinstall launch entry"
    assert "shellexec" in run, (
        "the postinstall launch must use shellexec or it fails with code 740 "
        "against our requireAdministrator manifest"
    )


def test_installer_requires_admin(script):
    """It writes to Program Files and registers a per-machine uninstall entry."""
    assert _directive(script, "PrivilegesRequired") == "admin"


def test_close_applications_is_enabled(script):
    """The self-updater hands off to this installer while the app is running."""
    assert _directive(script, "CloseApplications") == "yes"
    assert _directive(script, "RestartApplications") == "yes"


def test_output_name_matches_what_the_updater_looks_for(script):
    """updater.check picks the asset ending in .exe whose name contains 'setup'."""
    base = _directive(script, "OutputBaseFilename")
    assert base is not None
    assert "setup" in base.lower()
    assert "{#AppVersion}" in base, "the version must be in the filename"


def test_user_data_is_not_deleted_on_uninstall(script):
    """Config, logs and a 250MB cached model are the user's, not ours."""
    body = _section(script, "UninstallDelete")
    assert "{localappdata}\\pitradio\\updates" in body
    # Anything broader would take the model cache or the tuned config with it.
    assert "{userappdata}" not in body
    assert '"{app}"' not in body


def test_scheduled_task_is_removed_on_uninstall(script):
    """The app creates it via Settings; nothing else would ever clean it up."""
    assert "schtasks.exe" in script
    assert "/delete /f /tn PitRadio" in script


def test_app_id_is_stable():
    """Changing AppId makes Windows treat an upgrade as a separate product."""
    script = ISS.read_text(encoding="utf-8")
    app_id = _directive(script, "AppId")
    assert app_id == "{{A292DFB9-10EC-463E-B766-771B660524FA}"


def test_architecture_is_64bit(script):
    """ctranslate2 and PyAV ship 64-bit binaries only."""
    assert "x64" in (_directive(script, "ArchitecturesAllowed") or "")
