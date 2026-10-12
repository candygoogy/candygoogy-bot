"""Repeating schedules in chat: "/schedule_shutdown weekdays 23:00" (or typed
into "when?"), its confirmation, /timers' "Repeating" block and its buttons,
and the buttons under a repeating shutdown's warning. The engine is schedules.py.

Callback data: "rsched:<action>:<daymask>:<HHMM>:<issued>[:<machine>]" confirms
a new schedule (used once, expires like every confirmation); "rep:<op>:<id>[:
<YYYYMMDD>]" skips, unskips or deletes one, the date naming the run a skip is
for, so a button for a run that has since happened does nothing.
"""

import time
from dataclasses import replace
from datetime import date
from typing import cast

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from candygoogy import machines, schedules, timers
from candygoogy.auth import authorized
from candygoogy.config import CONFIRM_TTL_SECONDS, App, audit
from candygoogy.messages import card, edit, reply
from candygoogy.schedules import Schedule, ScheduleError
from candygoogy.settings import DAY_NAMES, Machine, is_number
from candygoogy.timers import (
    NOT_SAVED,
    SCHEDULE_BUTTONS,
    SNOOZE_SECONDS,
    TIMER_ACTIONS,
    TIMER_ICON,
    TIMER_TITLE,
    TIMER_WARNING_SECONDS,
    Timer,
    of,
    when_text,
)

REPEAT_EXAMPLE = "weekdays 23:00"
NO_SCHEDULE = "That schedule doesn't exist any more."
ALREADY_HAPPENED = "That run already happened."


# --- Asking ----------------------------------------------------------------------------


def days_mask(days: tuple[int, ...]) -> int:
    return sum(1 << d for d in days)


def mask_days(mask: int) -> tuple[int, ...]:
    return tuple(d for d in range(7) if mask >> d & 1)


def every(days: tuple[int, ...]) -> str:
    """ "Every weekday", "Every Monday, Wednesday and Friday", ..."""
    named: dict[tuple[int, ...], str] = {
        schedules.EVERY_DAY: "Every day",
        (0, 1, 2, 3, 4): "Every weekday",
    }
    if days in named:
        return named[days]
    names = [DAY_NAMES[d].capitalize() for d in days]
    return "Every " + (
        " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]
    )


def repeat_prompt(
    action: str, machine: Machine, days: tuple[int, ...], at: tuple[int, int], now: float
) -> tuple[str, InlineKeyboardMarkup]:
    """ "Repeat a shutdown of desk?" with its Repeat/Cancel buttons."""
    first = schedules.first_run(days, at, now)
    hhmm = f"{at[0]:02d}{at[1]:02d}"
    data = f"rsched:{action}:{days_mask(days)}:{hhmm}:{int(now)}"
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🔁 Repeat {action}", callback_data=machines.tagged(data, machine)
                ),
                InlineKeyboardButton("Cancel", callback_data="cancel:sched"),
            ]
        ]
    )
    text = card(
        "🔁",
        f"Repeat a {action}{of(machine.name)}?",
        f"{every(days)} at {at[0]:02d}:{at[1]:02d}. First: {when_text(first, now)}.",
    )
    return text, buttons


async def ask_to_repeat(update: Update, action: str, machine: Machine, text: str) -> bool:
    """Reply to a typed schedule: its confirmation, or why it can't be read.
    False if it was refused (e.g. a typo: a "when?" prompt stays open)."""
    if reason := schedules.schedules_disabled_reason():
        await reply(update, timers.when_error(action, machine, ValueError(reason), REPEAT_EXAMPLE))
        return False
    try:
        days, at = schedules.parse_rule(text)
    except ScheduleError as e:
        await reply(update, timers.when_error(action, machine, e, REPEAT_EXAMPLE))
        return False
    await reply(update, *repeat_prompt(action, machine, days, at, time.time()))
    return True


async def schedule_for(update: Update, action: str, machine: Machine, args: list[str]) -> None:
    """/schedule_<action> for a machine: day words repeat, anything else is the
    one-shot timer as before."""
    text = " ".join(args)
    if schedules.starts_with_days(text):
        _ = await ask_to_repeat(update, action, machine, text)
        return
    await timers.schedule_for(update, action, machine, args)


