# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/week, the weekly report, and the PC on-time it reports."""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

from candygoogy import config, handlers, homelab, internet, pctime, probes, settings, weekly
from candygoogy.system import RunResult
from tests.helpers import (
    FakeHomelab,
    FakeRun,
    call,
    fake_app,
    make_button_press,
    make_update,
    replies,
    sent_texts,
)


def local(day: int, hour: int, minute: int = 0) -> float:
    """A local time in October 2026. The 5th is a Monday, the 11th a Sunday."""
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


# Sunday evening: the report covers Mon 5 Oct 00:00 until now.
NOW = local(11, 20)
WEEK_START = local(5, 0)

# --- When it's sent ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sun 20:00", (6, 20, 0)),
        ("Monday 6:50", (0, 6, 50)),
        ("  SAT   21.45 ", (5, 21, 45)),
        ("off", None),
        ("", None),
    ],
)
def test_report_time(text: str, expected: tuple[int, int, int] | None) -> None:
    assert settings.report_clock(text) == expected


@pytest.mark.parametrize(
    "text", ["20:00", "sun", "su 20:00", "someday 20:00", "sun 25:00", "sun ２０:００"]
)
def test_bad_report_time_fails_fast(text: str) -> None:
    """Reported with the other config problems at startup."""
    with pytest.raises(settings.ConfigError, match="WEEKLY_REPORT"):
        _ = settings.load_settings({**os.environ, "WEEKLY_REPORT": text})


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (local(8, 14), local(11, 20)),  # Thursday: this Sunday
        (local(11, 19), local(11, 20)),  # Sunday, before the time: today
        (local(11, 20), local(18, 20)),  # exactly then: next week
        (local(12, 9), local(18, 20)),
    ],
)
def test_next_weekly(now: float, expected: float) -> None:
    assert weekly.next_weekly(6, 20, 0, now) == expected


def test_report_is_sent_at_the_set_time_and_survives_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[int, int, int]] = []
    sends: list[int] = []

    def due_now(day: int, hour: int, minute: int, now: float) -> float:
        asked.append((day, hour, minute))
        return now

    async def send(_app: object) -> None:
        sends.append(1)
        if len(sends) == 1:
            raise RuntimeError("boom")
        if len(sends) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(weekly, "REPORT_AT", (6, 20, 0))
    monkeypatch.setattr(weekly, "next_weekly", due_now)
    monkeypatch.setattr(weekly, "send_report", send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(weekly.weekly(MagicMock()))
    assert len(sends) == 3
    assert set(asked) == {(6, 20, 0)}


def test_report_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weekly, "REPORT_AT", None)
    app, _ = fake_app()
    asyncio.run(weekly.weekly(app))  # returns at once instead of waiting forever
    assert sent_texts(app) == []


@pytest.mark.usefixtures("lab")
def test_send_report_retries_until_it_gets_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from candygoogy import digest

    app, _ = fake_app()
    app.bot.send_message.side_effect = [TelegramError("offline"), None]
    monkeypatch.setattr(digest, "RETRY_SECONDS", 0)
    asyncio.run(weekly.send_report(app))
    assert app.bot.send_message.await_count == 2
    assert app.bot.send_message.call_args.args[1].startswith("📅 <b>Weekly report</b>")


# --- The report ------------------------------------------------------------------------


def snapshot(lab: FakeHomelab, name: str, finished: float | None) -> None:
    folder = lab.backups / name
    folder.mkdir()
    _ = (folder / "dump.tar.gz").write_bytes(b"x" * 1024)
    if finished is not None:
        marker = folder / homelab.COMPLETE_MARKER
        marker.touch()
        os.utime(marker, (finished, finished))


@pytest.fixture
def week_lab(lab: FakeHomelab) -> FakeHomelab:
    """A homelab up for 10 days, with no backups yet (the test adds them)."""
    shutil.rmtree(lab.backups)
    lab.backups.mkdir()
    _ = (lab.proc / "uptime").write_text("900000.00 1000.00\n")  # 10d 10h
    return lab


def report_lines() -> list[str]:
    return weekly.week_message(NOW).splitlines()


def test_a_quiet_week(week_lab: FakeHomelab) -> None:
    for day in range(5, 12):
        snapshot(week_lab, f"202610{day:02}-021502", local(day, 2, 20))
    pctime._states[config.FIRST_MACHINE.name] = pctime.State(since=local(1, 0))  # pyright: ignore[reportPrivateUsage]
    assert report_lines() == [
        "📅 <b>Weekly report</b>",
        "<i>Mon 5 Oct – Sun 11 Oct</i>",
        "🌐 Internet: no outages.",
        "💾 7 backups completed, the last 17h 40m ago (0.0 MB).",
        "🖥 Homelab: up 10d 10h, no problems.",
        "💻 The PC wasn't on.",
        "<i>Details: /net · /homelab · /status</i>",
    ]


