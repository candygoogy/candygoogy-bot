"""Scheduled shutdown and wake, persisted across restarts."""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import cast

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
)

from candygoogy import machines, probes
from candygoogy.auth import authorized
from candygoogy.config import (
    AUTHORIZED_USER_IDS,
    CONFIRM_TTL_SECONDS,
    STATE_DIR,
    App,
    audit,
)
from candygoogy.messages import (
    app_spawner,
    card,
    chat_poster,
    duration,
    edit,
    message_setter,
    reply,
)
from candygoogy.power import POWER_ACTIONS, run_power_action, start_wake
from candygoogy.settings import CLOCK, Machine, is_number
from candygoogy.state import load_json, save_json

log = logging.getLogger(__name__)

# --- Timers -------------------------------------------------------------------
# /schedule_shutdown and /schedule_wake set one timer per machine and action. Timers are
# saved to disk so a restart (e.g. a deploy) keeps them. One that fell due
# while the bot was down is reported as missed instead of running late.

TIMER_ACTIONS = ("shutdown", "wake")
TIMER_TITLE = {"shutdown": "Shutdown", "wake": "Wake"}
TIMER_ICON = {"shutdown": "🔌", "wake": "⚡"}
TIMER_USAGE = "45m · 2h · 1h30m · 1d · 22:30 (a bare number means minutes)"
MIN_TIMER_SECONDS = 60
MAX_TIMER_SECONDS = 7 * 24 * 3600
# Before a scheduled shutdown whoever set it gets a warning with Cancel and +30m.
TIMER_WARNING_SECONDS = 5 * 60
SNOOZE_SECONDS = 30 * 60
# A timer at most this overdue at startup (a quick restart) still runs.
MISSED_GRACE_SECONDS = 120
# Longest single asyncio.sleep while waiting, so wall-clock jumps are noticed.
TIMER_TICK_SECONDS = 30.0

TIMERS_FILE = STATE_DIR / "timers.json"


def first_machine_name() -> str:
    return machines.first().name


@dataclass(frozen=True)
class Timer:
    action: str  # one of TIMER_ACTIONS
    due: float  # wall-clock time.time()
    chat_id: int
    machine: str = field(default_factory=first_machine_name)  # a configured name

    @property
    def key(self) -> tuple[str, str]:
        return self.machine, self.action


# (machine, action) -> the armed timer and its task
_timers: dict[tuple[str, str], Timer] = {}
_timer_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}


def of(machine: str) -> str:
    """ " of desk" when there are several machines; "" with one."""
    return f" of {escape(machine)}" if machines.several() else ""


# ASCII digits only, like CLOCK: other scripts' digits would reach int().
DURATION_PART = re.compile(r"([0-9]+)([dhm])")
# Longer typed text isn't a time; checked before any parsing.
MAX_WHEN_CHARS = 32


def next_at(hour: int, minute: int, now: float) -> float:
    """The next time after `now` that the local clock shows hour:minute."""
    today = time.localtime(now)
    # mktime rolls day overflow into the next month, and isdst=-1 lets it apply
    # whatever DST offset that local time has. Tomorrow is always after now.
    due = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, hour, minute, 0, 0, 0, -1))
    if due > now:
        return due
    return time.mktime((today.tm_year, today.tm_mon, today.tm_mday + 1, hour, minute, 0, 0, 0, -1))


def parse_when(text: str, now: float) -> float:
    """Due time for "45m", "1h30m", "90" (minutes) or "22:30" (next occurrence).

    Raises ValueError with a message for the user.
    """
    if len(text.strip()) > MAX_WHEN_CHARS:
        raise ValueError(f"That's too long to be a time. Try {TIMER_USAGE}")
    text = "".join(text.lower().split())
    if not text:
        raise ValueError("When? " + TIMER_USAGE)
    if match := CLOCK.fullmatch(text):
        return next_at(int(match[1]), int(match[2]), now)
    if is_number(text):
        seconds = int(text) * 60
    elif DURATION_PART.sub("", text) == "":
        units = {"d": 86400, "h": 3600, "m": 60}
        seconds = sum(int(m[1]) * units[m[2]] for m in DURATION_PART.finditer(text))
    else:
        raise ValueError(f"Couldn't read <code>{escape(text)}</code>. Try {TIMER_USAGE}")
    if not MIN_TIMER_SECONDS <= seconds <= MAX_TIMER_SECONDS:
        raise ValueError("Pick something between 1 minute and 7 days.")
    return now + seconds


