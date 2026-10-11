# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Several allowed users, private chats only, and who gets what is sent unasked."""

import asyncio
import datetime
import json
import logging
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.constants import ChatType
from telegram.error import TelegramError

from candygoogy import app as application
from candygoogy import auth, config, handlers, messages, timers
from candygoogy.config import Handler
from tests.conftest import ALLOWLIST_READERS
from tests.helpers import (
    AUTHORIZED_ID,
    SECOND_ID,
    STRANGER_ID,
    call,
    edits,
    fake_app,
    make_button_press,
    make_update,
    replies,
    sent_texts,
)


def recorder() -> tuple[Handler, list[object]]:
    """A guarded handler that records each update that got through."""
    seen: list[object] = []

    @auth.authorized
    async def handler(update: Update, _context: object) -> None:
        seen.append(update)

    return handler, seen


# --- Who gets through ---------------------------------------------------------------------


@pytest.mark.usefixtures("two_allowed_users")
@pytest.mark.parametrize("user_id", [AUTHORIZED_ID, SECOND_ID])
def test_each_allowed_user_in_a_private_chat_gets_through(user_id: int) -> None:
    handler, seen = recorder()
    update = make_update(user_id)
    call(handler, update)
    assert seen == [update]


@pytest.mark.usefixtures("two_allowed_users")
def test_second_allowed_user_can_run_a_command() -> None:
    update = make_update(SECOND_ID)
    call(handlers.start, update)
    assert "/wake" in replies(update)[0]


@pytest.mark.usefixtures("two_allowed_users")
@pytest.mark.parametrize(
    ("user_id", "chat_type"),
    [
        (STRANGER_ID, ChatType.PRIVATE),
        (AUTHORIZED_ID, ChatType.GROUP),
        (AUTHORIZED_ID, ChatType.SUPERGROUP),
        (AUTHORIZED_ID, ChatType.CHANNEL),
        (SECOND_ID, ChatType.GROUP),
        (AUTHORIZED_ID, None),  # no chat, e.g. an inline query
        (None, ChatType.PRIVATE),  # no user, e.g. a channel post
        (None, None),
    ],
)
def test_everything_else_is_ignored(user_id: int | None, chat_type: str | None) -> None:
    handler, seen = recorder()
    update = make_update(user_id, chat_type)
    call(handler, update)
    assert seen == []
    assert replies(update) == []


@pytest.mark.usefixtures("two_allowed_users")
def test_a_command_in_a_group_gets_no_reply() -> None:
    update = make_update(AUTHORIZED_ID, ChatType.GROUP)
    call(handlers.start, update)
    assert replies(update) == []


@pytest.mark.usefixtures("two_allowed_users")
@pytest.mark.parametrize("user_id", [AUTHORIZED_ID, SECOND_ID])
def test_button_tap_from_an_allowed_user_in_a_private_chat(user_id: int) -> None:
    handler, seen = recorder()
    press = make_button_press("menu:status", user_id=user_id)
    call(handler, press)
    assert seen == [press]


@pytest.mark.parametrize("chat_type", [ChatType.GROUP, ChatType.SUPERGROUP, None])
def test_button_tap_outside_a_private_chat_is_ignored(chat_type: str | None) -> None:
    press = make_button_press("cancel", chat_type=chat_type)
    call(handlers.on_button, press)
    assert edits(press) == []
    press.callback_query.answer.assert_not_awaited()


def real_update(user_id: int, chat_type: str) -> Update:
    """PTB's own objects, not mocks, so the chat type comparison is the real one."""
    user = User(user_id, "Alice", is_bot=False)
    chat = Chat(user_id if chat_type == ChatType.PRIVATE else -100123, chat_type)
    now = datetime.datetime.now(datetime.UTC)
    return Update(1, message=Message(1, now, chat, from_user=user, text="/status"))


@pytest.mark.parametrize(
    ("user_id", "chat_type", "allowed"),
    [
        (AUTHORIZED_ID, ChatType.PRIVATE, True),
        (AUTHORIZED_ID, ChatType.GROUP, False),
        (AUTHORIZED_ID, ChatType.SUPERGROUP, False),
        (STRANGER_ID, ChatType.PRIVATE, False),
    ],
)
def test_is_authorized_with_real_updates(user_id: int, chat_type: str, allowed: bool) -> None:
    assert auth.is_authorized(real_update(user_id, chat_type)) is allowed


def test_ignored_updates_are_logged_without_their_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    handler, _ = recorder()
    update = make_update(STRANGER_ID)
    update.effective_message.text = "secret words Zq9"
    call(handler, update)
    assert "Ignored handler from user 2222 in a private chat" in caplog.text
    assert "Zq9" not in caplog.text


def test_fixture_patches_every_module_that_reads_the_allowlist(
    two_allowed_users: tuple[int, int],
) -> None:
    """A module added later that imports AUTHORIZED_USER_IDS must be added to
    ALLOWLIST_READERS, or multi-user tests would silently test one user."""
    readers = {
        name
        for name, module in sys.modules.items()
        if name.startswith("candygoogy") and hasattr(module, "AUTHORIZED_USER_IDS")
    }
    assert readers == {m.__name__ for m in ALLOWLIST_READERS}
    for name in readers:
        assert sys.modules[name].AUTHORIZED_USER_IDS == two_allowed_users