async def ask_to_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if (chosen := await machines.choose(update, context.args or [], f"schedule_{action}")) is None:
        return
    machine, args = chosen
    await schedule_for(update, action, machine, args)


@authorized
async def schedule_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_to_schedule(update, context, "shutdown")


@authorized
async def schedule_wake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ask_to_schedule(update, context, "wake")


@authorized
async def on_when_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A typed answer to this user's "when?": day words repeat; anything else
    goes to the one-shot timer as before."""
    message, user = update.effective_message, update.effective_user
    text = message.text if message is not None else None
    if (
        user is not None
        and text
        and schedules.starts_with_days(text)
        and (asking := timers.asking_when(user.id)) is not None
    ):
        machine, action = asking
        if await ask_to_repeat(update, action, machine, text):
            timers.stop_asking_when(user.id)  # else still listening: they can retry
        return
    await timers.on_text(update, context)


# --- Confirming --------------------------------------------------------------------------


def parse_hhmm(text: str) -> tuple[int, int] | None:
    if len(text) != 4 or not is_number(text) or int(text[:2]) > 23 or int(text[2:]) > 59:
        return None
    return int(text[:2]), int(text[2:])


async def confirm_repeat(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    """The "Repeat" button: rest is "<action>:<daymask>:<HHMM>:<issued>[:<machine>]"."""
    fields = rest.split(":")
    action, mask, hhmm, issued = (fields + ["", "", ""])[:4]
    machine = machines.from_field(fields, 4)
    at = parse_hhmm(hhmm)
    if (
        action not in TIMER_ACTIONS
        or not is_number(mask)
        or len(mask) > 3
        or not 1 <= int(mask) <= 127
        or at is None
        or machine is None
    ):
        await edit(query, "✖️ Cancelled. Nothing set.")
        return
    now = time.time()
    if not is_number(issued) or not int(issued) <= now <= int(issued) + CONFIRM_TTL_SECONDS:
        audit(action, "schedule_expired")
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
    # Reports go where it was set; a private chat's ID is its user's.
    chat = query.message.chat.id if query.message else query.from_user.id
    app = cast(App, context.application)
    try:
        s, saved = schedules.add_schedule(app, machine.name, action, mask_days(int(mask)), at, chat)
    except ScheduleError as e:
        await edit(query, card("✖️", "Not set", str(e)))
        return
    due = schedules.next_occurrence(s, now)
    lines = [f"{repeat_line(s, due, now)}.", "Skip a run or delete it with /timers."]
    if action == "shutdown":
        lines.append(f"<i>I'll warn you {TIMER_WARNING_SECONDS // 60}m before each one.</i>")
    if not saved:
        lines.append(NOT_SAVED)
    await edit(query, card("🔁", f"Repeating {action} set", *lines))


# --- /timers --------------------------------------------------------------------------


def upcoming(s: Schedule, now: float) -> tuple[date, float, float | None]:
    """The next run (date, time), and if that one is skipped, the run after it."""
    day, due = schedules.next_run(s, now)
    if s.skip != day:
        return day, due, None
    return day, due, schedules.next_occurrence(replace(s, last=day), due)


def repeat_line(s: Schedule, due: float, now: float | None = None) -> str:
    """ "🔁🔌 Shutdown of desk · weekdays 23:00 · next Mon 23:00"."""
    now = time.time() if now is None else now
    head = f"🔁{TIMER_ICON[s.action]} {TIMER_TITLE[s.action]}{of(s.machine)} · {s.rule}"
    _, first, after_skip = upcoming(s, now)
    if after_skip is not None:
        return f"{head} · skipping {when_text(first, now)}, next {when_text(after_skip, now)}"
    return f"{head} · next {when_text(due, now)}"


def repeat_buttons(s: Schedule, now: float) -> list[InlineKeyboardButton]:
    day, _, after_skip = upcoming(s, now)
    which = f"{s.machine} {s.rule}" if machines.several() else s.rule
    if after_skip is not None:
        skip = InlineKeyboardButton(f"↩️ Don't skip · {which}", callback_data=f"rep:unskip:{s.id}")
    else:
        skip = InlineKeyboardButton(
            f"⏭ Skip next · {which}",
            callback_data=f"rep:skip:{s.id}:{schedules.day_field(day)}",
        )
    return [skip, InlineKeyboardButton("🗑 Delete", callback_data=f"rep:delete:{s.id}")]


def timers_message() -> tuple[str, InlineKeyboardMarkup | None]:
    """/timers: the one-shot timers, then the repeating schedules."""
    now = time.time()
    pending = timers.pending_timers()
    repeating = schedules.pending_schedules()
    lines = [r] if (r := schedules.schedules_disabled_reason()) else []
    if not pending and not repeating:
        hint = f"To repeat: /schedule_shutdown {REPEAT_EXAMPLE}."
        return card("⏰", "No timers", *lines, timers.NO_TIMERS, hint), SCHEDULE_BUTTONS
    rows: list[list[InlineKeyboardButton]] = []
    lines += [timers.timer_line(t) for t in pending]
    rows += [timers.timer_buttons(t) for t in pending]
    if repeating:
        lines.append("Repeating:")
        lines += [repeat_line(s, due, now) for s, due in repeating]
        rows += [repeat_buttons(s, now) for s, _ in repeating]
    return card("⏰", "Timers", *lines), InlineKeyboardMarkup(rows)


@authorized
async def timers_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, *timers_message())


# --- Buttons: /timers' Skip next / Don't skip / Delete, the warning's Skip / +30m -----


def is_next_run(s: Schedule, field: str) -> date | None:
    """The run `field` (YYYYMMDD) names, if it's still the schedule's next one."""
    day, _ = schedules.next_run(s, time.time())
    return day if field == schedules.day_field(day) else None


async def on_repeat_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    """rest is "<op>:<id>[:<YYYYMMDD>]". Reusable: each names the exact run."""
    app = cast(App, context.application)
    fields = rest.split(":")
    op, sid, field = (fields + ["", ""])[:3]
    in_list = op in ("skip", "unskip", "delete")
    if op not in ("skip", "unskip", "delete", "skipone", "later"):
        _ = await query.answer()
        return
    if (s := schedules.get_schedule(sid)) is None:
        _ = await query.answer(NO_SCHEDULE)
        if in_list:
            await edit(query, *timers_message())
        return
    if not in_list and s.action != "shutdown":  # only a shutdown has a warning (forged data)
        _ = await query.answer()
        return
    if op in ("skip", "skipone", "later") and is_next_run(s, field) is None:
        _ = await query.answer(ALREADY_HAPPENED)
        if in_list:
            await edit(query, *timers_message())
        return
    if op == "unskip":
        _ = schedules.clear_skip(app, sid)
        _ = await query.answer("Not skipping it")
    elif op == "delete":
        _ = schedules.delete_schedule(sid)
        _ = await query.answer("Deleted")
    else:
        day, saved = schedules.set_skip(app, sid)
        if op == "skip" or day is None:
            _ = await query.answer("Skipping the next one")
        else:
            await on_warning_choice(query, app, s, day, op == "later", saved)
            return
    await edit(query, *timers_message())


async def on_warning_choice(
    query: CallbackQuery, app: App, s: Schedule, day: date, later: bool, saved: bool
) -> None:
    """The warning's Skip this one (skip is set) or +30m (skip is set, and a
    one-shot shutdown is set 30 minutes after this run)."""
    due = schedules.run_time(day, s.at)
    _ = await query.answer("Postponed" if later else "Skipped")
    nxt = schedules.get_schedule(s.id)
    following = when_text(upcoming(nxt, time.time())[2] or due) if nxt else ""
    if not later:
        lines = [f"This {s.action}{of(s.machine)} won't happen. Next: {following}."]
        if not saved:
            lines.append(NOT_SAVED)
        await edit(query, card("⏭", "Skipped", *lines))
        return
    chat = query.message.chat.id if query.message else query.from_user.id
    timer = Timer("shutdown", due + SNOOZE_SECONDS, chat, s.machine)
    timers.start_timer(app, timer)
    saved = timers.save_timers() and saved
    audit("shutdown", "schedule_postponed", machine=s.machine, rule=s.rule)
    lines = [
        f"{timers.timer_line(timer)}, this time only.",
        f"Repeats as usual from {following}.",
    ]
    if not saved:
        lines.append(NOT_SAVED)
    await edit(query, card("⏰", "Postponed", *lines))