def when_text(due: float, now: float | None = None) -> str:
    """ "22:30", "tomorrow 06:45" or "Mon 5 Oct 06:45", in the homelab's local time."""
    now = time.time() if now is None else now
    at, today = time.localtime(due), time.localtime(now)
    days = (date(*at[:3]) - date(*today[:3])).days
    if days == 0:
        return time.strftime("%H:%M", at)
    if days == 1:
        return time.strftime("tomorrow %H:%M", at)
    return f"{time.strftime('%a', at)} {at.tm_mday} {time.strftime('%b %H:%M', at)}"


def timer_line(timer: Timer) -> str:
    icon, title = TIMER_ICON[timer.action], TIMER_TITLE[timer.action]
    left = duration(timer.due - time.time())
    return f"{icon} {title}{of(timer.machine)} at {when_text(timer.due)} (in {left})"


def save_timers() -> bool:
    """False (and a log line) if it failed. {machine: {action: {due, chat_id}}}."""
    data: dict[str, dict[str, dict[str, float]]] = {}
    for t in _timers.values():
        data.setdefault(t.machine, {})[t.action] = {"due": t.due, "chat_id": t.chat_id}
    return save_json(TIMERS_FILE, data, "timers")


def parse_timers(machine: str, data: dict[str, object]) -> list[Timer]:
    """One machine's {action: {due, chat_id}}; malformed entries are skipped (logged)."""
    loaded: list[Timer] = []
    for action, entry in data.items():
        fields = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        due, chat_id = fields.get("due"), fields.get("chat_id")
        if (
            action in TIMER_ACTIONS
            and isinstance(due, int | float)
            and not isinstance(due, bool)
            and isinstance(chat_id, int)
            and not isinstance(chat_id, bool)
        ):
            loaded.append(Timer(action, float(due), chat_id, machine))
        else:
            log.warning("Ignoring malformed timer %r in %s", action, TIMERS_FILE)
    return loaded


def load_timers() -> list[Timer]:
    """Every saved timer. The file from before several machines ({action: {due,
    chat_id}}) is read as the first machine's."""
    if (data := load_json(TIMERS_FILE, "timers")) is None:
        return []
    if not isinstance(data, dict):
        log.warning("Ignoring timers file %s: not a JSON object", TIMERS_FILE)
        return []
    entries = cast(dict[str, object], data)
    if any(isinstance(v, dict) and "due" in v for v in entries.values()):
        return parse_timers(first_machine_name(), entries)  # the old format
    loaded: list[Timer] = []
    for machine, per_machine in entries.items():
        if isinstance(per_machine, dict):
            loaded += parse_timers(machine, cast(dict[str, object], per_machine))
        else:
            log.warning("Ignoring malformed timers in %s", TIMERS_FILE)
    return loaded


async def sleep_until(when: float) -> None:
    while (left := when - time.time()) > 0:
        await asyncio.sleep(min(left, TIMER_TICK_SECONDS))


def timer_buttons(timer: Timer) -> list[InlineKeyboardButton]:
    """Cancel (and +30m for shutdown). The due time in the data makes a button
    for a timer that has since changed or fired do nothing."""
    ref = f"{timer.action}:{int(timer.due)}"
    which = f" ({timer.machine})" if machines.several() else ""
    tag = timer_tag(timer)
    buttons = [
        InlineKeyboardButton(
            f"✖️ Cancel {timer.action}{which}", callback_data=tag(f"timer:cancel:{ref}")
        )
    ]
    if timer.action == "shutdown":
        buttons.append(InlineKeyboardButton("⏰ +30m", callback_data=tag(f"timer:snooze:{ref}")))
    return buttons


def timer_tag(timer: Timer) -> Callable[[str], str]:
    """Appends the timer's machine to callback data when there are several."""
    return lambda data: f"{data}:{timer.machine}" if machines.several() else data


def shutdown_warning(timer: Timer, awake: bool) -> str:
    left = duration(timer.due - time.time())
    name = f" {escape(timer.machine)}" if machines.several() else ""
    if not awake:
        # fire_timer() checks again at the due time and sends nothing if it's down.
        who = escape(timer.machine) if machines.several() else "The workstation"
        return card(
            "⏰",
            f"Shutdown timer{of(timer.machine)} in {left}",
            f"{who} is already asleep or off. "
            + f"If it's still down at {when_text(timer.due)}, nothing will be sent.",
        )
    return card(
        "⏰",
        f"Shutting down{name} in {left}",
        f"Scheduled for {when_text(timer.due)}. Unsaved work will be lost.",
    )


