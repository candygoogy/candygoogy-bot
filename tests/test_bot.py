"""bot.py: .env is loaded only here, and a bad configuration stops it cleanly."""

import importlib
import inspect
import os
import pkgutil
import sys
from pathlib import Path

import pytest

import bot
import candygoogy
from candygoogy import app as application


@pytest.fixture(autouse=True)
def no_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """bot.py reads tmp_path/.env, which a test may write, never the repo's."""
    path = tmp_path / ".env"
    monkeypatch.setattr(bot, "ENV_FILE", path)
    return path


@pytest.fixture(autouse=True)
def plain_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() without arguments reads sys.argv, which is pytest's own here."""
    monkeypatch.setattr(sys, "argv", ["bot.py"])


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replaces the app's main(); each call is recorded instead of polling Telegram."""
    calls: list[int] = []
    monkeypatch.setattr(application, "main", lambda: calls.append(1))
    return calls


def test_bad_config_exits_2_with_a_plain_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runs: list[int]
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:SECRET-Zq9-pasted-in-the-wrong-place-00")
    monkeypatch.setenv("WORKSTATION_IP", "SECRET-Zq9")
    monkeypatch.delenv("SSH_USER")
    assert bot.start([]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("The configuration has problems:")
    assert "- WORKSTATION_IP must be an IPv4 or IPv6 address" in err
    assert "- SSH_USER is missing." in err
    assert "Traceback" not in err
    assert "Zq9" not in err
    assert runs == []  # the app was never started


def test_good_config_runs_the_app(capsys: pytest.CaptureFixture[str], runs: list[int]) -> None:
    assert bot.start([]) == 0
    assert runs == [1]
    assert capsys.readouterr().err == ""


def test_dotenv_fills_gaps_but_never_overrides(
    no_dotenv_file: Path, monkeypatch: pytest.MonkeyPatch, runs: list[int]
) -> None:
    monkeypatch.delenv("SSH_USER")
    monkeypatch.setenv("WORKSTATION_IP", "192.0.2.10")
    _ = no_dotenv_file.write_text("SSH_USER=from-dotenv\nWORKSTATION_IP=not-an-ip\n")
    assert bot.start([]) == 0  # the bad IP in .env was never used
    assert runs == [1]
    assert os.environ["SSH_USER"] == "from-dotenv"
    assert os.environ["WORKSTATION_IP"] == "192.0.2.10"


def test_the_package_never_loads_dotenv() -> None:
    """Only bot.py does, so importing the package (as the tests do) can't read a real .env."""
    for info in pkgutil.iter_modules(candygoogy.__path__):
        module = importlib.import_module(f"candygoogy.{info.name}")
        assert "dotenv" not in inspect.getsource(module), info.name


def test_check_config_says_ok_and_never_starts(
    capsys: pytest.CaptureFixture[str], runs: list[int]
) -> None:
    assert bot.start(["--check-config"]) == 0
    out, err = capsys.readouterr()
    assert out == "OK: the configuration is valid.\n"
    assert err == ""
    assert runs == []


def test_check_config_reports_problems_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runs: list[int]
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "not-a-token-SECRET-Zq9")
    assert bot.start(["--check-config"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("The configuration has problems:")
    assert "- BOT_TOKEN doesn't look like a bot token" in err
    assert "Zq9" not in err
    assert runs == []


def test_check_config_reads_dotenv(
    no_dotenv_file: Path, monkeypatch: pytest.MonkeyPatch, runs: list[int]
) -> None:
    monkeypatch.delenv("SSH_USER")
    assert bot.start(["--check-config"]) == 2  # SSH_USER is missing
    _ = no_dotenv_file.write_text("SSH_USER=from-dotenv\n")
    assert bot.start(["--check-config"]) == 0
    assert runs == []


def test_the_command_line_is_read_when_not_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runs: list[int]
) -> None:
    monkeypatch.setattr(sys, "argv", ["bot.py", "--check-config"])
    assert bot.start() == 0
    assert capsys.readouterr().out.startswith("OK")
    assert runs == []


@pytest.mark.parametrize(
    "argv", [["--check"], ["--check-config", "extra"], ["-h"], ["--check-config=1"]]
)
def test_unknown_arguments_are_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str], runs: list[int]
) -> None:
    assert bot.start(argv) == 2
    assert capsys.readouterr().err == "usage: python bot.py [--check-config]\n"
    assert runs == []
