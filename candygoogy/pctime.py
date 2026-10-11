"""When each machine was on: checked every few minutes, for the weekly report."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import cast

from candygoogy import machines, probes
from candygoogy.config import STATE_DIR, App
from candygoogy.history import last_action_name
from candygoogy.oscommands import commands_for
from candygoogy.settings import Machine
from candygoogy.state import load_json, number, save_json
from candygoogy.tasks import repeat

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300.0
# Seen on again within this long: the same session. A longer gap (the PC was
# off or asleep, or the bot wasn't running) starts a new one.
SAME_SESSION_SECONDS = 2.5 * CHECK_INTERVAL_SECONDS
# Sessions that ended longer ago than this are forgotten.
HISTORY_SECONDS = 8 * 24 * 3600
STATE_FILE = STATE_DIR / "pc_time.json"


@dataclass
class Session:
    start: float  # first check that found it on (wall clock)
    end: float  # last check that found it on


@dataclass
class State:
    since: float | None = None  # the first check ever, so the report knows what it can't know
    sessions: list[Session] = field(default_factory=list)  # oldest first


_states: dict[str, State] = {}  # machine name -> its on-time


def state_of(machine: Machine) -> State:
    return _states.setdefault(machine.name, State())


async def is_on(machine: Machine) -> bool:
    """Like probes.is_awake(), but the SSH port is only tried if ping gets no
    answer, so a PC that's on doesn't log an SSH connection every few minutes.
    A Mac put to sleep by the bot gets ping only: traffic to its SSH port can
    wake it through a Bonjour Sleep Proxy, and ping doesn't."""
    if await probes.ping(machine):
        return True
    if not commands_for(machine.os).wakes_from_off and last_action_name(machine) == "sleep":
        return False
    return await probes.ssh_port_open(machine)


# --- Saved state ----------------------------------------------------------------


def save_state() -> None:
    """A failure only loses history after a restart. {machine: {since, sessions}}."""
    data = {
        name: {"since": s.since, "sessions": [[x.start, x.end] for x in s.sessions]}
        for name, s in _states.items()
        if s.since is not None
    }
    _ = save_json(STATE_FILE, data, "the PCs' on-time")


def parse_state(data: object) -> State | None:
    """One machine's {since, sessions}, or None if it isn't that."""
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    since, entries = number(fields.get("since")), fields.get("sessions")
    if since is None or not isinstance(entries, list):
        return None
    found: list[Session] = []
    for entry in cast(list[object], entries):
        if not isinstance(entry, list) or len(cast(list[object], entry)) != 2:
            return None
        start, end = (number(x) for x in cast(list[object], entry))
        if start is None or end is None:
            return None
        found.append(Session(start, end))
    return State(since, found)


def load_state() -> None:
    """The file from before several machines (one {since, sessions}) becomes the
    first machine's. Machines no longer configured are dropped."""
    if (data := load_json(STATE_FILE, "PC on-time")) is None:
        return
    if not isinstance(data, dict):
        log.warning("Ignoring malformed PC on-time file %s", STATE_FILE)
        return
    fields = cast(dict[str, object], data)
    if "since" in fields and not isinstance(fields["since"], dict):
        # The old format: one {since, sessions}, for what is now the first machine.
        if (single := parse_state(fields)) is None:
            log.warning("Ignoring malformed PC on-time file %s", STATE_FILE)
            return
        _states.clear()
        _states[machines.first().name] = single
        save_state()  # in the new format from now on
        return
    found: dict[str, State] = {}
    for name, entry in cast(dict[str, object], data).items():
        if machines.named(name) is None:
            continue
        if (saved := parse_state(entry)) is None:
            log.warning("Ignoring a malformed entry in the PC on-time file %s", STATE_FILE)
            continue
        found[name] = saved
    _states.clear()
    _states.update(found)


# --- Checking -------------------------------------------------------------------


def record(machine: Machine, on: bool, now: float) -> bool:
    """Add one check's result; True if the state changed."""
    state = state_of(machine)
    first = state.since is None
    if first:
        state.since = now
    if on:
        found = state.sessions
        if found and now - found[-1].end <= SAME_SESSION_SECONDS:
            found[-1].end = now
        else:
            found.append(Session(now, now))
        found[:] = [s for s in found if s.end >= now - HISTORY_SECONDS]
    return on or first


async def check() -> None:
    """One check of every machine, in parallel."""
    targets = machines.all_machines()
    results = await asyncio.gather(*(is_on(m) for m in targets))
    now = time.time()
    changed = [record(m, on, now) for m, on in zip(targets, results, strict=True)]
    if any(changed):
        save_state()


async def watch(_app: App) -> None:
    """Runs from startup until the bot stops (see app.py)."""
    load_state()
    await repeat(check, CHECK_INTERVAL_SECONDS, "PC on-time check")


# --- The weekly report ----------------------------------------------------------


def tracked_since(machine: Machine) -> float | None:
    return state_of(machine).since


def on_seconds(machine: Machine, start: float, end: float) -> float:
    """How long the machine was on between start and end. Each check that found it
    on counts until the next one, so this is right to within CHECK_INTERVAL_SECONDS."""
    total = 0.0
    for s in state_of(machine).sessions:
        until = min(s.end + CHECK_INTERVAL_SECONDS, end)
        total += max(0.0, until - max(s.start, start))
    return total
