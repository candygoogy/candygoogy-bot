"""The configured machines: looking one up by name, picking one in chat, and how
texts refer to it.

With one machine the chat looks exactly as it always has: no picker, no names
("the workstation"), and callback data without a machine. With several, the
power and timer commands take a machine (`/wake desk`), or ask with one button
per machine, and callback data carries the machine's name as its last field.

A name from chat or callback data is only ever compared with the configured
names. Nothing is built from it, so it can't reach a command line (the
configured machine's validated fields do; see system.py).
"""

from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from candygoogy import config
from candygoogy.messages import card, reply
from candygoogy.settings import Machine

# Configured names are at most 16 characters; longer text can't be one.
MAX_NAME_CHARS = 16
# Picker buttons per row.
PICKER_COLUMNS = 2


def all_machines() -> tuple[Machine, ...]:
    return config.MACHINES


def first() -> Machine:
    return config.MACHINES[0]


def several() -> bool:
    return len(config.MACHINES) > 1


def named(text: str) -> Machine | None:
    """The configured machine called `text` (any case), or None."""
    if not text or len(text) > MAX_NAME_CHARS:
        return None
    wanted = text.lower()
    return next((m for m in config.MACHINES if m.name == wanted), None)


def from_field(fields: list[str], index: int) -> Machine | None:
    """The machine named by callback data's field `index`; the first machine if
    the field isn't there (one machine, or a button from before there were
    several); None if it names no configured machine."""
    if len(fields) <= index:
        return first()
    return named(fields[index])


def tagged(data: str, machine: Machine) -> str:
    """Callback data, with ":<machine>" appended when there are several machines."""
    return f"{data}:{machine.name}" if several() else data


def called(machine: Machine) -> str:
    """How a sentence names the machine: "the workstation" with one, else its name."""
    return machine.name if several() else "the workstation"


def capitalised(machine: Machine) -> str:
    """called(), at the start of a sentence."""
    return machine.name if several() else "The workstation"


def names_text() -> str:
    return ", ".join(f"<code>{escape(m.name)}</code>" for m in config.MACHINES)


def picker(command: str) -> tuple[str, InlineKeyboardMarkup]:
    """ "Which machine?" with one pick:<command>:<machine> button per machine."""
    buttons = [
        InlineKeyboardButton(m.name, callback_data=f"pick:{command}:{m.name}")
        for m in config.MACHINES
    ]
    rows = [buttons[i : i + PICKER_COLUMNS] for i in range(0, len(buttons), PICKER_COLUMNS)]
    return card("🖥", "Which machine?", f"For /{command}."), InlineKeyboardMarkup(rows)


def unknown(command: str) -> str:
    """For a name that isn't configured. The name itself isn't repeated."""
    return card(
        "❔",
        "Unknown machine",
        f"Use one of: {names_text()}.",
        f"<i>e.g. /{command} {escape(first().name)}</i>",
    )


async def choose(update: Update, args: list[str], command: str) -> tuple[Machine, list[str]] | None:
    """The machine a command is for, and the arguments after its name. With one
    machine that's always it (arguments untouched, as before). With several, the
    first argument must name one; without arguments the reply asks with buttons.
    None if a reply was sent instead."""
    if not several():
        return first(), args
    if not args:
        await reply(update, *picker(command))
        return None
    if (machine := named(args[0])) is None:
        await reply(update, unknown(command))
        return None
    return machine, args[1:]
