# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/net and the internet outage checks."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

from candygoogy import (
    handlers,
    internet,
)
from tests.helpers import (
    AUTHORIZED_ID,
    FakeInternet,
    button_data,
    call,
    edits,
    make_button_press,
    make_update,
    replies,
)

# --- Internet outages --------------------------------------------------------------


REAL_REACHABLE = internet.reachable


def hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def test_one_failed_check_is_not_an_outage(net: FakeInternet) -> None:
    net.check(online=False)
    net.check(online=True)
    assert net.sent() == []
    assert internet._state.outages == []  # pyright: ignore[reportPrivateUsage]


def test_outage_is_reported_once_back(net: FakeInternet) -> None:
    start = net.now
    for _ in range(3):
        net.check(online=False)
    assert internet._state.down_since == start  # pyright: ignore[reportPrivateUsage]
    assert net.sent() == []  # nothing can be sent while it's down
    end = net.now
    net.check(online=True)
    net.check(online=True)
    [message] = net.sent()
    assert message == f"🌐 <b>Internet is back</b>\nIt was down {hm(start)}–{hm(end)} (3m)."
    assert net.app.bot.send_message.call_args.args[0] == AUTHORIZED_ID


def test_unsent_outage_report_is_retried(net: FakeInternet) -> None:
    net.check(online=False)
    net.check(online=False)
    net.app.bot.send_message.side_effect = TelegramError("DNS not ready yet")
    net.check(online=True)
    net.app.bot.send_message.side_effect = None
    net.check(online=True)
    net.check(online=True)
    assert net.app.bot.send_message.await_count == 2
    assert "Internet is back" in net.sent()[-1]


def test_outages_survive_a_restart(net: FakeInternet) -> None:
    net.check(online=False)
    net.check(online=False)
    internet._state = internet.State()  # pyright: ignore[reportPrivateUsage]
    internet.load_state()  # restarted mid-outage
    assert internet._state.down_since is not None  # pyright: ignore[reportPrivateUsage]
    net.check(online=True)
    assert len(net.sent()) == 1
    internet._state = internet.State()  # pyright: ignore[reportPrivateUsage]
    internet.load_state()
    [outage] = internet._state.outages  # pyright: ignore[reportPrivateUsage]
    assert outage.reported


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"outages": "x"}',
        '{"down_since": "x", "outages": []}',
        '{"outages": [[1, 2]]}',
        '{"outages": [[1, "b", true]]}',
        '{"outages": [[1, 2, "yes"]]}',
    ],
)
def test_bad_outages_file_is_ignored(caplog: pytest.LogCaptureFixture, content: str) -> None:
    _ = internet.STATE_FILE.write_text(content)
    internet.load_state()
    assert internet._state == internet.State()  # pyright: ignore[reportPrivateUsage]
    assert "Ignoring" in caplog.text


def test_old_outages_are_forgotten(net: FakeInternet) -> None:
    old = internet.Outage(net.now - 8 * 86400, net.now - 8 * 86400 + 60, reported=True)
    recent = internet.Outage(net.now - 3600, net.now - 3000, reported=True)
    internet._state.outages = [old, recent]  # pyright: ignore[reportPrivateUsage]
    net.check(online=True)
    assert internet._state.outages == [recent]  # pyright: ignore[reportPrivateUsage]


def test_clock_and_span(net: FakeInternet) -> None:
    now = net.now  # 14:00
    assert internet.clock(now - 3600, now) == hm(now - 3600)
    yesterday = time.mktime((2026, 10, 6, 23, 50, 0, 0, 0, -1))
    assert internet.clock(yesterday, now) == "yesterday 23:50"
    assert internet.clock(time.mktime((2026, 10, 5, 9, 5, 0, 0, 0, -1)), now) == "Mon 5 Oct 09:05"
    overnight = internet.Outage(yesterday, yesterday + 1200)
    assert internet.span(overnight, now) == "yesterday 23:50–00:10 (20m)"


@pytest.mark.usefixtures("fake_run")
def test_net_without_outages() -> None:
    update = make_update()
    call(handlers.net, update)
    [text] = replies(update)
    assert "✅ Online." in text
    assert "No outages in the last 7 days." in text
    assert "1.1.1.1 and 9.9.9.9" in text
    assert button_data(update) == ["net"]


def test_net_lists_outages_newest_first(net: FakeInternet) -> None:
    first = internet.Outage(net.now - 2 * 3600, net.now - 2 * 3600 + 120, reported=True)
    second = internet.Outage(net.now - 3600, net.now - 3600 + 900, reported=True)
    internet._state.outages = [first, second]  # pyright: ignore[reportPrivateUsage]
    text = asyncio.run(internet.net_message())
    assert "2 outages in the last 7 days, 17m in total:" in text
    assert text.index(internet.span(second, net.now)) < text.index(internet.span(first, net.now))


def test_net_while_checks_fail(net: FakeInternet) -> None:
    net.check(online=False)
    net.check(online=False)
    assert "⚠️ Down since 14:00 (2m)." in asyncio.run(internet.net_message())


def test_net_refresh_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "REFRESH_COOLDOWN_SECONDS", 0)
    update = make_button_press("net", message_id=9)
    call(handlers.on_button, update)
    [text] = edits(update)
    assert "Internet" in text
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert markup is handlers.NET_REFRESH


def test_reachable_tries_the_second_target_only_if_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(return_value=(MagicMock(), writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(REAL_REACHABLE())
    connect.assert_awaited_once_with("1.1.1.1", 443)
    writer.close.assert_called_once()

    connect = AsyncMock(side_effect=[OSError("unreachable"), (MagicMock(), writer)])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(REAL_REACHABLE())
    assert [c.args for c in connect.await_args_list] == [("1.1.1.1", 443), ("9.9.9.9", 443)]


def test_unreachable_when_both_targets_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = AsyncMock(side_effect=[OSError("unreachable"), TimeoutError()])
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert not asyncio.run(REAL_REACHABLE())


def test_internet_watch_keeps_going_after_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    checks: list[int] = []

    async def flaky(_app: object) -> None:
        checks.append(1)
        if len(checks) == 1:
            raise RuntimeError("boom")
        if len(checks) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(internet, "check", flaky)
    monkeypatch.setattr(internet, "CHECK_INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(internet.watch(MagicMock()))
    assert len(checks) == 3
