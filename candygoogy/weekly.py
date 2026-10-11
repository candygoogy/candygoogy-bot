"""/week and the weekly report: internet, backups, homelab problems and PC use over 7 days."""

import logging
import math
import time
from html import escape
from itertools import pairwise

from candygoogy import homelab, internet, machines, pctime
from candygoogy.config import REPORT_AT, App
from candygoogy.digest import every, send_retrying
from candygoogy.messages import card, duration
from candygoogy.settings import Machine

log = logging.getLogger(__name__)


def next_weekly(weekday: int, hour: int, minute: int, now: float) -> float:
    """The next time after `now` that it's that weekday and hour:minute, local time."""
    today = time.localtime(now)

    def days_ahead(days: int) -> float:
        # Like next_at(): mktime rolls the day over and finds the DST offset itself.
        return time.mktime(
            (today.tm_year, today.tm_mon, today.tm_mday + days, hour, minute, 0, 0, 0, -1)
        )

    due = days_ahead((weekday - today.tm_wday) % 7)
    return due if due > now else days_ahead((weekday - today.tm_wday) % 7 + 7)


def day_starts(now: float) -> list[float]:
    """Midnight of each of the 7 days that end today, and then tomorrow's."""
    today = time.localtime(now)
    return [
        time.mktime((today.tm_year, today.tm_mon, today.tm_mday + days, 0, 0, 0, 0, 0, -1))
        for days in range(-6, 2)
    ]


# --- Wording ----------------------------------------------------------------------
# The report covers 7 calendar days, so a weekday name alone says which one.


def weekday(t: float) -> str:
    return time.strftime("%a", time.localtime(t))


def date_label(t: float) -> str:
    """ "Mon 5 Oct"."""
    at = time.localtime(t)
    return f"{time.strftime('%a', at)} {at.tm_mday} {time.strftime('%b', at)}"


def at(t: float, week_start: float) -> str:
    """ "Tue 14:02", or "Mon 28 Sep 14:02" from before the week."""
    hhmm = time.strftime("%H:%M", time.localtime(t))
    return f"{weekday(t) if t >= week_start else date_label(t)} {hhmm}"


def span(start: float, end: float, week_start: float) -> str:
    """ "Tue 14:02–14:31 (29m)", or "Tue 23:50–Wed 00:20 (30m)"."""
    same_day = time.localtime(start)[:3] == time.localtime(end)[:3]
    until = time.strftime("%H:%M", time.localtime(end)) if same_day else at(end, week_start)
    return f"{at(start, week_start)}–{until} ({duration(end - start)})"


def minutes(seconds: float) -> str:
    """On-time to the minute: "2h 5m", or "–" for none."""
    return duration(round(seconds / 60) * 60) if seconds >= 60 else "–"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --- Sections -----------------------------------------------------------------------


def internet_lines(start: float, now: float) -> list[str]:
    outages = internet.outages_since(start, now)
    if not outages:
        return ["🌐 Internet: no outages."]
    down = sum(o.end - max(o.start, start) for o in outages)
    # Rounded down, so any outage at all shows as under 100%.
    online = math.floor(1000 * (1 - down / (now - start))) / 10
    if len(outages) == 1:
        listed = span(outages[0].start, outages[0].end, start)
    else:
        longest = max(outages, key=lambda o: o.end - o.start)
        listed = f"{duration(down)} in total; the longest {span(longest.start, longest.end, start)}"
    return [
        f"🌐 Internet: online {online:.1f}% of the time. {plural(len(outages), 'outage')}: {listed}."
    ]


def snapshot_started(name: str) -> float:
    """Your backup job names a snapshot after when it started, in local time."""
    return time.mktime(time.strptime(name, "%Y%m%d-%H%M%S"))


