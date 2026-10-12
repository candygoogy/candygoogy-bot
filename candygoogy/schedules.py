"""Repeating schedules: a shutdown or wake on chosen weekdays at a local time,
e.g. "weekdays 23:00". Saved in schedules.json, next to (never inside)
timers.json, so an older version that doesn't know them leaves them alone.

Each schedule keeps only its rule and `last`, the local date of the last run it
handled (ran, missed, skipped or deferred). A run is never earlier than the day
after `last`, so a run can't happen twice on one date, across restarts and on
the day the clocks go back. Runs missed while the bot was down are reported,
never made up for late. Setting, skipping and deleting from chat go through
add_schedule(), set_skip(), clear_skip() and delete_schedule().
"""

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from html import escape
from typing import cast

from telegram import InlineKeyboardButton

from candygoogy import machines, timers
from candygoogy.config import AUTHORIZED_USER_IDS, STATE_DIR, App, audit
from candygoogy.messages import card, chat_poster
from candygoogy.settings import CLOCK, DAY_NAMES
from candygoogy.state import load_json, save_json
from candygoogy.timers import (
    MISSED_GRACE_SECONDS,
    TIMER_ACTIONS,
    TIMER_TITLE,
    TIMER_WARNING_SECONDS,
    Timer,
    of,
    when_text,
)

log = logging.getLogger(__name__)

SCHEDULES_FILE = STATE_DIR / "schedules.json"
# The file's format. A file with another version is from a newer bot: it is
# then neither run nor ever written (see schedules_disabled_reason()).
FORMAT_VERSION = 1
DISABLED = (
    "⚠️ schedules.json is from a newer version: repeating schedules are off until you upgrade."
)
MAX_PER_MACHINE_ACTION = 4
MAX_SCHEDULES = 20
# Longer text isn't a schedule; checked before any parsing.
MAX_RULE_CHARS = 40
# A one-shot timer this close to a run (same machine and action) replaces it.
OVERLAP_SECONDS = 600
# At startup, missed runs are looked for this many days back, no further.
LOOK_BACK_DAYS = 7
# The next run is always within this many dates (each weekday at least once,
# plus today, whose time may have passed).
CANDIDATE_DAYS = 8
# The longest clock change looked through for a time that doesn't exist.
MAX_GAP_MINUTES = 180
# After an unexpected failure, how long a schedule waits before carrying on.
RETRY_SECONDS = 60.0
ID = re.compile(r"[0-9a-f]{6}")
ID_BYTES = 3

EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)
NAMED_DAYS = {"daily": EVERY_DAY, "weekdays": (0, 1, 2, 3, 4), "weekends": (5, 6)}
DAYS_ERROR = "Couldn't read the days. Try daily, weekdays, weekends or mon,wed,fri."
TIME_ERROR = "Couldn't read the time. Use 24-hour HH:MM, e.g. 23:00."
TOO_LONG = "That's too long to be a schedule."


class ScheduleError(ValueError):
    """Why a rule or a new schedule was refused, as a message for the user."""


@dataclass(frozen=True)
class Schedule:
    id: str  # 6 hex characters, for button data
    machine: str  # a configured name
    action: str  # one of TIMER_ACTIONS
    days: tuple[int, ...]  # sorted, Monday is 0
    at: tuple[int, int]  # (hour, minute), local time
    chat_id: int  # where it was set, and where its runs report
    last: date | None = None  # the last run handled; None: none yet
    skip: date | None = None  # the date of a run to skip once

    @property
    def rule(self) -> str:
        return rule_text(self.days, self.at)


# --- The rule: "weekdays 23:00" ------------------------------------------------


def day_number(word: str) -> int | None:
    """0 for "mon" or "monday", ..., None if it isn't a day."""
    return next((i for i, name in enumerate(DAY_NAMES) if word in (name, name[:3])), None)


