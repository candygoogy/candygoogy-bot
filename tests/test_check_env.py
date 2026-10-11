"""deploy/check-env.sh: runs in CI on Linux; skipped on Windows (bash may be WSL's).
What .env.example requires is also checked without bash, in test_deploy_files.py."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "check-env.sh"
EXAMPLE = ROOT / ".env.example"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)

# The two required variables; the machine settings are the loader's business.
COMPLETE = "BOT_TOKEN=1:fake\nAUTHORIZED_USER_IDS=1\n"


def check(tmp_path: Path, env_text: str | None) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env"
    if env_text is not None:
        _ = env_file.write_text(env_text)
    return subprocess.run(
        ["bash", str(SCRIPT), str(EXAMPLE), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_complete_env_passes(tmp_path: Path) -> None:
    result = check(tmp_path, COMPLETE)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "extra",
    [
        "",  # no machine at all: --check-config reports that, not this script
        "MACHINES_FILE=/etc/candygoogy/machines.toml\n",
        "WORKSTATION_MAC=00:00:5e:00:53:01\nWORKSTATION_IP=192.0.2.10\nSSH_USER=u\n",
    ],
)
def test_machine_settings_are_not_required_here(tmp_path: Path, extra: str) -> None:
    assert check(tmp_path, COMPLETE + extra).returncode == 0


def test_missing_names_are_listed(tmp_path: Path) -> None:
    env = "# AUTHORIZED_USER_IDS=commented\nWORKSTATION_IP=192.0.2.10\n"
    result = check(tmp_path, env)
    assert result.returncode == 1
    assert "BOT_TOKEN" in result.stderr and "AUTHORIZED_USER_IDS" in result.stderr
    assert "WORKSTATION" not in result.stderr


def test_export_form_counts(tmp_path: Path) -> None:
    env = COMPLETE.replace("BOT_TOKEN=", "export BOT_TOKEN=")
    assert check(tmp_path, env).returncode == 0


@pytest.mark.parametrize("empty", ["BOT_TOKEN=", 'BOT_TOKEN=""', "BOT_TOKEN=''"])
def test_empty_values_fail(tmp_path: Path, empty: str) -> None:
    env = COMPLETE.replace("BOT_TOKEN=1:fake", empty)
    result = check(tmp_path, env)
    assert result.returncode == 1
    assert "empty" in result.stderr and "BOT_TOKEN" in result.stderr


def test_missing_env_file_fails(tmp_path: Path) -> None:
    result = check(tmp_path, None)
    assert result.returncode == 1 and "not found" in result.stderr


def test_values_are_never_printed(tmp_path: Path) -> None:
    result = check(tmp_path, "BOT_TOKEN=SECRET-VALUE\nWORKSTATION_IP=192.0.2.10\n")
    assert result.returncode == 1
    assert "SECRET-VALUE" not in result.stdout + result.stderr
    assert "192.0.2.10" not in result.stdout + result.stderr