def backup_lines(days: list[float], now: float) -> list[str]:
    if not homelab.backups_monitored():  # off: no section at all
        return []
    start = days[0]
    try:
        snapshots = homelab.backup_snapshots()
    except OSError:
        return ["⚠️ Couldn't read the backups folder."]
    # (name, when it completed), newest first
    done = [
        (s.name, s.finished) for s in snapshots if s.finished is not None and s.finished >= start
    ]
    if not done:
        return ["⚠️ No backup completed this week."]
    last, finished = done[0]
    try:
        last_size = f" ({homelab.backup_size(homelab.snapshot_size(last))})"
    except OSError:
        last_size = ""
    lines = [
        f"💾 {plural(len(done), 'backup')} completed, "
        + f"the last {duration(now - finished)} ago{last_size}."
    ]
    # Days before today: today's may not have run yet.
    missing = [
        weekday(day)
        for day, next_day in pairwise(days[:7])
        if not any(day <= t < next_day for _, t in done)
    ]
    if missing:
        lines.append(f"⚠️ None completed on {', '.join(missing)}.")
    unfinished = [
        f"<code>{escape(s.name)}</code>"
        for s in snapshots
        if s.finished is None and snapshot_started(s.name) >= start
    ]
    if unfinished:
        lines.append(f"⚠️ Didn't finish (failed, or still running): {', '.join(unfinished)}.")
    return lines


def homelab_lines(start: float, now: float) -> list[str]:
    parts: list[str] = []
    if (uptime := homelab.read_uptime()) is not None:
        booted = now - uptime
        parts.append(
            f"rebooted {at(booted, start)}" if booted >= start else f"up {duration(uptime)}"
        )
    incidents = homelab.incidents_since(start)
    parts.append(f"{plural(len(incidents), 'problem')}:" if incidents else "no problems.")
    lines = [f"🖥 Homelab: {', '.join(parts)}"]
    for i in incidents:
        if i.end is None:
            lines.append(f"• {i.title}: since {at(i.start, start)}, still open.")
        else:
            lines.append(f"• {i.title}: {span(i.start, i.end, start)}.")
    return lines


def pc_lines(days: list[float], now: float) -> list[str]:
    """Each machine's on-time; with several, each line names its machine."""
    lines: list[str] = []
    for machine in machines.all_machines():
        lines += machine_lines(machine, days, now)
    return lines


def machine_lines(machine: Machine, days: list[float], now: float) -> list[str]:
    several = machines.several()
    the = escape(machine.name) if several else "The PC"
    on = escape(machine.name) if several else "PC"
    since = pctime.tracked_since(machine)
    if since is None:
        return [f"<i>💻 {the}'s on-time isn't tracked yet.</i>"]
    per_day = [
        pctime.on_seconds(machine, day, min(next_day, now)) for day, next_day in pairwise(days)
    ]
    if sum(per_day) < 60:
        lines = [f"💻 {the} wasn't on."]
    else:
        listed = " · ".join(
            f"{weekday(day)} {minutes(s)}" for day, s in zip(days[:7], per_day, strict=True)
        )
        lines = [f"💻 {on} on {minutes(sum(per_day))}: {listed}."]
    if since > days[0]:
        lines.append(f"<i>{the} is tracked since {at(since, days[0])}.</i>")
    return lines


def week_message(now: float) -> str:
    days = day_starts(now)
    return card(
        "📅",
        "Weekly report",
        f"<i>{date_label(days[0])} – {date_label(now)}</i>",
        *internet_lines(days[0], now),
        *backup_lines(days, now),
        *homelab_lines(days[0], now),
        *pc_lines(days, now),
        "<i>Details: /net · /homelab · /status</i>",
    )


# --- Sending ------------------------------------------------------------------------


async def report_text() -> str:
    return week_message(time.time())


async def send_report(app: App) -> None:
    await send_retrying(app, report_text, "the weekly report")


async def weekly(app: App) -> None:
    """Runs from startup until the bot stops (see app.py)."""
    if REPORT_AT is None:
        log.info("Weekly report is off (WEEKLY_REPORT)")
        return
    when = REPORT_AT
    await every(lambda now: next_weekly(*when, now), lambda: send_report(app), "Weekly report")