def test_a_busy_week(week_lab: FakeHomelab) -> None:
    internet._state.outages = [  # pyright: ignore[reportPrivateUsage]
        internet.Outage(local(4, 23, 0), local(4, 23, 30)),  # before the week: left out
        internet.Outage(local(6, 14, 2), local(6, 14, 31)),
        internet.Outage(local(7, 23, 50), local(8, 0, 20)),
    ]
    for day in (5, 6, 8, 10, 11):
        snapshot(week_lab, f"202610{day:02}-021502", local(day, 2, 20))
    snapshot(week_lab, "20261009-021502", None)
    snapshot(week_lab, "20260930-021502", None)  # before the week: left out
    _ = (week_lab.proc / "uptime").write_text(f"{15 * 3600}.00 1000.00\n")  # since Sun 05:00
    homelab._incidents[:] = [  # pyright: ignore[reportPrivateUsage]
        homelab.Incident("unit:old.service", "old", local(3, 9), local(4, 9)),
        homelab.Incident(
            "unit:example.service",
            "<code>example.service</code> failed",
            local(6, 14, 2),
            local(6, 17, 5),
        ),
        homelab.Incident("reboot", "Reboot required", local(10, 6, 30)),
    ]
    pctime._states[config.FIRST_MACHINE.name] = pctime.State(  # pyright: ignore[reportPrivateUsage]
        since=local(1, 0),
        sessions=[
            pctime.Session(local(5, 6, 50), local(5, 8, 45)),  # 2h, counting the last check's 5m
            pctime.Session(local(10, 22, 15), local(11, 1, 10)),  # 1h 45m Saturday, 1h 15m Sunday
        ],
    )
    assert report_lines()[2:-1] == [
        "🌐 Internet: online 99.4% of the time. 2 outages: 59m in total; "
        + "the longest Wed 23:50–Thu 00:20 (30m).",
        "💾 5 backups completed, the last 17h 40m ago (0.0 MB).",
        "⚠️ None completed on Wed, Fri.",
        "⚠️ Didn't finish (failed, or still running): <code>20261009-021502</code>.",
        "🖥 Homelab: rebooted Sun 05:00, 2 problems:",
        "• <code>example.service</code> failed: Tue 14:02–17:05 (3h 3m).",
        "• Reboot required: since Sat 06:30, still open.",
        "💻 PC on 5h: Mon 2h · Tue – · Wed – · Thu – · Fri – · Sat 1h 45m · Sun 1h 15m.",
    ]


@pytest.mark.usefixtures("week_lab")
def test_an_outage_from_before_the_week_and_one_still_going() -> None:
    internet._state.outages = [  # pyright: ignore[reportPrivateUsage]
        internet.Outage(local(4, 23, 50), local(5, 0, 10)),
    ]
    assert (
        "online 99.8% of the time. 1 outage: Sun 4 Oct 23:50–Mon 00:10 (20m)." in report_lines()[2]
    )
    internet._state.down_since = local(11, 19, 30)  # pyright: ignore[reportPrivateUsage]
    assert "2 outages: 40m in total; the longest Sun 19:30–20:00 (30m)." in report_lines()[2]


@pytest.mark.usefixtures("week_lab")
def test_no_backups_this_week() -> None:
    assert "⚠️ No backup completed this week." in report_lines()


def test_unreadable_backups(week_lab: FakeHomelab) -> None:
    shutil.rmtree(week_lab.backups)
    assert "⚠️ Couldn't read the backups folder." in report_lines()


@pytest.mark.usefixtures("week_lab")
def test_says_since_when_the_pc_is_tracked() -> None:
    assert "<i>💻 The PC's on-time isn't tracked yet.</i>" in report_lines()
    pctime._states[config.FIRST_MACHINE.name] = pctime.State(  # pyright: ignore[reportPrivateUsage]
        since=local(8, 14, 30), sessions=[pctime.Session(local(8, 14, 30), local(8, 15, 25))]
    )
    lines = report_lines()
    assert "💻 PC on 1h: Mon – · Tue – · Wed – · Thu 1h · Fri – · Sat – · Sun –." in lines
    assert "<i>The PC is tracked since Thu 14:30.</i>" in lines