# --- Sent unasked: alerts, summaries, the startup report ---------------------------------


def chats(app: MagicMock) -> list[int]:
    return [c.args[0] for c in app.bot.send_message.call_args_list]


@pytest.mark.usefixtures("two_allowed_users")
def test_unasked_messages_go_to_every_allowed_user() -> None:
    app, _ = fake_app()
    assert asyncio.run(messages.notify_allowed(app, "hello", "a test message"))
    assert chats(app) == [AUTHORIZED_ID, SECOND_ID]
    assert sent_texts(app) == ["hello", "hello"]


@pytest.mark.usefixtures("two_allowed_users")
def test_one_failed_delivery_still_counts_as_sent(caplog: pytest.LogCaptureFixture) -> None:
    """True, so a retrying caller doesn't send it again to whoever already has it."""
    app, _ = fake_app()
    app.bot.send_message.side_effect = [TelegramError("Forbidden: bot was blocked"), MagicMock()]
    assert asyncio.run(messages.notify_allowed(app, "hello", "a test message"))
    assert chats(app) == [AUTHORIZED_ID, SECOND_ID]  # the second still got it
    assert "Could not send a test message to allowed user #1" in caplog.text
    assert str(AUTHORIZED_ID) not in caplog.text


@pytest.mark.usefixtures("two_allowed_users")
def test_nobody_reached_is_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    app, _ = fake_app()
    app.bot.send_message.side_effect = TelegramError("offline")
    assert not asyncio.run(messages.notify_allowed(app, "hello", "a test message"))
    assert "allowed user #1" in caplog.text and "allowed user #2" in caplog.text
    assert str(SECOND_ID) not in caplog.text


@pytest.mark.usefixtures("two_allowed_users")
def test_command_menu_is_published_to_each_allowed_user(caplog: pytest.LogCaptureFixture) -> None:
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock(side_effect=[TelegramError("flood"), None])
    asyncio.run(application.publish_command_menu(app))
    scopes = [c.kwargs["scope"].chat_id for c in app.bot.set_my_commands.call_args_list]
    assert scopes == [AUTHORIZED_ID, SECOND_ID]  # the first failing didn't stop the second
    assert "command menu for allowed user #1" in caplog.text
    assert str(AUTHORIZED_ID) not in caplog.text


# --- Timers -------------------------------------------------------------------------------


def saved_timers() -> dict[str, dict[str, float]]:
    """The first machine's part of timers.json ({machine: {action: …}})."""
    return json.loads(timers.TIMERS_FILE.read_text()).get(config.FIRST_MACHINE.name, {})


def test_restore_drops_a_timer_from_a_chat_no_longer_allowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    due = time.time() + 3600
    _ = timers.TIMERS_FILE.write_text(
        json.dumps(
            {
                "wake": {"due": due, "chat_id": AUTHORIZED_ID},
                "shutdown": {"due": due, "chat_id": STRANGER_ID},  # removed from the list since
            }
        )
    )
    app, _ = fake_app()

    async def main() -> list[timers.Timer]:
        missed, _ = timers.restore_timers(app)
        for task in timers._timer_tasks.values():  # pyright: ignore[reportPrivateUsage]
            _ = task.cancel()
        return missed

    assert asyncio.run(main()) == []  # dropped, not reported as missed
    assert list(timers._timers) == [(config.FIRST_MACHINE.name, "wake")]  # pyright: ignore[reportPrivateUsage]
    assert list(saved_timers()) == ["wake"]
    app.bot.send_message.assert_not_awaited()
    assert "Dropped the saved shutdown timer" in caplog.text
    assert "audit action=shutdown event=timer_dropped" in caplog.text
    assert str(STRANGER_ID) not in caplog.text


@pytest.mark.usefixtures("two_allowed_users", "fake_run")
@pytest.mark.parametrize("without_message", [False, True])
def test_timer_set_by_the_second_user_reports_to_their_chat(without_message: bool) -> None:
    """Even when Telegram no longer sends the message along, the tapping user's chat is used."""
    due = time.time() + 3600
    press = make_button_press(f"sched:wake:{int(due)}:{int(time.time())}", user_id=SECOND_ID)
    if without_message:
        press.callback_query.message = None
    call(handlers.on_button, press)
    timer = timers._timers[(config.FIRST_MACHINE.name, "wake")]  # pyright: ignore[reportPrivateUsage]
    assert timer.chat_id == SECOND_ID
    assert saved_timers()["wake"]["chat_id"] == SECOND_ID

    app, tasks = fake_app()

    async def fire() -> None:
        await timers.fire_timer(app, timer)
        for task in tasks:
            await task

    asyncio.run(fire())
    assert set(chats(app)) == {SECOND_ID}