async def warn_before_shutdown(app: App, timer: Timer) -> None:
    if (machine := machines.named(timer.machine)) is None:
        return  # not configured any more
    awake = await probes.is_awake(machine)
    try:
        _ = await app.bot.send_message(
            timer.chat_id,
            shutdown_warning(timer, awake),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([timer_buttons(timer)]),
        )
    except TelegramError as e:
        log.warning("Could not send the shutdown warning: %s", e)


async def fire_timer(app: App, timer: Timer) -> None:
    if (machine := machines.named(timer.machine)) is None:
        return  # not configured any more (restore_timers drops those)
    audit(timer.action, "timer_fired", due=when_text(timer.due), machine=timer.machine)
    post = chat_poster(app, timer.chat_id)
    title = f"Scheduled {timer.action}{of(timer.machine)}"
    _ = await post(card("⏰", title, f"Set for {when_text(timer.due)}."))
    if timer.action == "wake":
        await start_wake(machine, post, app_spawner(app))
        return
    if message := await post(POWER_ACTIONS["shutdown"].progress):
        await run_power_action(
            machine,
            "shutdown",
            message_setter(message),
            app_spawner(app),
            event="timer_confirmed",
        )


async def run_timer(app: App, timer: Timer) -> None:
    try:
        if timer.action == "shutdown" and timer.due - TIMER_WARNING_SECONDS > time.time():
            await sleep_until(timer.due - TIMER_WARNING_SECONDS)
            await warn_before_shutdown(app, timer)
        await sleep_until(timer.due)
        # Spent from here on, so a restart mid-action can't run it twice.
        del _timers[timer.key]
        del _timer_tasks[timer.key]
        _ = save_timers()
        await fire_timer(app, timer)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Timer for %s failed", timer.action)


def start_timer(app: App, timer: Timer) -> None:
    """Arm `timer`, replacing any timer for the same machine and action. Doesn't save."""
    if old := _timer_tasks.pop(timer.key, None):
        _ = old.cancel()
    _timers[timer.key] = timer
    _timer_tasks[timer.key] = asyncio.get_running_loop().create_task(run_timer(app, timer))


def cancel_timer(key: tuple[str, str]) -> bool:
    """Disarm and forget the timer for (machine, action). False if saving failed."""
    if task := _timer_tasks.pop(key, None):
        _ = task.cancel()
    _ = _timers.pop(key, None)
    return save_timers()


NOT_SAVED = "<i>⚠️ Couldn't save it to disk, so a bot restart will lose it. See the logs.</i>"


# /schedule_shutdown with no time asks for one: tap a preset or type it.
WHEN_PRESETS = {"shutdown": ("15m", "30m", "1h", "2h"), "wake": ("1h", "4h", "8h", "12h")}
# How long a typed reply still counts as the answer to "when?".
ASK_WHEN_TTL_SECONDS = 120
# (machine name, action, time.monotonic() when asked)
_asking_when: tuple[str, str, float] | None = None


def stop_asking_when() -> None:
    global _asking_when
    _asking_when = None


def when_buttons(action: str, machine: Machine) -> InlineKeyboardMarkup:
    presets = [
        InlineKeyboardButton(
            f"in {p}", callback_data=machines.tagged(f"when:{action}:{p}", machine)
        )
        for p in WHEN_PRESETS[action]
    ]
    return InlineKeyboardMarkup(
        [presets, [InlineKeyboardButton("Cancel", callback_data="cancel:sched")]]
    )


def schedule_prompt(
    action: str, machine: Machine, due: float, now: float
) -> tuple[str, InlineKeyboardMarkup]:
    """The "Schedule a shutdown?" question with its Schedule/Cancel buttons."""
    lines = [f"At {when_text(due, now)}, in {duration(due - now)}."]
    if old := _timers.get((machine.name, action)):
        lines.append(f"Replaces the one set for {when_text(old.due, now)}.")
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⏰ Schedule {action}",
                    callback_data=machines.tagged(f"sched:{action}:{int(due)}:{int(now)}", machine),
                ),
                InlineKeyboardButton("Cancel", callback_data="cancel:sched"),
            ]
        ]
    )
    title = f"{TIMER_ICON[action]} <b>Schedule a {action}{of(machine.name)}?</b>"
    return "\n".join([title, *lines]), buttons


def when_error(action: str, machine: Machine, error: ValueError) -> str:
    name = f"{escape(machine.name)} " if machines.several() else ""
    return card(
        "⏰",
        f"Schedule a {action}{of(machine.name)}",
        str(error),
        f"<i>e.g. /schedule_{action} {name}45m</i>",
    )