def parse_days(text: str) -> tuple[int, ...]:
    """daily, weekdays, weekends, or comma-separated days (no spaces)."""
    if text in NAMED_DAYS:
        return NAMED_DAYS[text]
    days: set[int] = set()
    for word in text.split(","):
        if (day := day_number(word)) is None:
            raise ScheduleError(DAYS_ERROR)
        days.add(day)
    return tuple(sorted(days))


def starts_with_days(text: str) -> bool:
    """Whether typed text is meant as a schedule: it starts with a day ("mon-fri"
    and "everyday" too, so they get the schedule's error, not a timer's)."""
    first = re.split(r"[\s,-]", text.strip().lower(), maxsplit=1)[0]
    return first in NAMED_DAYS or first.startswith("every") or day_number(first) is not None


def parse_rule(text: str) -> tuple[tuple[int, ...], tuple[int, int]]:
    """(days, (hour, minute)) from "weekdays 23:00", "mon, wed,fri 7:30", ...
    Raises ScheduleError with a message for the user."""
    if len(text.strip()) > MAX_RULE_CHARS:
        raise ScheduleError(TOO_LONG)
    words = text.lower().split()
    if len(words) < 2:
        raise ScheduleError(DAYS_ERROR if not words or ":" in words[0] else TIME_ERROR)
    days = parse_days("".join(words[:-1]))  # "mon, fri" is "mon,fri"
    if not (match := CLOCK.fullmatch(words[-1])):
        raise ScheduleError(TIME_ERROR)
    return days, (int(match[1]), int(match[2]))


def rule_text(days: tuple[int, ...], at: tuple[int, int]) -> str:
    """The canonical text: "daily 23:00", "weekdays 07:30" or "mon,wed,fri 07:30"."""
    named = next((name for name, d in NAMED_DAYS.items() if d == days), None)
    listed = named or ",".join(DAY_NAMES[d][:3] for d in days)
    return f"{listed} {at[0]:02d}:{at[1]:02d}"


# --- When it runs ----------------------------------------------------------------


def local_minute(when: float) -> tuple[date, int, int]:
    at = time.localtime(when)
    return date(at.tm_year, at.tm_mon, at.tm_mday), at.tm_hour, at.tm_min


def local_date(when: float) -> date:
    return local_minute(when)[0]


def run_time(day: date, at: tuple[int, int]) -> float:
    """When `day` at hour:minute is, in local time. On the day the clocks go
    forward, a time in the gap (e.g. 02:30) is the first minute after it (03:00).
    On the day they go back, a time that happens twice is one of them; `last`
    keeps the other from running."""
    target = (day, *at)
    due = time.mktime((day.year, day.month, day.day, at[0], at[1], 0, 0, 0, -1))
    if local_minute(due) == target:
        return due
    for _ in range(MAX_GAP_MINUTES):  # C libraries round a missing time either way
        if local_minute(due) < target:
            due += 60
        elif local_minute(due - 60) >= target:
            due -= 60
        else:
            break
    return due


def next_run(schedule: Schedule, after: float) -> tuple[date, float]:
    """The first run after `after`, as (its local date, its time): on the dates
    from the day after `last`, or from today if that's later."""
    start = local_date(after)
    if schedule.last is not None:
        start = max(start, schedule.last + timedelta(days=1))
    for offset in range(CANDIDATE_DAYS):
        day = start + timedelta(days=offset)
        if day.weekday() in schedule.days and (due := run_time(day, schedule.at)) > after:
            return day, due
    raise ValueError("A schedule without days")  # parse_rule() and loading refuse those


def next_occurrence(schedule: Schedule, after: float) -> float:
    return next_run(schedule, after)[1]


def past_runs(schedule: Schedule, now: float) -> list[tuple[date, float]]:
    """Runs due at or before `now` that weren't handled yet, at most
    LOOK_BACK_DAYS back, oldest first."""
    today = local_date(now)
    day = today - timedelta(days=LOOK_BACK_DAYS)
    if schedule.last is not None:
        day = max(day, schedule.last + timedelta(days=1))
    runs: list[tuple[date, float]] = []
    while day <= today:
        if day.weekday() in schedule.days and (due := run_time(day, schedule.at)) <= now:
            runs.append((day, due))
        day += timedelta(days=1)
    return runs


