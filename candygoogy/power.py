"""Wake, shutdown, reboot and sleep, one at a time per machine, with follow-ups."""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from candygoogy import machines, probes, wol
from candygoogy.config import audit
from candygoogy.history import last_action_name, remember, settle
from candygoogy.messages import Post, Spawn, TextSetter, card, duration, message_setter
from candygoogy.oscommands import commands_for, outcome
from candygoogy.probes import ssh_unavailable_reason
from candygoogy.settings import Machine
from candygoogy.system import RunResult, ssh

log = logging.getLogger(__name__)

# --- Follow-ups ---------------------------------------------------------------
# After a power action, keep probing and update the reply with what actually
# happened. Probing a sleeping PC is safe: its NIC has "Wake on pattern match"
# disabled, so pings and ARP don't wake it (only the magic packet does).


# One power operation at a time per machine: from sending the command until its
# follow-up finishes, other /wake, /sleep, /reboot and /shutdown requests for
# that machine are refused. Another machine has its own lane.
_busy: dict[str, tuple[str, float]] = {}  # machine name -> (action, time.monotonic() at start)

# "{it}" is "it" with one machine, else its name.
BUSY = {
    "wake": "waking {it}",
    "sleep": "putting {it} to sleep",
    "shutdown": "shutting {it} down",
    "reboot": "rebooting {it}",
}


def busy_message(machine: Machine) -> str | None:
    if (busy := _busy.get(machine.name)) is None:
        return None
    name, since = busy
    it = machine.name if machines.several() else "it"
    return card(
        "⏳",
        "Busy",
        f"Already {BUSY[name].format(it=it)} (started {duration(time.monotonic() - since)} ago). "
        + "Try again once it finishes.",
    )


def claim(machine: Machine, name: str) -> None:
    _busy[machine.name] = (name, time.monotonic())


def release(machine: Machine) -> None:
    _ = _busy.pop(machine.name, None)


def release_all() -> None:
    """Free every lane (tests start from a clean slate)."""
    _busy.clear()


async def released_after(machine: Machine, coro: Coroutine[Any, Any, None]) -> None:
    """Run a follow-up, then free the machine's lane even if it fails or is cancelled."""
    try:
        await coro
    finally:
        release(machine)


POLL_SECONDS = 5.0
# Budgets count from just before the power command, so they include its SSH call
# (up to POWER_SSH_TIMEOUT_SECONDS; 10s more than the old 15s, added here too).
WAKE_BUDGET_SECONDS = 180.0
DOWN_BUDGET_SECONDS = 100.0
REBOOT_UP_BUDGET_SECONDS = 250.0


async def wait_until(machine: Machine, awake: bool, budget: float, started: float) -> float | None:
    """Poll until is_awake() == awake; return seconds since `started`, or None on timeout."""
    deadline = time.monotonic() + budget
    while True:
        if await probes.is_awake(machine) == awake:
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(POLL_SECONDS)


def waiting(text: str, what: str) -> str:
    return f"{text}\n\n<i>⏳ {what}</i>"


def gave_up(text: str, what: str) -> str:
    """Keep the original message and add the timeout warning under it."""
    return f"{text}\n\n⚠️ {what} Check /status."


# Once a follow-up confirms the result, that result becomes the headline and
# the "in progress" text is dropped.

# Sleep doesn't reset the last boot time (Windows' LastBootUpTime, Linux's
# /proc/uptime, macOS's kern.boottime), so a boot no older than the wake
# request (plus slack) means a cold boot, and anything older means it resumed.
BOOT_SLACK_SECONDS = 60.0
# Right after a cold boot, ping may answer before sshd has started.
WAKE_DETAILS_TRIES = 3

How = Literal["booted", "resumed"] | None


def woke_how(boot: float | None, woke: float | None, since_wake: float) -> How:
    """ "booted", "resumed", or None if the readings don't say. `woke` (macOS
    only) is the time since the last wake; if it's there it must confirm a resume."""
    if boot is None:
        return None
    if boot <= since_wake + BOOT_SLACK_SECONDS:
        return "booted"
    if woke is not None and woke > since_wake + BOOT_SLACK_SECONDS:
        return None  # up since before the request, but not woken by it either
    return "resumed"


