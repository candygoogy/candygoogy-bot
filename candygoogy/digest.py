"""/summary and the daily summary: the homelab, internet, the PC and today's timers."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from html import escape

from candygoogy import homelab, internet, machines, probes, schedules
from candygoogy.config import DIGEST_AT, App
from candygoogy.messages import card, notify_allowed
from candygoogy.timers import next_at, pending_timers, timer_line

log = logging.getLogger(__name__)

# Longest single asyncio.sleep while waiting, so wall-clock jumps are noticed.
TICK_SECONDS = 60.0
# If Telegram can't be reached at DIGEST_TIME (e.g. the internet is down), keep
# trying this often, for up to an hour; after that the summary is stale anyway.
RETRY_SECONDS = 60.0
GIVE_UP_SECONDS = 3600.0
# Timers and repeating runs due within this long are listed.
TIMERS_AHEAD_SECONDS = 24 * 3600


async def digest_message() -> str:
    every = machines.all_machines()
    health, awake = await asyncio.gather(
        homelab.read_health(), asyncio.gather(*(probes.is_awake(m) for m in every))
    )
    now = time.time()
    lines = homelab.summary_lines(health, now)
    lines.append(internet.summary_line())
    if machines.several():  # one line per machine, by name
        for machine, on in zip(every, awake, strict=True):
            name = escape(machine.name)
            lines.append(f"🟢 {name} is on." if on else f"⚫ {name} is off or asleep.")
    else:
        lines.append("🟢 The PC is on." if awake[0] else "⚫ The PC is off or asleep.")
    lines += [timer_line(t) for t in pending_timers() if t.due - now <= TIMERS_AHEAD_SECONDS]
    lines += [
        schedules.schedule_line(s, due)
        for s, due in schedules.pending_schedules()
        if due - now <= TIMERS_AHEAD_SECONDS
    ]
    lines.append("<i>Details: /homelab · /net · /status</i>")
    return card("🗓", "Daily summary", *lines)


async def send_retrying(app: App, make_text: Callable[[], Awaitable[str]], what: str) -> None:
    """Send a scheduled message, retrying while Telegram can't be reached."""
    give_up = time.monotonic() + GIVE_UP_SECONDS
    while True:
        if await notify_allowed(app, await make_text(), what):
            return
        if time.monotonic() + RETRY_SECONDS > give_up:
            log.warning("Gave up sending %s", what)
            return
        await asyncio.sleep(RETRY_SECONDS)


async def send_digest(app: App) -> None:
    await send_retrying(app, digest_message, "the daily summary")


async def every(
    next_due: Callable[[float], float], send: Callable[[], Awaitable[None]], what: str
) -> None:
    """Run send() at every next_due(now) until cancelled; a failure is logged."""
    while True:
        due = next_due(time.time())
        while (left := due - time.time()) > 0:
            await asyncio.sleep(min(left, TICK_SECONDS))
        try:
            await send()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s failed", what)


async def daily(app: App) -> None:
    """Runs from startup until the bot stops (see app.py)."""
    if DIGEST_AT is None:
        log.info("Daily summary is off (DIGEST_TIME)")
        return
    at = DIGEST_AT
    await every(lambda now: next_at(*at, now), lambda: send_digest(app), "Daily summary")
