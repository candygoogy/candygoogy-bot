"""Only AUTHORIZED_USER_IDS may use the bot, and only in a private chat with it."""

import functools
import logging
import time

from telegram import (
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    ContextTypes,
)

from candygoogy.config import AUTHORIZED_USER_IDS, Handler

log = logging.getLogger(__name__)

# --- Authorization ------------------------------------------------------------

# Handlers wrapped by @authorized; build_app() refuses to register anything else.
_AUTHORIZED_HANDLERS: set[Handler] = set()


def is_authorized(update: Update) -> bool:
    """An allowed user, in their private chat with the bot. Groups, channels and
    updates without a user or chat (e.g. inline queries) never are, so adding
    the bot to a group can't make it answer there."""
    user, chat = update.effective_user, update.effective_chat
    return (
        user is not None
        and user.id in AUTHORIZED_USER_IDS
        and chat is not None
        and chat.type == ChatType.PRIVATE
    )


# Ignored updates are logged at most once per this long per user, so a stranger
# (or a group the bot was added to) can't flood the journal and /logs.
IGNORED_LOG_SECONDS = 60.0
# Users remembered for that; past this many, the least recently logged is forgotten.
IGNORED_LOG_MAX_USERS = 1000
# The clock for that; tests move it without touching time.monotonic itself.
monotonic = time.monotonic
# User ID (None: no user) -> (when last logged, updates ignored since without a line).
# Ordered by when last logged, oldest first.
_ignored: dict[int | None, tuple[float, int]] = {}


def log_ignored(handler: Handler, update: Update) -> None:
    """One warning per user per IGNORED_LOG_SECONDS, saying how many were skipped
    since the last one. Never the message text."""
    user, chat = update.effective_user, update.effective_chat
    key = user.id if user else None
    now = monotonic()
    if (last := _ignored.get(key)) is not None and now - last[0] < IGNORED_LOG_SECONDS:
        _ignored[key] = (last[0], last[1] + 1)
        return
    skipped = last[1] if last is not None else 0
    _ = _ignored.pop(key, None)  # re-added last: the dict stays ordered by when logged
    while len(_ignored) >= IGNORED_LOG_MAX_USERS:
        del _ignored[next(iter(_ignored))]
    _ignored[key] = (now, 0)
    log.warning(
        "Ignored %s from user %s in a %s chat%s",
        handler.__name__,
        key if key is not None else "unknown",
        chat.type if chat else "missing",
        f" ({skipped} more from them since the last line)" if skipped else "",
    )


def authorized(handler: Handler) -> Handler:
    """Silently ignore updates that aren't from an allowed user in a private chat."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update):
            log_ignored(handler, update)
            return
        await handler(update, context)

    _AUTHORIZED_HANDLERS.add(wrapper)
    return wrapper


def is_guarded(handler: Handler) -> bool:
    """Whether `handler` was wrapped by @authorized."""
    return handler in _AUTHORIZED_HANDLERS