async def how_it_woke(machine: Machine, started: float) -> How:
    """From the machine's last boot (and wake) time; None if SSH never answered."""
    for attempt in range(WAKE_DETAILS_TRIES):
        if attempt:
            await asyncio.sleep(POLL_SECONDS)
        details = await probes.machine_details(machine)
        if details is not None and details.boot is not None:
            return woke_how(details.boot, details.woke, time.monotonic() - started)
    return None


WOKE = {"booted": "Booted in {t}.", "resumed": "Woke from sleep in {t}.", None: "Up in {t}."}


async def follow_wake(machine: Machine, set_text: TextSetter, text: str, started: float) -> None:
    await set_text(waiting(text, "Waiting for it to come up…"))
    up = await wait_until(machine, True, WAKE_BUDGET_SECONDS, started)
    if up is None:
        audit("wake", "not_up", waited=f"{WAKE_BUDGET_SECONDS:.0f}s")
        settle(machine, "wake", confirmed=False)
        await set_text(gave_up(text, f"Still not up after {duration(WAKE_BUDGET_SECONDS)}."))
        return
    how = await how_it_woke(machine, started)
    audit("wake", "up", after=f"{up:.0f}s", how=how or "unknown")
    settle(machine, "wake", confirmed=True)
    await set_text(card("🟢", "Awake", WOKE[how].format(t=duration(up))))


FollowUp = Callable[[Machine, TextSetter, str, float], Coroutine[Any, Any, None]]


def follow_down(action: str, icon: str, title: str, done: str) -> FollowUp:
    """Follow-up for shutdown/sleep: wait until it stops answering.

    `done` is the confirmation detail, with "{t}" replaced by the elapsed time.
    """

    async def follow(machine: Machine, set_text: TextSetter, text: str, started: float) -> None:
        await set_text(waiting(text, "Waiting for it to go quiet…"))
        down = await wait_until(machine, False, DOWN_BUDGET_SECONDS, started)
        if down is None:
            audit(action, "still_up", waited=f"{DOWN_BUDGET_SECONDS:.0f}s")
            settle(machine, action, confirmed=False)
            await set_text(
                gave_up(text, f"Still responding after {duration(DOWN_BUDGET_SECONDS)}.")
            )
            return
        audit(action, "down", after=f"{down:.0f}s")
        settle(machine, action, confirmed=True)
        await set_text(card(icon, title, done.format(t=duration(down))))

    return follow


async def follow_reboot(machine: Machine, set_text: TextSetter, text: str, started: float) -> None:
    await set_text(waiting(text, "Waiting for it to go down…"))
    down = await wait_until(machine, False, DOWN_BUDGET_SECONDS, started)
    if down is None:
        audit("reboot", "still_up", waited=f"{DOWN_BUDGET_SECONDS:.0f}s")
        settle(machine, "reboot", confirmed=False)
        await set_text(
            gave_up(
                text,
                f"Still responding after {duration(DOWN_BUDGET_SECONDS)}; "
                + "it may not be rebooting.",
            )
        )
        return
    went_down = f"⚫ Went down after {duration(down)}."
    await set_text(waiting(f"{text}\n\n{went_down}", "Waiting for it to come back…"))
    up = await wait_until(machine, True, REBOOT_UP_BUDGET_SECONDS, started)
    if up is None:
        audit("reboot", "not_back", down_after=f"{down:.0f}s")
        settle(machine, "reboot", confirmed=False)
        await set_text(
            gave_up(
                f"{text}\n\n{went_down}",
                f"Not back after {duration(REBOOT_UP_BUDGET_SECONDS)}.",
            )
        )
        return
    audit("reboot", "back_up", down_after=f"{down:.0f}s", up_after=f"{up:.0f}s")
    settle(machine, "reboot", confirmed=True)
    await set_text(
        card("🟢", "Back up", f"Rebooted in {duration(up)} (went down after {duration(down)}).")
    )


# --- Wake-on-LAN --------------------------------------------------------------

