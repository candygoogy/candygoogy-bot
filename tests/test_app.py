# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Startup: skipping the backlog, and showing the keyboard."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TelegramError

from candygoogy import app as application
from candygoogy import (
    keyboard,
    timers,
)
from tests.helpers import (
    AUTHORIZED_ID,
    STRANGER_ID,
    fake_app,
    local,
    make_update,
    sent_texts,
)

# --- skipping the backlog at startup -----------------------------------------


def pending_update(
    update_id: int, text: str | None = None, user_id: int = AUTHORIZED_ID
) -> MagicMock:
    u = make_update(user_id)
    u.update_id = update_id
    if text is None:
        u.message = None
    else:
        u.message.text = text
        u.callback_query = None
    return u


def test_skip_backlog_confirms_and_reports() -> None:
    app, _ = fake_app()
    batch = [
        pending_update(10, "/wake"),
        pending_update(11, "/status@example_bot"),
        pending_update(12, "/status"),
        pending_update(13, "/shutdown", user_id=STRANGER_ID),
        pending_update(14, "hello"),
        pending_update(15),  # a button tap
    ]
    app.bot.get_updates = AsyncMock(side_effect=[batch, []])
    ignored = asyncio.run(application.skip_backlog(app))
    assert ignored == ["/wake", "/status", "/status", "button tap"]
    offsets = [c.kwargs["offset"] for c in app.bot.get_updates.call_args_list]
    assert offsets == [None, 16]


def test_skip_backlog_confirms_last_batch_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "MAX_BACKLOG_BATCHES", 1)
    app, _ = fake_app()
    app.bot.get_updates = AsyncMock(side_effect=[[pending_update(7, "/wake")], []])
    _ = asyncio.run(application.skip_backlog(app))
    assert app.bot.get_updates.call_args.kwargs["offset"] == 8


def test_skip_backlog_failure_is_not_fatal() -> None:
    app, _ = fake_app()
    app.bot.get_updates = AsyncMock(side_effect=TelegramError("conflict"))
    assert asyncio.run(application.skip_backlog(app)) == []


def test_startup_report() -> None:
    assert application.startup_report([], []) is None
    text = application.startup_report(["/status", "/wake", "/status"], [])
    assert text is not None
    assert "/status ×2, /wake" in text
    missed = timers.Timer("shutdown", local(21, 45), AUTHORIZED_ID)
    text = application.startup_report([], [missed])
    assert text is not None
    assert "Missed the shutdown timer" in text


@pytest.mark.usefixtures("fake_run")
def test_on_startup_reports_only_when_needed() -> None:
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(return_value=[])
    asyncio.run(application.on_startup(app))
    app.bot.send_message.assert_not_awaited()

    app.bot.get_updates = AsyncMock(side_effect=[[pending_update(1, "/wake")], []])
    asyncio.run(application.on_startup(app))
    [text] = sent_texts(app)
    assert "Back online" in text
    assert app.bot.send_message.call_args.args[0] == AUTHORIZED_ID
    assert app.bot.send_message.call_args.kwargs["reply_markup"] is keyboard.KEYBOARD


# --- the keyboard appears without /start ---------------------------------------


def start_bot(app: MagicMock, backlog: list[MagicMock] | None = None) -> None:
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(side_effect=[backlog or [], []])
    asyncio.run(application.on_startup(app))


@pytest.mark.parametrize("saved", [None, json.dumps(["⚡ Wake", "📊 Status"]), "not json"])
@pytest.mark.usefixtures("fake_run")
def test_new_or_changed_keyboard_is_sent_once(
    keyboard_already_shown: Path, saved: str | None
) -> None:
    if saved is None:
        keyboard_already_shown.unlink()  # first start with a keyboard
    else:
        _ = keyboard_already_shown.write_text(saved)  # an older layout
    app, _ = fake_app()
    start_bot(app)
    [text] = sent_texts(app)
    assert "Buttons" in text
    assert app.bot.send_message.call_args.kwargs["reply_markup"] is keyboard.KEYBOARD
    assert json.loads(keyboard_already_shown.read_text()) == keyboard.LABELS
    start_bot(app)  # restarted: not again
    assert len(sent_texts(app)) == 1


@pytest.mark.usefixtures("fake_run")
def test_keyboard_comes_with_the_startup_report(keyboard_already_shown: Path) -> None:
    keyboard_already_shown.unlink()
    app, _ = fake_app()
    start_bot(app, [pending_update(1, "/wake")])
    [text] = sent_texts(app)  # no separate "Buttons" message
    assert "Back online" in text
    assert app.bot.send_message.call_args.kwargs["reply_markup"] is keyboard.KEYBOARD
    assert json.loads(keyboard_already_shown.read_text()) == keyboard.LABELS


@pytest.mark.usefixtures("fake_run")
def test_unsent_keyboard_is_tried_at_the_next_start(keyboard_already_shown: Path) -> None:
    keyboard_already_shown.unlink()
    app, _ = fake_app()
    app.bot.send_message.side_effect = TelegramError("offline")
    start_bot(app)
    assert not keyboard_already_shown.exists()
    app.bot.send_message.side_effect = None
    start_bot(app)
    assert "Buttons" in sent_texts(app)[-1]
