"""/logs: the bot's recent log lines, kept in memory since the last start.

Reading the system journal would need extra privileges for the service user,
so instead a logging handler keeps the last LOG_BUFFER_LINES records. Older
history, including before a restart, is in `journalctl -u candygoogy-bot`.
"""

import io
import logging
import time
from collections import deque
from dataclasses import dataclass
from html import escape
from typing import TextIO

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import ContextTypes

from candygoogy.auth import authorized
from candygoogy.config import BOT_TOKEN, STARTED_AT
from candygoogy.messages import card, edit, reply

log = logging.getLogger(__name__)

LOG_BUFFER_LINES = 1000
# Lines shown in a chat message; fewer if they don't fit.
LOG_CHAT_LINES = 30
# Room for the <pre> block, leaving the rest of Telegram's 4096 for the header.
LOG_CHAT_CHARS = 3500
LOG_VIEWS = {
    "all": "All",
    "audit": "Audit",
    "errors": "Warnings & errors",
}


@dataclass(frozen=True)
class Entry:
    at: float  # record.created, wall clock
    level: int
    logger: str  # without the "candygoogy." prefix
    message: str  # one or more lines, token masked
    traceback: str  # "" unless the record carried an exception

    def line(self) -> str:
        """One compact line for chat: time, level, source, first line of the message."""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        level = logging.getLevelName(self.level)[0]  # I, W, E, C
        first = self.message.splitlines()[0] if self.message else ""
        if self.logger == "audit":
            first = first.removeprefix("audit ")  # "audit: audit action=..." says it twice
        if self.traceback:
            first += f" [{self.traceback.strip().splitlines()[-1]}]"
        return f"{stamp} {level} {self.logger}: {first}"

    def full(self) -> str:
        """Everything, for the downloadable file."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at))
        text = f"{stamp} {logging.getLevelName(self.level)} {self.logger}: {self.message}"
        return f"{text}\n{self.traceback}" if self.traceback else text


# The bot token in every form it can appear in: as is (e.g. inside a failed
# request URL), URL-encoded, and the secret part after the colon on its own.
_BOT_ID, _, _SECRET = BOT_TOKEN.partition(":")
_TOKEN_FORMS = [
    form
    for form in (BOT_TOKEN, f"{_BOT_ID}%3A{_SECRET}", f"{_BOT_ID}%3a{_SECRET}", _SECRET)
    if form
]


def redact(text: str) -> str:
    """Never keep the bot token: used by every log handler the bot installs."""
    for form in _TOKEN_FORMS:
        text = text.replace(form, "<BOT_TOKEN>")
    return text


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then redacts the whole line: message, args and traceback."""

    # typing.override is 3.12+, and the code still supports 3.11.
    def format(self, record: logging.LogRecord) -> str:  # pyright: ignore[reportImplicitOverride]
        return redact(super().format(record))


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def console_handler(stream: TextIO | None = None) -> logging.Handler:
    """stderr (the journal under systemd), with the token redacted."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    return handler


class BufferHandler(logging.Handler):
    def __init__(self, capacity: int = LOG_BUFFER_LINES) -> None:
        super().__init__()
        self.entries: deque[Entry] = deque(maxlen=capacity)

    # typing.override is 3.12+, and the code still supports 3.11.
    def emit(self, record: logging.LogRecord) -> None:  # pyright: ignore[reportImplicitOverride]
        try:
            traceback = ""
            if record.exc_info:
                traceback = logging.Formatter().formatException(record.exc_info)
            name = record.name.removeprefix("candygoogy.")
            self.entries.append(
                Entry(
                    record.created,
                    record.levelno,
                    name,
                    redact(record.getMessage()),
                    redact(traceback),
                )
            )
        except (TypeError, ValueError):  # bad format args: never break the bot over a log
            self.handleError(record)


BUFFER = BufferHandler()


def install() -> None:
    """Attach the buffer to the root logger, so every module's records land in it."""
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)


def matching(view: str) -> list[Entry]:
    entries = list(BUFFER.entries)
    if view == "audit":
        return [e for e in entries if e.logger == "audit"]
    if view == "errors":
        return [e for e in entries if e.level >= logging.WARNING]
    return entries


def logs_message(view: str) -> tuple[str, InlineKeyboardMarkup]:
    entries = matching(view)
    since = time.strftime("%d %b %H:%M", time.localtime(STARTED_AT))
    shown: list[str] = []
    size = 0
    for entry in reversed(entries[-LOG_CHAT_LINES:]):  # newest first, until full
        line = escape(entry.line())
        if size + len(line) + 1 > LOG_CHAT_CHARS:
            break
        shown.append(line)
        size += len(line) + 1
    shown.reverse()
    if shown:
        body = f"<pre>{chr(10).join(shown)}</pre>"
        count = f"Last {len(shown)} of {len(entries)}, since {since}."
    else:
        body = "<i>Nothing yet.</i>"
        count = f"Nothing logged since {since}."
    text = card("📜", f"Logs · {LOG_VIEWS[view]}", body, f"<i>{count} Older: journalctl.</i>")
    return text, logs_buttons(view)


def logs_buttons(view: str) -> InlineKeyboardMarkup:
    tabs = [
        InlineKeyboardButton(("• " if name == view else "") + label, callback_data=f"logs:{name}")
        for name, label in (("all", "All"), ("audit", "Audit"), ("errors", "Errors"))
    ]
    return InlineKeyboardMarkup(
        [tabs, [InlineKeyboardButton("📄 File", callback_data="logs:file")]]
    )


def logs_file() -> InputFile:
    text = "\n".join(e.full() for e in BUFFER.entries) or "(nothing logged yet)"
    name = time.strftime("candygoogy-%Y%m%d-%H%M.log")
    return InputFile(io.BytesIO(text.encode()), filename=name)


@authorized
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args or ["all"])[0].lower()
    view = arg if arg in LOG_VIEWS else "all"
    await reply(update, *logs_message(view))


async def on_logs_button(query: CallbackQuery, view: str) -> None:
    """Switch the view in place, or send everything as a file. Safe to repeat."""
    if view == "file":
        _ = await query.answer("Sending…")
        if query.message is not None:
            _ = await query.message.chat.send_document(
                logs_file(), caption="📄 Everything kept since the last start."
            )
        return
    if view not in LOG_VIEWS:
        _ = await query.answer()
        return
    _ = await query.answer()
    text, markup = logs_message(view)
    await edit(query, text, markup)