# A magic packet is one UDP broadcast with no delivery guarantee, so send a few.
# Extra packets are harmless to a PC that is already booting.
WOL_PACKETS = 3
WOL_INTERVAL_SECONDS = 1.0


async def send_magic_packet(machine: Machine) -> RunResult:
    return await wol.send_magic_packet(machine)


async def send_remaining_packets(machine: Machine) -> None:
    """Packets 2..WOL_PACKETS, spaced with asyncio.sleep so nothing blocks."""
    for n in range(2, WOL_PACKETS + 1):
        await asyncio.sleep(WOL_INTERVAL_SECONDS)
        result = await send_magic_packet(machine)
        if not result.ok:  # the first one already went out; keep going
            log.warning("Magic packet %d/%d failed (exit %d)", n, WOL_PACKETS, result.code)
            audit("wake", "extra_packet_failed", packet=n)


async def wake_in_background(
    machine: Machine, set_text: TextSetter, text: str, started: float
) -> None:
    await send_remaining_packets(machine)
    await follow_wake(machine, set_text, text, started)


async def start_wake(machine: Machine, post: Post, spawn: Spawn) -> None:
    """Shared by /wake and the wake timer."""
    if busy := busy_message(machine):
        _ = await post(busy)
        return
    if await probes.is_awake(machine):
        audit("wake", "skipped_already_on")
        _ = await post(card("🟢", "Already on", "No magic packet sent."))
        return
    claim(machine, "wake")
    handed_off = False
    try:
        started = time.monotonic()
        # The first packet is sent here so a real failure (e.g. no network) is
        # reported right away instead of from the background.
        result = await send_magic_packet(machine)
        if not result.ok:
            audit("wake", "failed", code=result.code, timed_out=result.timed_out)
            _ = await post(result.describe("Wake-on-LAN"))
            return
        audit("wake", "packet_sent")
        lines = [f"Sending {WOL_PACKETS} magic packets, {WOL_INTERVAL_SECONDS:g}s apart."]
        if not commands_for(machine.os).wakes_from_off and last_action_name(machine) == "shutdown":
            lines.append("<i>Macs usually wake only from sleep, not after a shutdown.</i>")
        remember(machine, "wake")
        text = card("⚡", "Waking it up", *lines)
        if message := await post(text):
            # The remaining packets and the follow-up run in the background so
            # the bot keeps answering other commands (it handles updates one at
            # a time). The lane stays claimed until that task finishes, so a
            # second /wake meanwhile is refused instead of sending 3 more.
            follow = wake_in_background(machine, message_setter(message), text, started)
            spawn(released_after(machine, follow))
            handed_off = True
    finally:
        if not handed_off:
            release(machine)


# --- Power actions (confirmed with buttons) -----------------------------------

# How long a power command's SSH call may take. A session that freezes (the
# machine sleeps, or goes down before answering) normally ends by ssh's own
# keepalive: ConnectTimeout 5s, then ServerAliveInterval 5s x CountMax 2, so
# within about 20s, as exit 255 "Timeout, server ... not responding". This is
# longer, so that is the usual path; if the bot's timeout still hits first, both
# mean the same (oscommands.outcome).
POWER_SSH_TIMEOUT_SECONDS = 25.0
PowerName = Literal["shutdown", "reboot", "sleep"]
# (message, whether the action started and is worth following).
Result = tuple[str, bool]
# What each action says once started, and its name in failure messages.
STARTED: dict[PowerName, tuple[str, str, str]] = {
    "shutdown": ("🔌", "Shutting down", "Use /wake to power it back on."),
    "reboot": ("🔄", "Rebooting", ""),
    "sleep": ("😴", "Going to sleep", ""),
}
TITLE: dict[PowerName, str] = {"shutdown": "Shutdown", "reboot": "Reboot", "sleep": "Sleep"}


