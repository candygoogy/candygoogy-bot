# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/start, its buttons, and replies and errors."""

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReplyKeyboardMarkup

from candygoogy import app as application
from candygoogy import (
    auth,
    handlers,
    keyboard,
    messages,
    power,
    system,
)
from tests.helpers import (
    AUTHORIZED_ID,
    POWER_COMMANDS,
    STRANGER_ID,
    FakeRun,
    button_data,
    call,
    edits,
    make_button_press,
    make_update,
    replies,
)

# --- /start ----------------------------------------------------------------


def test_start_lists_commands(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.start, update)
    [text] = replies(update)
    for name in ["wake", "status", *POWER_COMMANDS]:
        assert f"/{name}" in text
    assert fake_run.calls == []


# --- replies and errors ----------------------------------------------------


def test_long_errors_are_clipped() -> None:
    text = system.RunResult(1, "e" * 10_000).describe("X")
    assert len(text) < 2000 and "(truncated)" in text


def test_error_output_is_html_escaped() -> None:
    text = system.RunResult(1, "<b>bad</b> & <script>").describe("X")
    assert "&lt;b&gt;bad&lt;/b&gt; &amp; &lt;script&gt;" in text


def test_replies_use_html() -> None:
    update = make_update()
    asyncio.run(messages.reply(update, "hi"))
    assert update.effective_message.reply_text.call_args.kwargs["parse_mode"] == "HTML"


def test_slow_commands_show_typing(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.status, update)
    update.effective_chat.send_action.assert_awaited_once_with("typing")
    assert fake_run.calls  # and then actually probed


def test_command_menu_is_scoped_to_owner() -> None:
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    asyncio.run(application.publish_command_menu(app))
    call_ = app.bot.set_my_commands.call_args
    assert call_.kwargs["scope"].chat_id == AUTHORIZED_ID
    assert [c.command for c in call_.args[0]] == [name for name, _, _ in handlers.HELP]


def test_command_menu_failure_does_not_stop_startup() -> None:
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock(side_effect=OSError("offline"))
    asyncio.run(application.publish_command_menu(app))  # must not raise


def test_error_handler_tells_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update()
    monkeypatch.setattr(handlers, "Update", MagicMock)  # make isinstance() accept the mock
    asyncio.run(handlers.on_error(update, MagicMock(error=RuntimeError("kaboom"))))
    [text] = replies(update)
    assert "RuntimeError" in text


def test_error_handler_stays_silent_for_strangers(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update(STRANGER_ID)
    monkeypatch.setattr(handlers, "Update", MagicMock)
    asyncio.run(handlers.on_error(update, MagicMock(error=RuntimeError())))
    assert replies(update) == []


def test_error_handler_survives_failing_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update()
    update.effective_message.reply_text.side_effect = OSError("network down")
    monkeypatch.setattr(handlers, "Update", MagicMock)
    asyncio.run(handlers.on_error(update, MagicMock(error=RuntimeError())))  # must not raise


# --- /start buttons -----------------------------------------------------------


def test_start_shows_the_keyboard(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.start, update)
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.is_persistent and markup.resize_keyboard
    labels = [b.text for row in markup.keyboard for b in row]
    assert labels == list(keyboard.KEYBOARD_COMMANDS)
    assert set(keyboard.KEYBOARD_COMMANDS.values()) <= set(handlers.COMMANDS)
    assert fake_run.calls == []


def tap(label: str, user_id: int | None = AUTHORIZED_ID) -> MagicMock:
    """A keyboard tap: Telegram sends the button's label as a message."""
    update = make_update(user_id)
    update.effective_message.text = label
    call(handlers.on_text, update)
    return update


def test_keyboard_status_runs_status(workstation_on: FakeRun) -> None:
    assert "Awake" in replies(tap("📊 Status"))[0]
    assert workstation_on.calls


@pytest.mark.parametrize(("name", "label"), [("shutdown", "🔌 Shut down"), ("sleep", "😴 Sleep")])
def test_keyboard_power_buttons_still_ask_first(
    workstation_on: FakeRun, name: str, label: str
) -> None:
    update = tap(label)
    assert replies(update)[0].startswith(power.POWER_ACTIONS[name].question)
    assert workstation_on.ssh_commands() in ([], ["quser"])  # only the read-only check
    assert len(button_data(update)) == 2  # Confirm / Cancel


def test_every_keyboard_button_runs_its_command(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
) -> None:
    ran: list[str] = []
    for label, name in keyboard.KEYBOARD_COMMANDS.items():

        async def record(_update: object, _context: object, name: str = name) -> None:
            ran.append(name)

        monkeypatch.setitem(handlers.COMMANDS, name, record)
        _ = tap(f" {label} ")
    assert ran == list(keyboard.KEYBOARD_COMMANDS.values())
    assert fake_run.calls == []


@pytest.mark.parametrize("text", ["Status", "📊 status", "hello", "⚡"])
def test_other_text_runs_nothing(fake_run: FakeRun, text: str) -> None:
    assert replies(tap(text)) == []
    assert fake_run.calls == []


@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_strangers_cannot_use_the_keyboard(fake_run: FakeRun, user_id: int | None) -> None:
    assert replies(tap("⚡ Wake", user_id)) == []
    assert fake_run.calls == []


def test_every_menu_command_is_a_guarded_command() -> None:
    for name in handlers.MENU_COMMANDS:
        assert auth.is_guarded(handlers.COMMANDS[name])


def test_menu_status_runs_status(workstation_on: FakeRun) -> None:
    update = make_button_press("menu:status")
    call(handlers.on_button, update)
    update.callback_query.answer.assert_awaited_once()
    assert "Awake" in replies(update)[0]
    assert workstation_on.calls


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_menu_power_buttons_still_ask_first(workstation_on: FakeRun, name: str) -> None:
    update = make_button_press(f"menu:{name}")
    call(handlers.on_button, update)
    assert replies(update)[0].startswith(power.POWER_ACTIONS[name].question)
    assert workstation_on.ssh_commands() in ([], ["quser"])  # only the read-only check
    assert len(button_data(update)) == 2  # Confirm / Cancel


def test_menu_wake_wakes(fake_run: FakeRun, awake_sequence: Callable[..., None]) -> None:
    awake_sequence(False, True)
    call(handlers.on_button, make_button_press("menu:wake"))
    assert "wol" in fake_run.programs()


def test_menu_can_be_tapped_again(workstation_on: FakeRun) -> None:
    for _ in range(2):
        update = make_button_press("menu:about", message_id=5)
        call(handlers.on_button, update)
        assert "About candygoogy" in replies(update)[0]
    assert workstation_on.calls


@pytest.mark.parametrize("data", ["menu:", "menu:nope", "menu:start", "menu:../wake", "menu:WAKE"])
def test_unknown_menu_entries_do_nothing(fake_run: FakeRun, data: str) -> None:
    update = make_button_press(data)
    call(handlers.on_button, update)
    assert replies(update) == [] and edits(update) == []
    assert fake_run.calls == []


def test_too_old_menu_says_so(fake_run: FakeRun) -> None:
    update = make_button_press("menu:wake")
    update.effective_message = None
    call(handlers.on_button, update)
    update.callback_query.answer.assert_awaited_once_with(
        "This menu is too old. Send /start again."
    )
    assert fake_run.calls == []


@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_strangers_cannot_use_the_menu(fake_run: FakeRun, user_id: int | None) -> None:
    update = make_button_press("menu:wake", user_id=user_id)
    call(handlers.on_button, update)
    assert replies(update) == []
    assert fake_run.calls == []
