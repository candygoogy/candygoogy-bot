"""Telegram HTML replies and the helpers that send, edit and follow them up."""

import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any, cast

from telegram import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ContextTypes,
)

from candygoogy.config import AUTHORIZED_USER_IDS, App
from candygoogy.keyboard import KEYBOARD

log = logging.getLogger(__name__)

# --- Messages -----------------------------------------------------------------
# All replies are Telegram HTML. Anything dynamic must go through escape().

MAX_DETAIL_CHARS = 1500  # keeps every message well under Telegram's 4096 limit


def card(icon: str, title: str, *lines: str) -> str:
    """A message with an emoji + bold headline and optional detail lines."""
    return "\n".join([f"{icon} <b>{title}</b>", *lines])


def clip(text: str) -> str:
    text = text.strip()
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + "\n...(truncated)"
    return text


# Buttons under one message, or the keyboard /start shows under the message box.
Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup


async def send(update: Update, text: str, markup: Markup | None = None) -> Message | None:
    """Reply and return the sent message, so it can be edited later."""
    if update.effective_message is None:
        return None
    return await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup
    )


async def reply(update: Update, text: str, markup: Markup | None = None) -> None:
    _ = await send(update, text, markup)


def not_modified(e: BadRequest) -> bool:
    """Telegram rejects edits that change nothing; the message is already right."""
    return "message is not modified" in str(e).lower()


async def edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        _ = await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if not not_modified(e):
            raise


# Rewrites one message in place; used by follow-ups that report progress.
TextSetter = Callable[[str], Coroutine[Any, Any, None]]


def message_setter(message: Message) -> TextSetter:
    async def set_text(text: str) -> None:
        try:
            _ = await message.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if not not_modified(e):
                log.warning("Could not update message: %s", e)
        except TelegramError as e:  # e.g. the message was deleted meanwhile
            log.warning("Could not update message: %s", e)

    return set_text


def query_setter(query: CallbackQuery) -> TextSetter:
    async def set_text(text: str) -> None:
        try:
            await edit(query, text)
        except TelegramError as e:
            log.warning("Could not update message: %s", e)

    return set_text


# Sends a new message and returns it (or None if there was nowhere to send it).
Post = Callable[[str], Coroutine[Any, Any, Message | None]]
# Starts a follow-up without waiting for it.
Spawn = Callable[[Coroutine[Any, Any, None]], None]


def update_poster(update: Update) -> Post:
    async def post(text: str) -> Message | None:
        return await send(update, text)

    return post


def chat_poster(app: App, chat_id: int) -> Post:
    """For messages nobody asked for right now, e.g. a timer going off."""

    async def post(text: str) -> Message | None:
        return await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    return post


async def notify_allowed(
    app: App, text: str, what: str, buttons: InlineKeyboardMarkup | None = None
) -> bool:
    """Send something unasked (an alert, the daily summary) to every allowed
    user. True if at least one got it; False if nobody did, so the caller can
    try again later without resending to those who already have it. Unless it
    has buttons of its own, it carries the keyboard, which keeps that under the
    message box (these messages are never edited)."""
    delivered = False
    for n, user_id in enumerate(AUTHORIZED_USER_IDS, start=1):
        try:
            _ = await app.bot.send_message(
                user_id, text, parse_mode=ParseMode.HTML, reply_markup=buttons or KEYBOARD
            )
        except TelegramError as e:
            # By position, not ID, so the log doesn't list who may use the bot.
            log.warning("Could not send %s to allowed user #%d: %s", what, n, e)
        else:
            delivered = True
    return delivered


def in_background(
    context: ContextTypes.DEFAULT_TYPE, coro: Coroutine[Any, Any, None], update: Update
) -> None:
    """Run `coro` without blocking the handler; its errors reach on_error."""
    app = cast(App, context.application)
    _ = app.create_task(coro, update=update)


def background_spawner(context: ContextTypes.DEFAULT_TYPE, update: Update) -> Spawn:
    def spawn(coro: Coroutine[Any, Any, None]) -> None:
        in_background(context, coro, update)

    return spawn


def app_spawner(app: App) -> Spawn:
    def spawn(coro: Coroutine[Any, Any, None]) -> None:
        _ = app.create_task(coro)

    return spawn


async def typing(update: Update) -> None:
    """Show "typing..." while a slow probe runs."""
    if update.effective_chat is not None:
        with contextlib.suppress(Exception):  # purely cosmetic
            _ = await update.effective_chat.send_action(ChatAction.TYPING)


def duration(seconds: float) -> str:
    """Human-friendly duration: "8s", "1m 23s", "3m", "2h 5m", "3d 4h"."""
    total = max(0, round(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    if mins:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    return f"{secs}s"