async def perform(machine: Machine, name: PowerName) -> Result:
    """Send the action's command and say what its result means (oscommands.outcome)."""
    commands = commands_for(machine.os)
    # Each action spelled out, so every ssh() call names a literal action (the
    # subprocess invariant test checks that).
    match name:
        case "shutdown":
            result = await ssh(machine, "shutdown", timeout=POWER_SSH_TIMEOUT_SECONDS)
        case "reboot":
            result = await ssh(machine, "reboot", timeout=POWER_SSH_TIMEOUT_SECONDS)
        case "sleep":
            result = await ssh(machine, "sleep", timeout=POWER_SSH_TIMEOUT_SECONDS)
    icon, title, detail = STARTED[name]
    started_lines = [detail] if detail else []
    match outcome(commands, name, result):
        case "started":
            return card(icon, title, *started_lines), True
        case "unconfirmed":
            note = "<i>The connection closed before it confirmed; the follow-up will tell.</i>"
            return card(icon, title, *started_lines, note), True
        case "no_answer":
            return card(
                icon, title, "<i>No answer over SSH; checking whether it went down…</i>"
            ), True
        case "asleep":
            # The sleep call returns only after the machine wakes, so the session froze.
            return card(
                "😴",
                "Going to sleep",
                "The SSH session froze, which is what happens when the PC falls asleep.",
            ), True
        case "returned_early":
            return card(
                "⚠️",
                "Sleep returned early",
                f"{machines.capitalised(machine)} may not have slept. Check /status.",
            ), False
        case "denied":
            return card("🔒", f"{TITLE[name]} not allowed", commands.denial_hint), False
        case "host_key":
            return card(
                "🔑",
                "SSH host key changed",
                "The machine's SSH host key isn't the one the bot knows. If that's "
                + "expected (e.g. after a reinstall), remove the "
                + f"<code>candygoogy-{machine.name}</code> entry from the bot's known_hosts.",
            ), False
        case "failed":
            return result.describe(TITLE[name]), False


@dataclass(frozen=True)
class PowerAction:
    question: str  # as asked with one machine; question() names the machine with several
    button: str
    progress: str
    follow_up: FollowUp


POWER_ACTIONS: dict[str, PowerAction] = {
    "shutdown": PowerAction(
        "🔌 <b>Shut down the workstation?</b>", "🔌 Shut down", "⏳ Shutting down…",
        follow_down("shutdown", "⚫", "Off", "Shut down in {t}. Use /wake to power it back on."),
    ),
    "reboot": PowerAction(
        "🔄 <b>Reboot the workstation?</b>", "🔄 Reboot", "⏳ Rebooting…",
        follow_reboot,
    ),
    "sleep": PowerAction(
        "😴 <b>Put the workstation to sleep?</b>", "😴 Sleep", "⏳ Going to sleep…",
        follow_down("sleep", "🌙", "Asleep", "Fell asleep in {t}. Use /wake to wake it."),
    ),
}  # fmt: skip
POWER_NAMES: dict[str, PowerName] = {"shutdown": "shutdown", "reboot": "reboot", "sleep": "sleep"}


def question(name: str, machine: Machine) -> str:
    """The confirmation prompt's question, e.g. "Shut down the workstation?"."""
    return POWER_ACTIONS[name].question.replace("the workstation", machines.called(machine))


async def run_power_action(
    machine: Machine, name: str, set_text: TextSetter, spawn: Spawn, event: str = "confirmed"
) -> None:
    """Shared by a confirmed button and the shutdown timer."""
    # Something else may have started while the prompt was open or the timer ran.
    if busy := busy_message(machine):
        audit(name, "blocked_busy")
        await set_text(busy)
        return

    action = POWER_ACTIONS[name]
    claim(machine, name)
    handed_off = False
    try:
        await set_text(action.progress)
        # Re-check: it may have gone to sleep or off in the meantime.
        if reason := await ssh_unavailable_reason(machine):
            audit(name, "blocked_unreachable")
            await set_text(reason)
            return
        audit(name, event)
        started = time.monotonic()
        text, started_ok = await perform(machine, POWER_NAMES[name])
        audit(name, "sent" if started_ok else "failed")
        await set_text(text)
        if started_ok:
            remember(machine, name)
            spawn(released_after(machine, action.follow_up(machine, set_text, text, started)))
            handed_off = True
    finally:
        if not handed_off:
            release(machine)