def handled(schedule: Schedule, day: date) -> Schedule:
    """`schedule` once its run on `day` is dealt with: a skip for it (or an
    older, stale one) is used up."""
    skip = schedule.skip if schedule.skip is not None and schedule.skip > day else None
    return replace(schedule, last=day, skip=skip)


# --- Saving and loading -------------------------------------------------------------

_schedules: dict[str, Schedule] = {}  # by ID, in the order they were set
_tasks: dict[str, asyncio.Task[None]] = {}
# True when schedules.json is from a newer version: nothing runs and it's never written.
_disabled = False


def schedules_disabled_reason() -> str | None:
    """The ⚠️ line for the startup report and /timers, or None if schedules work."""
    return DISABLED if _disabled else None


def to_json(s: Schedule) -> dict[str, object]:
    return {
        "id": s.id,
        "machine": s.machine,
        "action": s.action,
        "days": list(s.days),
        "at": f"{s.at[0]:02d}:{s.at[1]:02d}",
        "chat_id": s.chat_id,
        "last": s.last.isoformat() if s.last is not None else None,
        "skip": s.skip.isoformat() if s.skip is not None else None,
    }


def save_schedules() -> bool:
    """False (and a log line) if it failed, or if the file is a newer version's."""
    if _disabled:
        return False
    data = {"version": FORMAT_VERSION, "schedules": [to_json(s) for s in _schedules.values()]}
    return save_json(SCHEDULES_FILE, data, "schedules")


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def iso_date(value: object) -> date | None:
    """A date from "2026-10-12"; ValueError or TypeError if it's neither that nor null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("not a date")
    return date.fromisoformat(value)


def from_json(entry: object) -> Schedule | None:
    """One saved schedule, or None if it's malformed."""
    if not isinstance(entry, dict):
        return None
    fields = cast(dict[str, object], entry)
    sid, machine, action = fields.get("id"), fields.get("machine"), fields.get("action")
    days, at, chat_id = fields.get("days"), fields.get("at"), fields.get("chat_id")
    if not (
        isinstance(sid, str)
        and ID.fullmatch(sid)
        and isinstance(machine, str)
        and action in TIMER_ACTIONS
        and isinstance(days, list)
        and days
        and all(is_int(d) and 0 <= cast(int, d) <= 6 for d in cast(list[object], days))
        and isinstance(at, str)
        and (clock := CLOCK.fullmatch(at))
        and is_int(chat_id)
    ):
        return None
    try:
        last, skip = iso_date(fields.get("last")), iso_date(fields.get("skip"))
    except (ValueError, TypeError):
        return None
    day_set = tuple(sorted(set(cast(list[int], days))))
    hour_minute = (int(clock[1]), int(clock[2]))
    return Schedule(
        sid, machine, cast(str, action), day_set, hour_minute, cast(int, chat_id), last, skip
    )


def load_schedules() -> tuple[list[Schedule], bool]:
    """The saved schedules, and whether the file needs saving again (malformed
    or duplicate entries were left out). A newer version's file loads nothing
    and turns schedules off for this run."""
    global _disabled
    _disabled = False
    if (data := load_json(SCHEDULES_FILE, "schedules")) is None:
        return [], False
    fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
    version = fields.get("version")
    if not is_int(version) or version != FORMAT_VERSION:
        log.warning(
            "%s is from another version (%r): repeating schedules are off until you upgrade",
            SCHEDULES_FILE,
            version,
        )
        _disabled = True
        return [], False
    entries = fields.get("schedules")
    if not isinstance(entries, list):
        log.warning("Ignoring the schedules in %s: not a list", SCHEDULES_FILE)
        return [], True
    loaded: list[Schedule] = []
    changed = False
    for entry in cast(list[object], entries):
        if (s := from_json(entry)) is None:
            log.warning("Ignoring a malformed schedule in %s", SCHEDULES_FILE)
            changed = True
        elif any(s.id == other.id for other in loaded):
            log.warning("Ignoring a second schedule with the ID %s in %s", s.id, SCHEDULES_FILE)
            changed = True
        else:
            loaded.append(s)
    return loaded, changed


