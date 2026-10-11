"""Startup, the Application and the entry point."""

import asyncio
import logging
from collections import Counter
from html import escape
from typing import TextIO

from telegram import (
    BotCommand,
    BotCommandScopeChat,
)
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from candygoogy import digest, homelab, internet, pctime, weekly
from candygoogy.about import record_startup_version
from candygoogy.auth import is_authorized, is_guarded
from candygoogy.config import AUTHORIZED_USER_IDS, BOT_TOKEN, STATE_DIR, App, check_ssh_key
from candygoogy.handlers import COMMANDS, HELP, on_button, on_error, on_text
from candygoogy.history import load_last_action
from candygoogy.keyboard import LABELS
from candygoogy.logs import console_handler
from candygoogy.logs import install as install_log_buffer
from candygoogy.messages import card, notify_allowed
from candygoogy.state import load_json, save_json
from candygoogy.timers import (
    Timer,
    of,
    pending_timers,
    restore_timers,
    stop_timers,
    timer_line,
    when_text,
)

log = logging.getLogger(__name__)


async def publish_command_menu(app: App) -> None:
    """Show the "/" command menu in each allowed user's chat only; strangers see
    nothing. One failure doesn't stop the others."""
    commands = [BotCommand(name, text) for name, _, text in HELP]
    for n, user_id in enumerate(AUTHORIZED_USER_IDS, start=1):
        try:
            _ = await app.bot.set_my_commands(commands, scope=BotCommandScopeChat(user_id))
        except Exception:
            # By position, not ID, so the log doesn't list who may use the bot.
            log.warning("Could not publish the command menu for allowed user #%d", n, exc_info=True)


# getUpdates returns at most 100 per call; this bounds the loop if messages
# keep arriving while it drains.
MAX_BACKLOG_BATCHES = 20


async def skip_backlog(app: App) -> list[str]:
    """Mark everything sent while the bot was offline as read, without running it.

    Otherwise Telegram delivers the whole backlog at once and e.g. five stale
    /wake commands run in a row. Returns what allowed users sent meanwhile.
    """
    ignored: list[str] = []
    offset: int | None = None
    try:
        for _ in range(MAX_BACKLOG_BATCHES):
            # Asking for offset=N confirms every update below N.
            updates = await app.bot.get_updates(offset=offset, timeout=0)
            if not updates:
                break
            for u in updates:
                if not is_authorized(u):
                    continue
                if u.message and u.message.text and u.message.text.startswith("/"):
                    ignored.append(u.message.text.split()[0].split("@")[0])
                elif u.callback_query:
                    ignored.append("button tap")
            offset = updates[-1].update_id + 1
        else:  # still more after MAX_BACKLOG_BATCHES: confirm the last batch read
            _ = await app.bot.get_updates(offset=offset, timeout=0, limit=1)
    except TelegramError:
        log.warning("Could not skip the backlog; it will be handled normally", exc_info=True)
    if ignored:
        log.info("Skipped %d command(s) sent while offline", len(ignored))
    return ignored


def startup_report(
    ignored: list[str],
    missed: list[Timer],
    rebooted: str | None = None,
    dropped: list[Timer] | None = None,
) -> str | None:
    lines: list[str] = [rebooted] if rebooted else []
    if ignored:
        counts = Counter(ignored)
        listed = ", ".join(f"{escape(c)} ×{n}" if n > 1 else escape(c) for c, n in counts.items())
        lines.append(
            f"Ignored what you sent while I was offline: {listed}. Send it again if needed."
        )
    for t in missed:
        lines.append(
            f"⚠️ Missed the {t.action} timer{of(t.machine)} for {when_text(t.due)} "
            + "while I was offline. Nothing was done."
        )
    for t in dropped or []:
        lines.append(
            f"⚠️ Dropped the {t.action} timer of {escape(t.machine)}: that machine isn't configured any more."
        )
    if not lines:
        return None
    lines.extend(timer_line(t) for t in pending_timers())
    return card("🟢", "Back online", *lines)


# The keyboard layout last put under the allowed users' message box.
KEYBOARD_FILE = STATE_DIR / "keyboard.json"


async def show_keyboard(app: App, already_shown: bool) -> None:
    """Send the keyboard once per layout, so a new or changed one appears
    without /start. Afterwards alerts and the daily summary keep it there."""
    if load_json(KEYBOARD_FILE, "keyboard") == LABELS:
        return
    if already_shown or await notify_allowed(
        app,
        card("⌨️", "Buttons", "They stay below the message box. If they're ever gone, send /start."),
        "the keyboard",
    ):  # otherwise it's tried again at the next start
        _ = save_json(KEYBOARD_FILE, LABELS, "the keyboard layout")


# Started with the bot and cancelled when it stops.
BACKGROUND = (
    homelab.watch,
    internet.watch,
    pctime.watch,
    digest.daily,
    weekly.weekly,
)
_background: list[asyncio.Task[None]] = []


async def on_startup(app: App) -> None:
    await record_startup_version()  # for /about: the commit this process loaded
    load_last_action()  # for /status
    await publish_command_menu(app)
    ignored = await skip_backlog(app)
    missed, dropped = restore_timers(app)
    loop = asyncio.get_running_loop()
    _background[:] = [loop.create_task(run(app)) for run in BACKGROUND]
    report = startup_report(ignored, missed, homelab.reboot_outcome(), dropped)
    reported = report is not None and await notify_allowed(app, report, "the startup report")
    await show_keyboard(app, already_shown=reported)


async def on_stop(app: App) -> None:
    await stop_timers(app)
    for task in _background:
        _ = task.cancel()


def build_app() -> App:
    handlers = {**COMMANDS, "<buttons>": on_button, "<text>": on_text}
    unguarded = [name for name, h in handlers.items() if not is_guarded(h)]
    if unguarded:
        raise RuntimeError(f"Handlers missing @authorized: {unguarded}")
    # PTB's post_init() signature has unparameterised generics; nothing to fix on our side.
    builder = ApplicationBuilder().token(BOT_TOKEN)
    builder = builder.post_init(on_startup).post_stop(on_stop)  # pyright: ignore[reportUnknownMemberType]
    app = builder.build()
    for name, handler in COMMANDS.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


# Keep retrying the first contact with Telegram instead of exiting: after a
# homelab reboot the internet or DNS may lag network-online.target, and systemd
# stops restarting a unit that fails 5 times in 5 minutes. Config mistakes still
# fail fast: a missing variable crashes at import, and PTB doesn't retry a
# rejected token.
BOOTSTRAP_RETRIES = -1  # forever
# How long each getUpdates call waits for a message (Telegram's maximum). New
# messages still arrive at once; a longer wait only means fewer empty round
# trips, which matters on a metered connection. Stopping the bot cancels a wait
# at once.
POLL_TIMEOUT_SECONDS = 50


def configure_logging(stream: TextIO | None = None) -> logging.Handler:
    """Log at INFO to stderr (the journal) and to the /logs buffer, both with the
    bot token redacted. Returns the stderr handler."""
    root = logging.getLogger()
    console = console_handler(stream)
    root.addHandler(console)
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and those URLs contain the bot token.
    # Redacted anyway, but there's no need for a line per request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    install_log_buffer()  # for /logs
    return console


def main() -> None:
    _ = configure_logging()
    _ = check_ssh_key()  # only warns: the rest of the bot works without SSH
    app = build_app()
    log.info("Bot running...")
    app.run_polling(bootstrap_retries=BOOTSTRAP_RETRIES, timeout=POLL_TIMEOUT_SECONDS)
