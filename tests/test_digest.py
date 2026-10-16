# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/summary and the daily summary."""

import asyncio
import os
import time
from unittest.mock import MagicMock

import pytest
from telegram.error import TelegramError

from candygoogy import (
    config,
    digest,
    handlers,
    internet,
    schedules,
    settings,
    timers,
)
from tests.helpers import (
    AUTHORIZED_ID,
    FakeHomelab,
    FakeRun,
    call,
    fake_app,
    make_button_press,
    make_update,
    replies,
    sent_texts,
    set_timer,
)

# --- Daily summary ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("08:00", (8, 0)), (" 6:50 ", (6, 50)), ("21.45", (21, 45)), ("off", None), ("", None)],
)
def test_digest_time(text: str, expected: tuple[int, int] | None) -> None:
    assert settings.digest_clock(text) == expected


@pytest.mark.parametrize("text", ["8am", "25:00", "08:00:00", "０８:００", "08:0٠"])
def test_bad_digest_time_fails_fast(text: str) -> None:
    """Reported with the other config problems at startup."""
    with pytest.raises(settings.ConfigError, match="DIGEST_TIME"):
        _ = settings.load_settings({**os.environ, "DIGEST_TIME": text})


def summary_text() -> str:
    update = make_update()
    call(handlers.summary, update)
    [text] = replies(update)
    return text


@pytest.mark.usefixtures("lab")
def test_summary_when_all_is_well() -> None:
    text = summary_text()
    assert text.startswith("🗓 <b>Daily summary</b>")
    assert "🖥 Homelab: all good." in text
    assert "💾 Last backup 7h ago (10 MB)." in text
    assert "📦" not in text  # nothing to update
    assert "🌐 No internet outages in the last 24h." in text
    assert "⚫ The PC is off or asleep." in text


def test_summary_lists_problems_outages_updates_and_timers(
    lab: FakeHomelab, workstation_on: FakeRun
) -> None:
    lab.reboot_required("linux-image-amd64")
    lab.failed("example.service")
    lab.updates("example-pkg")
    now = time.time()
    outage = internet.Outage(now - 3600, now - 3000, reported=True)
    internet._state.outages = [outage]  # pyright: ignore[reportPrivateUsage]
    _ = set_timer("wake", now + 3600)
    _ = set_timer("shutdown", now + 2 * 86400)  # not today's business
    text = summary_text()
    assert "🖥 Homelab: 2 problems." in text
    assert "⚠️ <code>example.service</code> failed." in text
    assert "⚠️ Reboot required." in text
    assert "📦 1 update available: example-pkg." in text
    assert f"🌐 1 internet outage in the last 24h: {internet.span(outage, now)}." in text
    assert "🟢 The PC is on." in text
    assert "⚡ Wake at" in text
    assert "Shutdown" not in text
    assert workstation_on.calls  # read-only probes only
    assert all(c[0] in ("ping", "systemctl", "docker", "apt") for c in workstation_on.calls)


@pytest.mark.usefixtures("lab", "fake_run")
def test_summary_lists_todays_repeating_runs() -> None:
    first = config.FIRST_MACHINE.name
    in_three_days = (schedules.local_date(time.time()).weekday() + 3) % 7
    for s in (
        schedules.Schedule("aaaaaa", first, "wake", schedules.EVERY_DAY, (7, 0), AUTHORIZED_ID),
        schedules.Schedule("bbbbbb", first, "shutdown", (in_three_days,), (23, 0), AUTHORIZED_ID),
    ):
        schedules._schedules[s.id] = s  # pyright: ignore[reportPrivateUsage]
    text = summary_text()
    assert "🔁 Wake · daily 07:00 · next" in text
    assert "Shutdown" not in text  # not today's business


@pytest.mark.usefixtures("lab")
def test_summary_is_in_help_and_menu() -> None:
    for name in ("summary", "net"):
        assert name in handlers.MENU_COMMANDS
        assert any(name == h[0] for h in handlers.HELP)
    update = make_button_press("menu:summary")
    call(handlers.on_button, update)
    assert "Daily summary" in replies(update)[0]


@pytest.mark.usefixtures("lab")
def test_send_digest_retries_until_it_gets_through(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = fake_app()
    app.bot.send_message.side_effect = [TelegramError("offline"), TelegramError("offline"), None]
    monkeypatch.setattr(digest, "RETRY_SECONDS", 0)
    asyncio.run(digest.send_digest(app))
    assert app.bot.send_message.await_count == 3


@pytest.mark.usefixtures("lab")
def test_send_digest_gives_up(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, _ = fake_app()
    app.bot.send_message.side_effect = TelegramError("offline")
    monkeypatch.setattr(digest, "GIVE_UP_SECONDS", 0)
    asyncio.run(digest.send_digest(app))
    assert app.bot.send_message.await_count == 1
    assert "Gave up" in caplog.text


def test_daily_sends_at_the_set_time_and_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[int, int]] = []
    sends: list[int] = []

    def due_now(hour: int, minute: int, now: float) -> float:
        asked.append((hour, minute))
        return now

    async def send(_app: object) -> None:
        sends.append(1)
        if len(sends) == 1:
            raise RuntimeError("boom")
        if len(sends) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(digest, "DIGEST_AT", (8, 0))
    monkeypatch.setattr(digest, "next_at", due_now)
    monkeypatch.setattr(digest, "send_digest", send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(digest.daily(MagicMock()))
    assert len(sends) == 3
    assert set(asked) == {(8, 0)}


def test_daily_summary_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest, "DIGEST_AT", None)
    app, _ = fake_app()
    asyncio.run(digest.daily(app))  # returns at once instead of waiting forever
    assert sent_texts(app) == []


@pytest.mark.parametrize(
    ("clock", "expected"), [((13, 59), (2026, 10, 8, 8, 0)), ((7, 0), (2026, 10, 7, 8, 0))]
)
def test_next_at(clock: tuple[int, int], expected: tuple[int, int, int, int, int]) -> None:
    now = time.mktime((2026, 10, 7, *clock, 0, 0, 0, -1))
    year, month, day, hour, minute = expected
    assert timers.next_at(8, 0, now) == time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


@pytest.mark.usefixtures("lab")
def test_summary_ends_with_links_to_the_details() -> None:
    footer = summary_text().splitlines()[-1]
    assert footer == "<i>Details: /homelab · /net · /status</i>"
    for name in ("homelab", "net", "status"):
        assert name in handlers.COMMANDS