@pytest.mark.usefixtures("lab")
def test_week_command_and_button() -> None:
    assert "week" in handlers.MENU_COMMANDS
    assert any(h[0] == "week" for h in handlers.HELP)
    update = make_update()
    call(handlers.week, update)
    assert replies(update)[0].startswith("📅 <b>Weekly report</b>")
    update = make_button_press("menu:week")
    call(handlers.on_button, update)
    assert replies(update)[0].startswith("📅 <b>Weekly report</b>")
    update = make_update()
    update.effective_message.text = "📅 Week"
    call(handlers.on_text, update)
    assert replies(update)[0].startswith("📅 <b>Weekly report</b>")


# --- PC on-time --------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """time.time() returns clock[0]; move it by assigning."""
    now = [local(8, 9)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def check(fake_run: FakeRun, on: bool) -> None:
    fake_run.results["ping"] = RunResult(0 if on else 1)
    asyncio.run(pctime.check())


def test_pc_sessions(fake_run: FakeRun, clock: list[float]) -> None:
    check(fake_run, on=False)
    assert pctime.tracked_since(config.FIRST_MACHINE) == local(8, 9)
    assert pctime.state_of(config.FIRST_MACHINE).sessions == []
    for minute in (0, 5, 10):
        clock[0] = local(8, 10, minute)
        check(fake_run, on=True)
    clock[0] = local(8, 10, 15)
    check(fake_run, on=False)
    clock[0] = local(8, 11)  # 50m later: a new session
    check(fake_run, on=True)
    assert pctime.state_of(config.FIRST_MACHINE).sessions == [
        pctime.Session(local(8, 10), local(8, 10, 10)),
        pctime.Session(local(8, 11), local(8, 11)),
    ]
    # Each check that found it on counts 5 minutes: 15m, then 5m.
    assert pctime.on_seconds(config.FIRST_MACHINE, local(8, 0), local(8, 23)) == 20 * 60
    assert (
        pctime.on_seconds(config.FIRST_MACHINE, local(8, 10, 5), local(8, 11, 2))
        == 10 * 60 + 2 * 60
    )


def test_pc_check_pings_first(fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    port22 = AsyncMock(return_value=True)
    monkeypatch.setattr(probes, "ssh_port_open", port22)
    fake_run.results["ping"] = RunResult(0)
    assert asyncio.run(pctime.is_on(config.FIRST_MACHINE))
    port22.assert_not_awaited()  # no SSH connection logged on the PC every 5 minutes
    fake_run.results["ping"] = RunResult(1)  # firewall drops ping
    assert asyncio.run(pctime.is_on(config.FIRST_MACHINE))
    port22.assert_awaited_once()


@pytest.mark.usefixtures("clock")
def test_pc_time_survives_a_restart(fake_run: FakeRun) -> None:
    check(fake_run, on=True)
    saved = pctime.state_of(config.FIRST_MACHINE)
    pctime._states[config.FIRST_MACHINE.name] = pctime.State()  # pyright: ignore[reportPrivateUsage]
    pctime.load_state()
    assert pctime.state_of(config.FIRST_MACHINE) == saved


@pytest.mark.usefixtures("clock")
def test_old_pc_sessions_are_forgotten(fake_run: FakeRun) -> None:
    nine_days_ago = local(8, 9) - 9 * 24 * 3600
    pctime._states[config.FIRST_MACHINE.name] = pctime.State(  # pyright: ignore[reportPrivateUsage]
        since=nine_days_ago, sessions=[pctime.Session(nine_days_ago, nine_days_ago + 3600)]
    )
    check(fake_run, on=True)
    assert pctime.state_of(config.FIRST_MACHINE).sessions == [
        pctime.Session(local(8, 9), local(8, 9))
    ]


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"since": null, "sessions": []}', '{"since": 1, "sessions": [[1]]}'],
)
def test_bad_pc_time_file_is_ignored(
    content: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _ = pctime.STATE_FILE.write_text(content)
    pctime.load_state()
    assert pctime.tracked_since(config.FIRST_MACHINE) is None
    assert "Ignoring" in caplog.text
    assert tmp_path.exists()


@pytest.mark.usefixtures("clock")
def test_pc_time_file_format(fake_run: FakeRun) -> None:
    check(fake_run, on=True)
    assert json.loads(pctime.STATE_FILE.read_text()) == {
        "workstation": {"since": local(8, 9), "sessions": [[local(8, 9), local(8, 9)]]},
    }
