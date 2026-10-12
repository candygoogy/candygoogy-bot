# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/about."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from candygoogy import (
    __version__,
    about,
    auth,
    config,
    handlers,
    system,
)
from candygoogy import app as application
from tests.helpers import (
    FakeRun,
    call,
    fake_app,
    make_update,
    replies,
)

# --- /about --------------------------------------------------------------------


# What /about's first line starts with.
RELEASE = f"candygoogy {__version__} · "
GIT_LOG = system.RunResult(0, out="abc1234\x1fAdd /about\x1f1759400000\n")


def script_git(fake: FakeRun, log: system.RunResult, dirty: bool = False) -> None:
    fake.by_last_arg[about.GIT_LOG_FORMAT] = log
    fake.by_last_arg[about.GIT_DIRTY_ARG] = system.RunResult(0, out=" M bot.py\n" if dirty else "")


def about_text() -> str:
    update = make_update()
    call(about.about, update)
    [text] = replies(update)
    return text


def test_about_shows_commit_uptime_and_versions(fake_run: FakeRun) -> None:
    script_git(fake_run, GIT_LOG)
    asyncio.run(about.record_startup_version())
    text = about_text()
    assert RELEASE + "Running <code>abc1234</code> · Add /about" in text
    assert "Up " in text and "Python " in text and "python-telegram-bot " in text
    assert "uncommitted" not in text and "not running" not in text
    # Only read-only git queries, nothing else:
    assert {c[0] for c in fake_run.calls} == {"git"}
    assert all(("log" in c) != ("status" in c) for c in fake_run.calls)


def test_about_warns_when_pulled_but_not_restarted(fake_run: FakeRun) -> None:
    script_git(fake_run, GIT_LOG)
    asyncio.run(about.record_startup_version())  # the bot started on abc1234
    script_git(fake_run, system.RunResult(0, out="def5678\x1fNewer\x1f1759500000\n"))
    text = about_text()
    assert "Running <code>abc1234</code>" in text
    assert "<code>def5678</code> is checked out but not running" in text


def test_about_flags_uncommitted_changes(fake_run: FakeRun) -> None:
    script_git(fake_run, GIT_LOG, dirty=True)
    asyncio.run(about.record_startup_version())
    assert "uncommitted changes" in about_text()


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        (system.RunResult(128, "fatal: detected dubious ownership in repository\nmore"), "dubious"),
        (system.RunResult(-1, "No such file or directory: 'git'"), "No such file"),
        (system.RunResult(0, out="garbage"), "unexpected output"),
    ],
)
def test_about_survives_git_problems(fake_run: FakeRun, log: system.RunResult, reason: str) -> None:
    script_git(fake_run, log)
    asyncio.run(about.record_startup_version())
    text = about_text()
    assert RELEASE + "Commit unknown" in text and reason in text
    assert "more" not in text  # only git's first error line


IMAGE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def no_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The code runs from a folder without .git, as in the container image."""
    monkeypatch.setattr(about, "REPO_DIR", tmp_path)
    return tmp_path


@pytest.mark.usefixtures("no_checkout")
def test_about_shows_the_image_commit_without_a_checkout(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(about, "BUILD_COMMIT", IMAGE_COMMIT)
    asyncio.run(about.record_startup_version())
    text = about_text()
    image_line = "Running <code>0123456</code> <i>(the commit the image was built from)</i>."
    assert RELEASE + image_line in text
    assert "Commit unknown" not in text and "not running" not in text
    assert fake_run.calls == []  # git isn't asked (the image has none)


def test_about_prefers_the_checkout_to_the_image_commit(
    fake_run: FakeRun, no_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (no_checkout / ".git").mkdir()
    monkeypatch.setattr(about, "BUILD_COMMIT", IMAGE_COMMIT)
    script_git(fake_run, GIT_LOG)
    asyncio.run(about.record_startup_version())
    text = about_text()
    assert "Running <code>abc1234</code> · Add /about" in text
    assert "0123456" not in text


@pytest.mark.usefixtures("no_checkout")
def test_about_without_a_checkout_or_image_commit(fake_run: FakeRun) -> None:
    assert config.BUILD_COMMIT is None
    script_git(fake_run, system.RunResult(128, "fatal: not a git repository"))
    asyncio.run(about.record_startup_version())
    assert RELEASE + "Commit unknown <i>(git: fatal: not a git repository)</i>." in about_text()


def test_about_escapes_and_clips_the_subject(fake_run: FakeRun) -> None:
    subject = "<b>x</b> " + "y" * 200
    script_git(fake_run, system.RunResult(0, out=f"abc1234\x1f{subject}\x1f1759400000"))
    asyncio.run(about.record_startup_version())
    text = about_text()
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    assert "y" * 100 not in text and "…" in text


@pytest.mark.parametrize(
    ("state_dir", "writable", "expected"),
    [
        ("/var/lib/candygoogy-bot", True, "Timers and the last power action are saved in"),
        ("", True, "saved next to the code"),
        ("", False, "won't survive a restart"),
    ],
)
def test_about_timers_line(
    monkeypatch: pytest.MonkeyPatch, state_dir: str, writable: bool, expected: str
) -> None:
    def access(_path: object, _mode: int) -> bool:
        return writable

    monkeypatch.setenv("STATE_DIRECTORY", state_dir)
    monkeypatch.setattr(os, "access", access)
    assert expected in about.timers_line()


@pytest.mark.parametrize(
    ("tz", "name", "abbrev", "offset", "expected", "hint"),
    [
        (
            "",
            "America/New_York",
            "EDT",
            "-0400",
            "Times are in America/New_York (EDT, UTC-04:00).",
            False,
        ),
        (
            "America/New_York",
            "America/New_York",
            "EST",
            "-0500",
            "America/New_York (EST, UTC-05:00)",
            False,
        ),
        ("", "Etc/UTC", "UTC", "+0000", "Times are in Etc/UTC (UTC, UTC+00:00).", True),
        ("UTC", "UTC", "UTC", "+0000", "UTC (UTC, UTC+00:00)", False),  # chosen on purpose
        ("", None, "Eastern Daylight Time", "", "Times are in Eastern Daylight Time.", False),
    ],
)
def test_about_timezone_line(
    monkeypatch: pytest.MonkeyPatch,
    tz: str,
    name: str | None,
    abbrev: str,
    offset: str,
    expected: str,
    hint: bool,
) -> None:
    monkeypatch.setenv("TZ", tz)
    line = about.timezone_line(name, abbrev, offset)
    assert expected in line
    assert ("TZ=America/New_York" in line) == hint


def test_about_zone_name_prefers_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", ":America/New_York")
    assert about.zone_name() == "America/New_York"


def test_about_shows_the_timezone(fake_run: FakeRun) -> None:
    script_git(fake_run, GIT_LOG)
    assert "🕒 Times are in " in about_text()


def test_about_is_in_the_menu_and_guarded() -> None:
    assert "about" in handlers.COMMANDS
    assert "about" in [name for name, _, _ in handlers.HELP]
    assert auth.is_guarded(handlers.COMMANDS["about"])


def test_startup_records_the_running_version(fake_run: FakeRun) -> None:
    script_git(fake_run, GIT_LOG)
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(return_value=[])
    asyncio.run(application.on_startup(app))
    assert "abc1234" in about.version_line(about._startup_version)  # pyright: ignore[reportPrivateUsage]