async def ask_to_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    """/schedule_<action> [machine] [when]: the machine first (asks with buttons if
    there are several and none is named), then the time."""
    if (chosen := await machines.choose(update, context.args or [], f"schedule_{action}")) is None:
        return
    machine, args = chosen
    await schedule_for(update, action, machine, args)


async def schedule_for(update: Update, action: str, machine: Machine, args: list[str]) -> None:
    """Ask "when?" (no time given) or for confirmation of the given time."""
    global _asking_when
    text = " ".join(args)
    if not text.strip():
        _asking_when = (machine.name, action, time.monotonic())
        await reply(
            update,
            card(
                TIMER_ICON[action],
                f"{TIMER_TITLE[action]}{of(machine.name)} when?",
                f"Tap one, or type it: {TIMER_USAGE}.",
            ),
            when_buttons(action, machine),
        )
        return
    now = time.time()
    try:
        due = parse_when(text, now)
    except ValueError as e:
        await reply(update, when_error(action, machine, e))
        return
    await reply(update, *schedule_prompt(action, machine, due, now))


@authorized
async def on_text(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """A typed answer to "when?". Any other text is ignored, as before."""
    global _asking_when
    message = update.effective_message
    if _asking_when is None or message is None or not message.text:
        return
    name, action, asked = _asking_when
    machine = machines.named(name)
    if time.monotonic() - asked > ASK_WHEN_TTL_SECONDS or machine is None:
        _asking_when = None
        return
    now = time.time()
    try:
        due = parse_when(message.text, now)
    except ValueError as e:
        await reply(update, when_error(action, machine, e))  # still listening: they can retry
        return
    _asking_when = None
    await reply(update, *schedule_prompt(action, machine, due, now))


async def on_when_button(query: CallbackQuery, rest: str) -> None:
    """A preset from "when?": rest is "<action>:<preset>[:<machine>]". Turns into
    the confirmation."""
    global _asking_when
    fields = rest.split(":")
    action, preset = (fields + ["", ""])[:2]
    machine = machines.from_field(fields, 2)
    _ = await query.answer()
    now = time.time()
    try:
        if action not in TIMER_ACTIONS or machine is None:
            raise ValueError("Unknown timer.")
        due = parse_when(preset, now)
    except ValueError:
        await edit(query, "✖️ Cancelled. No timer set.")
        return
    _asking_when = None
    await edit(query, *schedule_prompt(action, machine, due, now))


@authorized
async def schedule_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_to_schedule(update, context, "shutdown")


@authorized
async def schedule_wake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_to_schedule(update, context, "wake")


def pending_timers() -> list[Timer]:
    """Armed timers, soonest first."""
    return sorted(_timers.values(), key=lambda t: t.due)


# Shown when no timer is set. "menu:" buttons run the command (see handlers.py).
SCHEDULE_BUTTONS = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("🔌 Schedule shutdown", callback_data="menu:schedule_shutdown"),
            InlineKeyboardButton("⚡ Schedule wake", callback_data="menu:schedule_wake"),
        ]
    ]
)


def timers_message() -> tuple[str, InlineKeyboardMarkup | None]:
    pending = pending_timers()
    if not pending:
        return card(
            "⏰", "No timers", "Tap to set one, or send /schedule_shutdown or /schedule_wake."
        ), SCHEDULE_BUTTONS
    lines = [timer_line(t) for t in pending]
    return card("⏰", "Timers", *lines), InlineKeyboardMarkup([timer_buttons(t) for t in pending])


@authorized
async def timers(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, *timers_message())