# --- Running -----------------------------------------------------------------------------


# Button data for a repeating run: "rep:<op>:<id>[:<YYYYMMDD>]" (see repeating.py).
def day_field(day: date) -> str:
    return day.strftime("%Y%m%d")


def warning_buttons(s: Schedule, day: date) -> list[InlineKeyboardButton]:
    """Under a repeating shutdown's warning: skip this run, or move it by 30 minutes."""
    return [
        InlineKeyboardButton(
            "⏭ Skip this one", callback_data=f"rep:skipone:{s.id}:{day_field(day)}"
        ),
        InlineKeyboardButton("⏰ +30m", callback_data=f"rep:later:{s.id}:{day_field(day)}"),
    ]


def as_timer(s: Schedule, due: float) -> Timer:
    """A run, as the one-shot timer code sees it (for the warning and the action)."""
    return Timer(s.action, due, s.chat_id, s.machine)


def deferred(s: Schedule, due: float) -> bool:
    """Whether a one-shot timer for the same machine and action is close
    enough to this run to replace it."""
    one_shot = timers.one_shot_due(s.machine, s.action)
    return one_shot is not None and abs(one_shot - due) <= OVERLAP_SECONDS


def missed_line(s: Schedule, missed: list[tuple[date, float]], earlier_ones: bool) -> str:
    """ "⚠️ Missed the repeating shutdown (weekdays 23:00) at Fri 9 Oct 23:00 (and 2 earlier)."""
    line = (
        f"⚠️ Missed the repeating {s.action}{of(s.machine)} ({s.rule}) at {when_text(missed[-1][1])}"
    )
    if earlier_ones:
        line += " (and earlier ones)"
    elif len(missed) > 1:
        line += f" (and {len(missed) - 1} earlier)"
    return line + ". Nothing was done."


def audit_schedule(s: Schedule, event: str, **details: object) -> None:
    audit(s.action, event, machine=s.machine, rule=s.rule, **details)


def update(s: Schedule) -> bool:
    """Keep the changed `s` and save; False if saving failed."""
    _schedules[s.id] = s
    return save_schedules()


async def handle_run(app: App, sid: str, day: date, due: float) -> None:
    """A run's time has come: skip it, leave it to a one-shot timer, report it
    as missed (the clock jumped past it), or run it."""
    if (s := _schedules.get(sid)) is None:
        return
    skipped = s.skip == day
    replaced = not skipped and deferred(s, due)
    late = time.time() - due > MISSED_GRACE_SECONDS
    _ = update(handled(s, day))  # before the action, so a restart can't run it twice
    if skipped:
        log.info("Skipped the repeating %s of %s once, as asked", s.action, s.machine)
    elif replaced:
        audit_schedule(s, "schedule_deferred", due=when_text(due))
    elif late:
        audit_schedule(s, "schedule_missed", due=when_text(due))
        _ = await chat_poster(app, s.chat_id)(
            card("⏰", "Missed", f"{missed_line(s, [(day, due)], False)} The clock jumped past it.")
        )
    else:
        audit_schedule(s, "schedule_fired", due=when_text(due))
        title = f"Scheduled {s.action}{of(s.machine)}"
        await timers.run_timed_action(
            app, as_timer(s, due), card("🔁", title, f"Repeating: {s.rule}.")
        )


async def next_one(app: App, sid: str, s: Schedule) -> None:
    """Wait for the schedule's next run (warning first, for a shutdown), then handle it."""
    day, due = next_run(s, time.time())
    if s.action == "shutdown" and due - TIMER_WARNING_SECONDS > time.time():
        await timers.sleep_until(due - TIMER_WARNING_SECONDS)
        if (now := _schedules.get(sid)) is None:
            return
        if now.skip != day and not deferred(now, due):
            buttons = warning_buttons(now, day)
            await timers.warn_before_shutdown(app, as_timer(now, due), buttons)
    await timers.sleep_until(due)
    await handle_run(app, sid, day, due)


