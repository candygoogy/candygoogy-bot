"""The last power action per machine, shown by /status and kept across restarts."""

import logging
import time
from dataclasses import asdict, dataclass
from typing import cast

from candygoogy import machines
from candygoogy.config import STATE_DIR
from candygoogy.messages import duration
from candygoogy.settings import Machine
from candygoogy.state import load_json, save_json

log = logging.getLogger(__name__)

# --- Last action -------------------------------------------------------------
# The last power action the bot performed on each machine, shown by /status when
# it can't tell asleep from off. Saved next to timers.json, so a restart (e.g. a
# deploy) doesn't forget that a PC was put to sleep rather than shut down.

HISTORY_FILE = STATE_DIR / "last_action.json"


@dataclass(frozen=True)
class LastAction:
    name: str  # "wake", "sleep", "shutdown" or "reboot"
    at: float  # wall-clock time.time()
    confirmed: bool | None = None  # None while the follow-up is still watching


_last_actions: dict[str, LastAction] = {}  # machine name -> its last action

DID = {
    "wake": "woke it",
    "sleep": "put it to sleep",
    "shutdown": "shut it down",
    "reboot": "rebooted it",
}
CONFIRMED = {
    "wake": "came up",
    "sleep": "went quiet",
    "shutdown": "went quiet",
    "reboot": "came back",
}


def save_last_actions() -> None:
    """A failure only costs the line after a restart, so it's just logged.
    The file is {machine: {name, at, confirmed}}."""
    data = {machine: asdict(action) for machine, action in _last_actions.items()}
    _ = save_json(HISTORY_FILE, data, "the last action")


def parse_action(data: object) -> LastAction | None:
    fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
    name, at, confirmed = fields.get("name"), fields.get("at"), fields.get("confirmed")
    if (
        name not in DID
        or isinstance(at, bool)
        or not isinstance(at, int | float)
        or not isinstance(confirmed, bool | None)
    ):
        return None
    # A follow-up a restart cut short counts as not confirmed.
    return LastAction(cast(str, name), float(at), confirmed is True)


def load_last_action() -> None:
    """Called once at startup. The file from before several machines (one action,
    no machine) becomes the first machine's. Machines no longer configured are dropped."""
    if (data := load_json(HISTORY_FILE, "last-action")) is None:
        return
    if not isinstance(data, dict):
        log.warning("Ignoring malformed last-action file %s", HISTORY_FILE)
        return
    fields = cast(dict[str, object], data)
    if "at" in fields and not isinstance(fields["at"], dict):
        # The old format: one action, for what is now the first machine.
        if (single := parse_action(fields)) is None:
            log.warning("Ignoring malformed last-action file %s", HISTORY_FILE)
            return
        _last_actions.clear()
        _last_actions[machines.first().name] = single
        save_last_actions()  # in the new format from now on
        return
    found: dict[str, LastAction] = {}
    for machine, entry in cast(dict[str, object], data).items():
        if machines.named(machine) is None:
            continue  # not configured any more
        if (action := parse_action(entry)) is None:
            log.warning("Ignoring a malformed entry in the last-action file %s", HISTORY_FILE)
            continue
        found[machine] = action
    _last_actions.clear()
    _last_actions.update(found)


def remember(machine: Machine, name: str) -> None:
    _last_actions[machine.name] = LastAction(name, time.time())
    save_last_actions()


def settle(machine: Machine, name: str, *, confirmed: bool) -> None:
    """Record the follow-up's verdict, unless another action happened since."""
    last = _last_actions.get(machine.name)
    if last is not None and last.name == name:
        _last_actions[machine.name] = LastAction(name, last.at, confirmed)
        save_last_actions()


def last_action_name(machine: Machine) -> str | None:
    """The machine's last power action ("wake", "sleep", "shutdown", "reboot"), if any."""
    last = _last_actions.get(machine.name)
    return last.name if last is not None else None


def last_action_line(machine: Machine) -> str:
    if (a := _last_actions.get(machine.name)) is None:
        return "<i>No power action via the bot on record.</i>"
    ago = duration(time.time() - a.at)
    at = time.strftime("%H:%M", time.localtime(a.at))
    if a.confirmed is None:
        outcome = "still checking"
    elif a.confirmed:
        outcome = f"it {CONFIRMED[a.name]}"
    else:
        outcome = "not confirmed"
    return f"<i>Last bot action: {DID[a.name]} at {at}, {ago} ago ({outcome}).</i>"
