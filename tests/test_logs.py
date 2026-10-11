# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/logs."""

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from candygoogy import (
    config,
    handlers,
    logs,
)
from tests.helpers import (
    button_data,
    call,
    call_with_args,
    edits,
    make_button_press,
    make_update,
    replies,
)

# --- /logs -------------------------------------------------------------------


@pytest.fixture
def buffered(caplog: pytest.LogCaptureFixture) -> Callable[..., None]:
    """Route log records into an empty /logs buffer; returns a helper to log lines."""
    caplog.set_level(logging.INFO)
    logs.BUFFER.entries.clear()
    logs.install()

    def emit(level: int, message: str, logger: str = "candygoogy.power") -> None:
        logging.getLogger(logger).log(level, message)

    return emit


def file_text(document: Any) -> str:
    content = document.input_file_content
    assert isinstance(content, bytes)
    return content.decode()


def logs_reply(*args: str) -> tuple[str, list[str]]:
    update = make_update()
    call_with_args(logs.logs, update, *args)
    [text] = replies(update)
    return text, button_data(update)


def test_install_is_idempotent() -> None:
    logs.install()
    logs.install()
    assert logging.getLogger().handlers.count(logs.BUFFER) == 1


def test_logs_shows_recent_lines(buffered: Callable[..., None]) -> None:
    buffered(logging.INFO, "first")
    buffered(logging.WARNING, "second", "candygoogy.timers")
    text, buttons = logs_reply()
    assert "Logs · All" in text
    assert text.index("I power: first") < text.index("W timers: second")
    assert "Last 2 of 2" in text
    assert buttons == ["logs:all", "logs:audit", "logs:errors", "logs:file"]


def test_logs_views_filter(buffered: Callable[..., None]) -> None:
    buffered(logging.INFO, "plain info")
    config.audit("wake", "packet_sent")
    buffered(logging.ERROR, "it broke")
    audit_text, _ = logs_reply("audit")
    assert "audit: action=wake event=packet_sent" in audit_text
    assert "plain info" not in audit_text and "it broke" not in audit_text
    errors_text, _ = logs_reply("ERRORS")
    assert "it broke" in errors_text
    assert "plain info" not in errors_text and "packet_sent" not in errors_text
    unknown_text, _ = logs_reply("bogus")
    assert "Logs · All" in unknown_text


@pytest.mark.usefixtures("buffered")
def test_logs_when_empty() -> None:
    text, _ = logs_reply()
    assert "Nothing yet" in text


def test_logs_mask_the_token_and_escape_html(buffered: Callable[..., None]) -> None:
    buffered(logging.WARNING, f"GET https://api.telegram.org/bot{config.BOT_TOKEN}/getMe <b>x</b>")
    text, _ = logs_reply()
    assert config.BOT_TOKEN not in text
    assert "&lt;BOT_TOKEN&gt;" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text


@pytest.mark.usefixtures("buffered")
def test_logs_show_exception_summary_and_file_has_traceback() -> None:
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        logging.getLogger("candygoogy.timers").exception("Timer for wake failed")
    text, _ = logs_reply()
    assert "Timer for wake failed [RuntimeError: kaboom]" in text
    assert "Traceback" not in text
    file = logs.logs_file()
    assert "Traceback (most recent call last)" in file_text(file)


def test_logs_fit_in_one_telegram_message(buffered: Callable[..., None]) -> None:
    for i in range(200):
        buffered(logging.INFO, f"{i:03d} " + "x" * 300)
    text, _ = logs_reply()
    assert len(text) < 4096
    assert "199 " in text  # newest kept
    assert "000 " not in text


def test_logs_buffer_is_bounded() -> None:
    handler = logs.BufferHandler(capacity=3)
    for i in range(5):
        _ = handler.handle(
            logging.LogRecord("candygoogy.x", logging.INFO, "", 0, str(i), None, None)
        )
    assert [e.message for e in handler.entries] == ["2", "3", "4"]


def test_bad_log_call_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    handled: list[logging.LogRecord] = []
    handler = logs.BufferHandler()
    monkeypatch.setattr(handler, "handleError", handled.append)
    record = logging.LogRecord("candygoogy.x", logging.INFO, "", 0, "%d", ("nan",), None)
    _ = handler.handle(record)
    assert handled == [record]
    assert not handler.entries


@pytest.mark.usefixtures("buffered")
def test_logs_button_switches_view_in_place() -> None:
    config.audit("sleep", "confirmed")
    update = make_button_press("logs:audit", message_id=3)
    call(handlers.on_button, update)
    call(handlers.on_button, make_button_press("logs:audit", message_id=3))  # repeatable
    [text] = edits(update)
    assert "Logs · Audit" in text
    assert "event=confirmed" in text


def test_logs_file_button_sends_document(buffered: Callable[..., None]) -> None:
    buffered(logging.INFO, "hello file")
    update = make_button_press("logs:file")
    update.callback_query.message.chat.send_document = AsyncMock()
    call(handlers.on_button, update)
    document = update.callback_query.message.chat.send_document.call_args.args[0]
    assert document.filename.startswith("candygoogy-")
    assert "hello file" in file_text(document)


def test_logs_unknown_button_view_is_ignored() -> None:
    update = make_button_press("logs:secrets")
    call(handlers.on_button, update)
    assert edits(update) == []