async def run_schedule(app: App, sid: str, first: tuple[date, float] | None = None) -> None:
    """Runs a schedule until it's deleted or the bot stops; `first`: a run that
    is due right away (at startup, within the grace). A failure is logged and the
    schedule carries on (`last` is saved before an action, so nothing repeats)."""
    if first is not None:
        try:
            await handle_run(app, sid, *first)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Repeating schedule %s failed", sid)
    while (s := _schedules.get(sid)) is not None:
        try:
            await next_one(app, sid, s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Repeating schedule %s failed; trying again", sid)
            await asyncio.sleep(RETRY_SECONDS)


def arm(app: App, s: Schedule, first: tuple[date, float] | None = None) -> None:
    """(Re)start the task for `s`; any older task for it is cancelled."""
    if old := _tasks.pop(s.id, None):
        _ = old.cancel()
    _tasks[s.id] = asyncio.get_running_loop().create_task(run_schedule(app, s.id, first))


def catch_up(s: Schedule, now: float) -> tuple[Schedule, str | None, tuple[date, float] | None]:
    """At startup: `s` with its past runs handled, the line reporting missed ones
    (None if none), and a run still within the grace, to run now."""
    runs = past_runs(s, now)
    if not runs:
        return s, None, None
    caught_up = handled(s, runs[-1][0])
    if s.last is None:  # never handled anything: what's before now isn't missed
        return caught_up, None, None
    runs = [r for r in runs if r[0] != s.skip]  # skipped as asked, not missed
    run_now = runs.pop() if runs and now - runs[-1][1] <= MISSED_GRACE_SECONDS else None
    if not runs:
        return caught_up, None, run_now
    for _, due in runs:
        audit_schedule(s, "schedule_missed", due=when_text(due, now))
    window = local_date(now) - timedelta(days=LOOK_BACK_DAYS)
    return caught_up, missed_line(s, runs, s.last + timedelta(days=1) < window), run_now


def restore_schedules(app: App) -> list[str]:
    """Load and arm the saved schedules at startup. Returns lines for the
    startup report: dropped schedules, missed runs, or that schedules are off."""
    for task in _tasks.values():
        _ = task.cancel()
    _tasks.clear()
    _schedules.clear()
    loaded, changed = load_schedules()
    if _disabled:
        return [DISABLED]
    now = time.time()
    lines: list[str] = []
    armed: list[tuple[Schedule, tuple[date, float] | None]] = []
    for s in loaded:
        if machines.named(s.machine) is None:
            log.warning(
                "Dropped the repeating %s of %s: no such machine any more", s.action, s.machine
            )
            audit_schedule(s, "schedule_dropped", reason="machine_not_configured")
            lines.append(
                f"⚠️ Dropped the repeating {s.action} of {escape(s.machine)} ({s.rule}): "
                + "that machine isn't configured any more."
            )
            changed = True
            continue
        if s.chat_id not in AUTHORIZED_USER_IDS:
            # Not logged by ID, like everything else about who may use the bot.
            log.warning("Dropped a repeating %s: its chat is no longer allowed", s.action)
            audit_schedule(s, "schedule_dropped", reason="chat_not_allowed")
            lines.append(
                f"⚠️ Dropped the repeating {s.action} of {escape(s.machine)} ({s.rule}): "
                + "whoever set it is no longer allowed."
            )
            changed = True
            continue
        caught_up, line, run_now = catch_up(s, now)
        changed = changed or caught_up != s
        if line:
            lines.append(line)
        _schedules[s.id] = caught_up
        armed.append((caught_up, run_now))
    if changed:
        _ = save_schedules()
    for s, run_now in armed:
        arm(app, s, run_now)
    return lines


async def stop_schedules(_app: App) -> None:
    """Stop the tasks on shutdown; the file keeps the schedules for the next start."""
    for task in _tasks.values():
        _ = task.cancel()
    _tasks.clear()


# --- What the chat side calls (T18b) ---------------------------------------------------------


def new_id() -> str:
    for _ in range(100):
        if (sid := secrets.token_hex(ID_BYTES)) not in _schedules:
            return sid
    raise RuntimeError("No free schedule ID")  # 16 million IDs for at most 20 schedules


def starting(s: Schedule, now: float) -> Schedule:
    """A new schedule: runs before `now` count as handled, so only later ones run."""
    runs = past_runs(s, now)
    return replace(s, last=runs[-1][0]) if runs else s


def first_run(days: tuple[int, ...], at: tuple[int, int], now: float) -> float:
    """When a schedule set now would first run, for its confirmation."""
    return next_occurrence(starting(Schedule("", "", "", days, at, 0), now), now)


def add_schedule(
    app: App, machine: str, action: str, days: tuple[int, ...], at: tuple[int, int], chat_id: int
) -> tuple[Schedule, bool]:
    """Set and arm a schedule; returns it and whether it was saved. Raises
    ScheduleError (a message for the user) if it's refused."""
    if _disabled:
        raise ScheduleError(DISABLED)
    same = [s for s in _schedules.values() if (s.machine, s.action) == (machine, action)]
    if any((s.days, s.at) == (days, at) for s in same):
        raise ScheduleError("You already have that one.")
    if len(same) >= MAX_PER_MACHINE_ACTION:
        raise ScheduleError(
            f"There are already {MAX_PER_MACHINE_ACTION} repeating {action}s{of(machine)}. "
            + "Delete one in /timers first."
        )
    if len(_schedules) >= MAX_SCHEDULES:
        raise ScheduleError(
            f"There are already {MAX_SCHEDULES} repeating schedules. Delete one in /timers first."
        )
    s = starting(Schedule(new_id(), machine, action, days, at, chat_id), time.time())
    saved = update(s)
    arm(app, s)
    audit_schedule(s, "schedule_set", saved=saved)
    return s, saved


def delete_schedule(sid: str) -> tuple[Schedule | None, bool]:
    """Delete one; returns it (None if there's no such schedule) and whether that was saved."""
    if (s := _schedules.pop(sid, None)) is None:
        return None, True
    if task := _tasks.pop(sid, None):
        _ = task.cancel()
    saved = save_schedules()
    audit_schedule(s, "schedule_deleted", saved=saved)
    return s, saved


def set_skip(app: App, sid: str) -> tuple[date | None, bool]:
    """Skip the next run once; returns its date (None: no such schedule) and
    whether that was saved."""
    if (s := _schedules.get(sid)) is None:
        return None, True
    day, _ = next_run(s, time.time())
    saved = update(replace(s, skip=day))
    arm(app, _schedules[sid])
    audit_schedule(s, "schedule_skipped", day=day.isoformat(), saved=saved)
    return day, saved


def clear_skip(app: App, sid: str) -> tuple[bool, bool]:
    """Undo set_skip(); returns whether there was a skip to undo, and whether saved."""
    if (s := _schedules.get(sid)) is None or s.skip is None:
        return False, True
    saved = update(replace(s, skip=None))
    arm(app, _schedules[sid])
    audit_schedule(s, "schedule_unskipped", saved=saved)
    return True, saved


def get_schedule(sid: str) -> Schedule | None:
    return _schedules.get(sid)


def pending_schedules() -> list[tuple[Schedule, float]]:
    """The schedules and their next run, soonest first (none while they're off)."""
    now = time.time()
    return sorted(((s, next_occurrence(s, now)) for s in _schedules.values()), key=lambda p: p[1])


def schedule_line(s: Schedule, due: float) -> str:
    """ "🔁 Shutdown of desk · weekdays 23:00 · next Mon 12 Oct 23:00"."""
    return f"🔁 {TIMER_TITLE[s.action]}{of(s.machine)} · {s.rule} · next {when_text(due)}"
