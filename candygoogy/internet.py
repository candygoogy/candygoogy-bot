"""/net: internet outages, checked every minute and reported once it's back."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import cast

from candygoogy.config import INTERNET_CHECK_TARGETS, STATE_DIR, App
from candygoogy.messages import card, duration, notify_allowed
from candygoogy.state import load_json, number, save_json
from candygoogy.tasks import repeat

log = logging.getLogger(__name__)

# INTERNET_CHECK_TARGETS (by default two anycast DNS providers on TCP 443). The
# internet is up if any accepts a connection; each is only tried when the ones
# before it fail, so a check costs about 0.5 KB (some 20 MB a month at one check
# a minute).
TARGETS = INTERNET_CHECK_TARGETS
CONNECT_TIMEOUT_SECONDS = 5.0
CHECK_INTERVAL_SECONDS = 60.0
# One failed check is often a blip. This many in a row make an outage, which
# then counts from the first of them.
FAILURES_FOR_OUTAGE = 2
# /net lists this far back; older outages are forgotten.
HISTORY_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600
STATE_FILE = STATE_DIR / "internet.json"


@dataclass
class Outage:
    start: float  # wall clock, time.time()
    end: float
    reported: bool = False  # the "back" message was sent


@dataclass
class State:
    outages: list[Outage] = field(default_factory=list)  # oldest first
    down_since: float | None = None  # an outage in progress
    failing_since: float | None = None  # first of the current run of failed checks
    failures: int = 0


_state = State()


async def reachable() -> bool:
    for host, port in TARGETS:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError):
            continue
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True
    return False


# --- Saved state ----------------------------------------------------------------
# Outages and one in progress survive a restart, so /net and the daily summary
# keep the week's history and a deploy during an outage doesn't lose it.


def save_state() -> None:
    """A failure only loses history after a restart."""
    data = {
        "down_since": _state.down_since,
        "outages": [[o.start, o.end, o.reported] for o in _state.outages],
    }
    _ = save_json(STATE_FILE, data, "internet outages")


def parse_state(data: object) -> State | None:
    """What save_state() wrote, or None if it isn't that."""
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    down_since, entries = fields.get("down_since"), fields.get("outages")
    if (down_since is not None and number(down_since) is None) or not isinstance(entries, list):
        return None
    outages: list[Outage] = []
    for entry in cast(list[object], entries):
        if not isinstance(entry, list) or len(cast(list[object], entry)) != 3:
            return None
        start, end, reported = cast(list[object], entry)
        start, end = number(start), number(end)
        if start is None or end is None or not isinstance(reported, bool):
            return None
        outages.append(Outage(start, end, reported))
    return State(outages, number(down_since))


def load_state() -> None:
    if (data := load_json(STATE_FILE, "internet outages")) is None:
        return
    if (saved := parse_state(data)) is None:
        log.warning("Ignoring malformed internet outages file %s", STATE_FILE)
        return
    _state.outages, _state.down_since = saved.outages, saved.down_since


# --- Checking -------------------------------------------------------------------


def clock(t: float, now: float) -> str:
    """ "14:02", "yesterday 23:50" or "Mon 5 Oct 14:02", in local time."""
    at, today = time.localtime(t), time.localtime(now)
    hhmm = time.strftime("%H:%M", at)
    days = (date(*today[:3]) - date(*at[:3])).days
    if days == 0:
        return hhmm
    if days == 1:
        return f"yesterday {hhmm}"
    return f"{time.strftime('%a', at)} {at.tm_mday} {time.strftime('%b', at)} {hhmm}"


def span(o: Outage, now: float) -> str:
    """ "14:02–14:19 (17m)"; the end gets its own day only if it differs."""
    same_day = time.localtime(o.start)[:3] == time.localtime(o.end)[:3]
    end = time.strftime("%H:%M", time.localtime(o.end)) if same_day else clock(o.end, now)
    return f"{clock(o.start, now)}–{end} ({duration(o.end - o.start)})"


def back_message(o: Outage, now: float) -> str:
    return card("🌐", "Internet is back", f"It was down {span(o, now)}.")


async def check(app: App) -> None:
    started = time.time()
    online = await reachable()
    now = time.time()
    s = _state
    changed = False
    if not online:
        s.failures += 1
        if s.failing_since is None:
            s.failing_since = started
        if s.down_since is None and s.failures >= FAILURES_FOR_OUTAGE:
            s.down_since = s.failing_since
            log.warning("Internet is down (since %s)", clock(s.down_since, now))
            save_state()
        return
    s.failures, s.failing_since = 0, None
    if s.down_since is not None:
        outage = Outage(s.down_since, now)
        s.outages.append(outage)
        s.down_since = None
        changed = True
        log.info("Internet is back after %s", duration(outage.end - outage.start))
    for outage in s.outages:
        if not outage.reported and await notify_allowed(
            app, back_message(outage, now), "an internet outage report"
        ):  # otherwise it's tried again next check
            outage.reported = True
            changed = True
    if s.outages and s.outages[0].end < now - HISTORY_SECONDS:
        s.outages = [o for o in s.outages if o.end >= now - HISTORY_SECONDS]
        changed = True
    if changed:
        save_state()


async def watch(app: App) -> None:
    """Runs from startup until the bot stops (see app.py)."""
    load_state()
    await repeat(lambda: check(app), CHECK_INTERVAL_SECONDS, "Internet check")


# --- /net and the daily summary ---------------------------------------------------


def since(seconds: float, now: float) -> list[Outage]:
    return [o for o in _state.outages if o.end > now - seconds]


def outages_since(start: float, now: float) -> list[Outage]:
    """Outages that ended after `start`, oldest first, and one in progress (ending now)."""
    found = [o for o in _state.outages if o.end > start]
    if _state.down_since is not None:
        found.append(Outage(_state.down_since, now))
    return found


def total(outages: list[Outage]) -> str:
    return duration(sum(o.end - o.start for o in outages))


async def net_message() -> str:
    online = await reachable()
    now = time.time()
    if _state.down_since is not None:
        # Only seen if Telegram still works while the check targets don't answer.
        status = f"⚠️ Down since {clock(_state.down_since, now)}"
        status += f" ({duration(now - _state.down_since)})."
    elif online:
        status = "✅ Online."
    else:
        status = "⚠️ The last check didn't get through."
    week = since(HISTORY_SECONDS, now)
    if week:
        plural = "s" if len(week) > 1 else ""
        lines = [f"{len(week)} outage{plural} in the last 7 days, {total(week)} in total:"]
        lines += [f"• {span(o, now)}" for o in reversed(week)]
    else:
        lines = ["No outages in the last 7 days."]
    return card(
        "🌐",
        "Internet",
        status,
        *lines,
        f"<i>{targets_text()} tried every minute while the bot runs.</i>",
    )


def targets_text() -> str:
    """ "1.1.1.1 and 9.9.9.9 are", "a, b and c are" or "1.1.1.1 is", escaped."""
    hosts = [escape(f"[{host}]" if ":" in host else host) for host, _ in TARGETS]
    if len(hosts) == 1:
        return f"{hosts[0]} is"
    return f"{', '.join(hosts[:-1])} and {hosts[-1]} are"


def summary_line() -> str:
    """The internet's part of the daily summary: the last 24 hours."""
    now = time.time()
    day = since(DAY_SECONDS, now)
    if not day:
        return "🌐 No internet outages in the last 24h."
    plural = "s" if len(day) > 1 else ""
    listed = ", ".join(span(o, now) for o in day)
    return f"🌐 {len(day)} internet outage{plural} in the last 24h: {listed}."