async def confirm_schedule(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    """The "Schedule" button: rest is "<action>:<due>:<issued>[:<machine>]"."""
    fields = rest.split(":")
    action, due, issued = (fields + ["", ""])[:3]
    machine = machines.from_field(fields, 3)
    if action not in TIMER_ACTIONS or not is_number(due) or machine is None:
        await edit(query, "✖️ Cancelled. No timer set.")
        return
    if not is_number(issued) or time.time() - int(issued) > CONFIRM_TTL_SECONDS:
        audit(action, "timer_expired")
        await edit(
            query,
            card(
                "⌛",
                "Expired",
                f"This confirmation is over {CONFIRM_TTL_SECONDS}s old. "
                + f"Send /schedule_{action} again.",
            ),
        )
        return
    # The button's times, checked again: asked in the past, due ahead, and no
    # further from when it was asked than parse_when() allows. (No lower bound
    # beyond "ahead": a clock time like 22:30 may be due in under MIN_TIMER_SECONDS.)
    now = time.time()
    if not (int(issued) <= now < int(due) <= int(issued) + MAX_TIMER_SECONDS):
        audit(action, "timer_rejected", machine=machine.name)
        await edit(query, "✖️ Cancelled. No timer set.")
        return
    # Reports go where it was set; a private chat's ID is its user's.
    chat = query.message.chat.id if query.message else query.from_user.id
    timer = Timer(action, float(due), chat, machine.name)
    start_timer(cast(App, context.application), timer)
    saved = save_timers()
    audit(action, "timer_set", due=when_text(timer.due), saved=saved, machine=machine.name)
    lines = [f"{timer_line(timer)}.", "Change or cancel it with /timers."]
    if timer.action == "shutdown" and timer.due - time.time() > TIMER_WARNING_SECONDS:
        lines.append(f"<i>I'll warn you {duration(TIMER_WARNING_SECONDS)} before.</i>")
    if not saved:
        lines.append(NOT_SAVED)
    await edit(query, card("⏰", f"{TIMER_TITLE[action]} scheduled", *lines))


async def on_timer_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    """Cancel or +30m, from /timers or the shutdown warning:
    "<op>:<action>:<due>[:<machine>]"."""
    fields = rest.split(":")
    op, action, due = (fields + ["", ""])[:3]
    machine = machines.from_field(fields, 3)
    timer = _timers.get((machine.name, action)) if machine is not None else None
    if timer is None or str(int(timer.due)) != due or op not in ("cancel", "snooze"):
        _ = await query.answer("That timer already ran or was changed.")
        await edit(query, *timers_message())
        return
    if op == "cancel":
        saved = cancel_timer(timer.key)
        audit(action, "timer_cancelled", machine=timer.machine)
        _ = await query.answer("Cancelled")
        lines = [f"The {action}{of(timer.machine)} set for {when_text(timer.due)} won't happen."]
        if not saved:
            lines.append("<i>⚠️ Couldn't update the file on disk. See the logs.</i>")
        await edit(query, card("✖️", "Timer cancelled", *lines))
        return
    moved = Timer(action, timer.due + SNOOZE_SECONDS, timer.chat_id, timer.machine)
    start_timer(cast(App, context.application), moved)
    saved = save_timers()
    audit(action, "timer_snoozed", due=when_text(moved.due), machine=timer.machine)
    _ = await query.answer(f"Moved to {when_text(moved.due)}")
    lines = [f"{timer_line(moved)}."]
    if not saved:
        lines.append(NOT_SAVED)
    await edit(query, card("⏰", f"{TIMER_TITLE[action]} postponed", *lines))


def restore_timers(app: App) -> tuple[list[Timer], list[Timer]]:
    """Re-arm saved timers at startup; returns (missed, dropped): the ones that
    fell due while the bot was down, and the ones for a machine that is no longer
    configured (logged and audited by name; the startup report names them).

    A timer set from a chat that is no longer in AUTHORIZED_USER_IDS is dropped
    without a word to that chat: whoever was removed mustn't still get reports.
    """
    now = time.time()
    missed: list[Timer] = []
    dropped: list[Timer] = []
    changed = False
    for timer in load_timers():
        if machines.named(timer.machine) is None:
            log.warning(
                "Dropped the saved %s timer of %s: no such machine any more",
                timer.action,
                timer.machine,
            )
            audit(
                timer.action,
                "timer_dropped",
                reason="machine_not_configured",
                machine=timer.machine,
            )
            dropped.append(timer)
            changed = True
        elif timer.chat_id not in AUTHORIZED_USER_IDS:
            # Not logged by ID, like everything else about who may use the bot.
            log.warning("Dropped the saved %s timer: its chat is no longer allowed", timer.action)
            audit(timer.action, "timer_dropped", reason="chat_not_allowed")
            changed = True
        elif timer.due < now - MISSED_GRACE_SECONDS:
            audit(timer.action, "timer_missed", due=when_text(timer.due, now))
            missed.append(timer)
            changed = True
        else:
            start_timer(app, timer)
    if changed:
        _ = save_timers()
    return missed, dropped


async def stop_timers(_app: App) -> None:
    """Stop the timer tasks on shutdown; the file keeps them for the next start."""
    for task in _timer_tasks.values():
        _ = task.cancel()
    _timer_tasks.clear()
