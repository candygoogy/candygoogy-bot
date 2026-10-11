"""Telegram command and button handlers."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from html import escape

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ContextTypes,
)

from candygoogy import machines
from candygoogy.about import about
from candygoogy.auth import authorized, is_authorized
from candygoogy.config import CONFIRM_TTL_SECONDS, Handler, audit
from candygoogy.digest import digest_message
from candygoogy.homelab import (
    HOMELAB_REBOOT,
    REBOOT_BUTTON,
    homelab_message,
    read_reboot_required,
    reboot_refusal,
    request_reboot,
)
from candygoogy.internet import net_message
from candygoogy.keyboard import KEYBOARD, KEYBOARD_COMMANDS, MENU
from candygoogy.logs import logs, on_logs_button
from candygoogy.messages import (
    background_spawner,
    card,
    edit,
    query_setter,
    reply,
    typing,
    update_poster,
)
from candygoogy.power import (
    POWER_ACTIONS,
    busy_message,
    question,
    run_power_action,
    start_wake,
)
from candygoogy.probes import (
    machine_status,
    machine_summary,
    sessions_warning,
    signed_in_sessions,
    ssh_unavailable_reason,
)
from candygoogy.settings import Machine, is_number
from candygoogy.timers import (
    confirm_schedule,
    on_timer_button,
    on_when_button,
    pending_timers,
    schedule_for,
    schedule_shutdown,
    schedule_wake,
    stop_asking_when,
    timer_line,
    timers,
)
from candygoogy.timers import (
    on_text as on_when_text,
)
from candygoogy.weekly import week_message

log = logging.getLogger(__name__)

# --- Commands -----------------------------------------------------------------

# (command, emoji, description). Drives /start and Telegram's "/" menu.
HELP = [
    ("wake", "⚡", "Power on (Wake-on-LAN)"),
    ("status", "📊", "Awake, asleep or off?"),
    ("homelab", "🖥", "Homelab disks, services, containers, backups"),
    ("net", "🌐", "Internet outages in the last 7 days"),
    ("summary", "🗓", "The daily summary, now"),
    ("week", "📅", "The weekly report, now"),
    ("sleep", "😴", "Sleep (S3)"),
    ("reboot", "🔄", "Restart"),
    ("shutdown", "🔌", "Power off"),
    ("schedule_shutdown", "⏰", "Power off later (asks when)"),
    ("schedule_wake", "⏰", "Power on later (asks when)"),
    ("timers", "📋", "Scheduled timers, cancel or postpone"),
    ("logs", "📜", "Recent logs: all, audit, errors"),
    ("about", "ℹ️", "Version, uptime, where timers are saved"),
]


# Commands a "menu:<name>" inline button may run (/timers' schedule buttons, and
# the inline menu older /start messages had). Never taken from the data alone.
MENU_COMMANDS = {name for row in MENU for name, _ in row} | {
    "schedule_shutdown",
    "schedule_wake",
}


@authorized
async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"{emoji} /{name} · {text}" for name, emoji, text in HELP]
    if machines.several():
        example = escape(machines.first().name)
        lines += [
            "",
            f"<i>Machines: {machines.names_text()}. Power and timer commands take "
            + f"one, e.g. /wake {example}; without it, I'll ask.</i>",
        ]
    await reply(
        update,
        card(
            "🍬",
            "candygoogy · workstation remote",
            "",
            *lines,
            "",
            "<i>Tap a button below or send a command. Power commands ask for confirmation "
            + "first.</i>",
        ),
        KEYBOARD,
    )


@authorized
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on the keyboard (its label arrives as text), or a typed answer to "when?"."""
    message = update.effective_message
    name = KEYBOARD_COMMANDS.get(message.text.strip()) if message and message.text else None
    if name is None:
        await on_when_text(update, context)
        return
    stop_asking_when()  # they moved on; a later message isn't an answer any more
    await COMMANDS[name](update, context)


async def on_menu_button(
    query: CallbackQuery, update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    """Run the command a menu button names, replying under the menu."""
    if name not in MENU_COMMANDS:
        _ = await query.answer()
        return
    if update.effective_message is None:  # Telegram no longer lets us reply to it
        _ = await query.answer("This menu is too old. Send /start again.")
        return
    _ = await query.answer()
    await COMMANDS[name](update, context)


async def wake_machine(
    update: Update, context: ContextTypes.DEFAULT_TYPE, machine: Machine, _args: list[str]
) -> None:
    await typing(update)
    await start_wake(machine, update_poster(update), background_spawner(context, update))


@authorized
async def wake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if chosen := await machines.choose(update, context.args or [], "wake"):
        await wake_machine(update, context, *chosen)


def refresh_button(data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data=data)]])


def checked(text: str) -> str:
    """A card that can be refreshed says when it was read, so a tap visibly does something."""
    return f"{text}\n\n<i>Checked {time.strftime('%H:%M:%S')}</i>"


REFRESH_BUTTON = refresh_button("status")


async def machine_card(machine: Machine) -> tuple[str, InlineKeyboardMarkup]:
    """One machine's full /status card. With several machines it names the
    machine, lists only its timers and has a way back to the overview."""
    text = await machine_status(machine)
    pending = pending_timers()
    if machines.several():
        text = f"🖥 <b>{escape(machine.name)}</b>\n{text}"
        pending = [t for t in pending if t.machine == machine.name]
    if pending:
        text += "\n\n" + "\n".join(timer_line(t) for t in pending)
    if not machines.several():
        return checked(text), REFRESH_BUTTON
    buttons = [
        InlineKeyboardButton("🔄 Refresh", callback_data=f"status:{machine.name}"),
        InlineKeyboardButton("◀ All machines", callback_data="status"),
    ]
    return checked(text), InlineKeyboardMarkup([buttons])


async def overview_card() -> tuple[str, InlineKeyboardMarkup]:
    """Several machines: one line each (ping, SSH port and ARP only), with a
    button per machine for its full card."""
    every = machines.all_machines()
    summaries = await asyncio.gather(*(machine_summary(m) for m in every))
    lines: list[str] = []
    for machine, (icon, state) in zip(every, summaries, strict=True):
        busy = " · ⏳ busy" if busy_message(machine) else ""
        lines.append(f"{icon} <b>{escape(machine.name)}</b> · {state}{busy}")
    if pending := pending_timers():
        lines += ["", *(timer_line(t) for t in pending)]
    buttons = [
        InlineKeyboardButton(f"📊 {m.name}", callback_data=f"status:{m.name}") for m in every
    ]
    rows = [
        buttons[i : i + machines.PICKER_COLUMNS]
        for i in range(0, len(buttons), machines.PICKER_COLUMNS)
    ]
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="status")])
    return checked(card("📊", "Machines", *lines)), InlineKeyboardMarkup(rows)


async def status_card() -> tuple[str, InlineKeyboardMarkup]:
    if machines.several():
        return await overview_card()
    return await machine_card(machines.first())


@authorized
async def status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await typing(update)
    await reply(update, *await status_card())


HOMELAB_REFRESH = refresh_button("homelab")


def homelab_buttons() -> InlineKeyboardMarkup:
    """Refresh, and Reboot homelab while an update needs a reboot (never in a
    container, where the reboot is the host's)."""
    if reboot_refusal() is not None or read_reboot_required() is None:
        return HOMELAB_REFRESH
    return InlineKeyboardMarkup([[*HOMELAB_REFRESH.inline_keyboard[0], REBOOT_BUTTON]])


async def homelab_card() -> tuple[str, InlineKeyboardMarkup]:
    return checked(await homelab_message()), homelab_buttons()


@authorized
async def homelab(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await typing(update)
    await reply(update, *await homelab_card())


NET_REFRESH = refresh_button("net")


async def net_card() -> tuple[str, InlineKeyboardMarkup]:
    return checked(await net_message()), NET_REFRESH


@authorized
async def net(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await typing(update)
    await reply(update, *await net_card())


@authorized
async def summary(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await typing(update)
    await reply(update, await digest_message(), KEYBOARD)


@authorized
async def week(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, week_message(time.time()), KEYBOARD)


# --- Confirmation prompts -----------------------------------------------------

# Prompt message IDs already confirmed or cancelled (with when), so a double tap
# can't run twice. The first tap removes the buttons, so entries only need to
# outlive a burst of taps; older ones are pruned.
_used_prompts: dict[int, float] = {}


async def ask_to_confirm(update: Update, name: str, machine: Machine) -> None:
    await typing(update)
    if busy := busy_message(machine):
        await reply(update, busy)
        return
    if reason := await ssh_unavailable_reason(machine):
        await reply(update, reason)
        return
    action = POWER_ACTIONS[name]
    prompt = question(name, machine)
    # Sleep keeps every app open, so only shutdown and reboot warn about sessions.
    if name in ("shutdown", "reboot") and (
        warning := sessions_warning(await signed_in_sessions(machine))
    ):
        prompt = f"{prompt}\n\n{warning}"
    data = machines.tagged(f"confirm:{name}:{int(time.time())}", machine)
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(action.button, callback_data=data),
                InlineKeyboardButton("Cancel", callback_data="cancel"),
            ]
        ]
    )
    await reply(update, prompt, buttons)


async def ask_to_reboot_homelab(query: CallbackQuery, update: Update) -> None:
    if refusal := reboot_refusal():  # e.g. an old button from before the move
        _ = await query.answer(refusal)
        return
    if update.effective_message is None:  # Telegram no longer lets us reply to it
        _ = await query.answer("This message is too old. Send /homelab again.")
        return
    _ = await query.answer()
    needed = read_reboot_required()
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Reboot", callback_data=f"confirm:{HOMELAB_REBOOT}:{int(time.time())}"
                ),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{HOMELAB_REBOOT}"),
            ]
        ]
    )
    await reply(
        update,
        card(
            "🔄",
            "Reboot the homelab?",
            "Its services, this bot included, are down for a minute or two. "
            + "I'll say when I'm back.",
            *([] if needed is not None else ["<i>No update is waiting for a reboot.</i>"]),
        ),
        buttons,
    )


async def reboot_homelab(query: CallbackQuery) -> None:
    audit(HOMELAB_REBOOT, "confirmed")
    await edit(query, card("⏳", "Rebooting the homelab…", "I'll say when I'm back."))
    if error := await request_reboot():
        audit(HOMELAB_REBOOT, "failed")
        await edit(query, error)
        return
    audit(HOMELAB_REBOOT, "requested")


async def confirm_on(name: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shutdown, /reboot, /sleep [machine]: ask for confirmation."""
    if chosen := await machines.choose(update, context.args or [], name):
        await ask_to_confirm(update, name, chosen[0])


@authorized
async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await confirm_on("shutdown", update, context)


@authorized
async def reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await confirm_on("reboot", update, context)


@authorized
async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await confirm_on("sleep", update, context)


# What a pick:<command>:<machine> button runs: the command for that machine.
MachineCommand = Callable[[Update, ContextTypes.DEFAULT_TYPE, Machine, list[str]], Awaitable[None]]
PICK_COMMANDS: dict[str, MachineCommand] = {
    "wake": wake_machine,
    "sleep": lambda update, _context, machine, _args: ask_to_confirm(update, "sleep", machine),
    "reboot": lambda update, _context, machine, _args: ask_to_confirm(update, "reboot", machine),
    "shutdown": lambda update, _context, machine, _args: ask_to_confirm(
        update, "shutdown", machine
    ),
    "schedule_shutdown": lambda update, _context, machine, args: schedule_for(
        update, "shutdown", machine, args
    ),
    "schedule_wake": lambda update, _context, machine, args: schedule_for(
        update, "wake", machine, args
    ),
}


async def on_pick_button(
    query: CallbackQuery, update: Update, context: ContextTypes.DEFAULT_TYPE, rest: str
) -> None:
    """A machine from the picker: rest is "<command>:<machine>". Runs the command
    for that machine, replying under the picker, like a menu button."""
    command, _, name = rest.partition(":")
    machine = machines.named(name)
    if command not in PICK_COMMANDS or machine is None:
        _ = await query.answer("That machine isn't configured any more.")
        return
    if update.effective_message is None:  # Telegram no longer lets us reply to it
        _ = await query.answer(f"This message is too old. Send /{command} again.")
        return
    _ = await query.answer()
    await PICK_COMMANDS[command](update, context, machine, [])


# Tapping Refresh repeatedly re-probes at most once per cooldown per message.
REFRESH_COOLDOWN_SECONDS = 3.0
_last_refresh: dict[int, float] = {}


# A Refresh button's data -> how to re-read its message and buttons.
Card = Callable[[], Awaitable[tuple[str, InlineKeyboardMarkup]]]
REFRESHABLE: dict[str, Card] = {
    "status": status_card,
    "homelab": homelab_card,
    "net": net_card,
}


async def refresh(query: CallbackQuery, make_card: Card) -> None:
    """Re-read what a Refresh button's message shows."""
    message_id = query.message.message_id if query.message else None
    now = time.monotonic()
    if message_id is not None:
        last = _last_refresh.get(message_id)
        if last is not None and now - last < REFRESH_COOLDOWN_SECONDS:
            _ = await query.answer("Just checked. Try again in a moment.")
            return
        # Forget old entries so the dict can't grow without bound.
        for old in [k for k, t in _last_refresh.items() if now - t >= REFRESH_COOLDOWN_SECONDS]:
            del _last_refresh[old]
        _last_refresh[message_id] = now
    _ = await query.answer("Checking…")
    await edit(query, *await make_card())


@authorized
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if query.data in REFRESHABLE:  # Refresh: read-only, may be tapped any number of times
        await refresh(query, REFRESHABLE[query.data])
        return
    kind, _, rest = (query.data or "").partition(":")
    if kind == "status":  # one machine's card, from the overview: read-only too
        if (machine := machines.named(rest)) is None:
            _ = await query.answer("That machine isn't configured any more.")
            return
        await refresh(query, lambda: machine_card(machine))
        return
    if kind == "pick":  # same as typing the command with the machine; reusable
        await on_pick_button(query, update, context, rest)
        return
    if kind == "timer":  # Cancel/+30m: each names the exact timer, so repeats are safe
        await on_timer_button(query, context, rest)
        return
    if kind == "when":  # a preset only asks for confirmation, on this same message
        await on_when_button(query, rest)
        return
    if kind == "logs":  # switching views only re-reads the buffer
        await on_logs_button(query, rest)
        return
    if kind == "menu":  # same as typing the command; reusable
        await on_menu_button(query, update, context, rest)
        return
    if query.data == f"ask:{HOMELAB_REBOOT}":  # only asks, in a new message; reusable
        await ask_to_reboot_homelab(query, update)
        return
    _ = await query.answer()

    prompt_id = query.message.message_id if query.message else None
    if prompt_id is not None:
        if prompt_id in _used_prompts:
            return
        now = time.monotonic()
        for old in [k for k, t in _used_prompts.items() if now - t > CONFIRM_TTL_SECONDS]:
            del _used_prompts[old]
        _used_prompts[prompt_id] = now

    if kind == "sched":
        await confirm_schedule(query, context, rest)
        return
    if query.data == "cancel:sched":
        stop_asking_when()
        await edit(query, "✖️ Cancelled. No timer set.")
        return
    fields = rest.split(":")
    name, issued = (fields + [""])[:2]
    # The machine a power prompt was for (None: no longer configured: expired).
    machine = machines.from_field(fields, 2)
    if kind != "confirm" or (name not in POWER_ACTIONS and name != HOMELAB_REBOOT):
        audit(name or "unknown", "cancelled")
        await edit(query, "✖️ Cancelled. Nothing was sent.")
        return
    if name == HOMELAB_REBOOT and (refusal := reboot_refusal()):
        audit(HOMELAB_REBOOT, "refused")
        await edit(query, f"✖️ {refusal}")
        return
    expired = time.time() - int(issued) > CONFIRM_TTL_SECONDS if is_number(issued) else True
    if expired or (name != HOMELAB_REBOOT and machine is None):
        audit(name, "expired")
        await edit(
            query,
            card(
                "⌛",
                "Expired",
                f"This confirmation is over {CONFIRM_TTL_SECONDS}s old. "
                + (
                    "Tap Reboot homelab under /homelab again."
                    if name == HOMELAB_REBOOT
                    else f"Send /{name} again."
                ),
            ),
        )
        return

    if name == HOMELAB_REBOOT:
        await reboot_homelab(query)
        return
    if machine is None:  # (checked above; keeps the type checker sure)
        return
    await run_power_action(machine, name, query_setter(query), background_spawner(context, update))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception a handler raised and tell the user something went wrong."""
    log.error("Unhandled error in handler", exc_info=context.error)
    if isinstance(update, Update) and is_authorized(update):
        try:
            await reply(
                update,
                card(
                    "💥",
                    "Something went wrong",
                    f"<code>{escape(type(context.error).__name__)}</code>. Details are in the logs.",
                ),
            )
        except Exception:
            log.exception("Could not report the error to the user")


COMMANDS: dict[str, Handler] = {
    "start": start,
    "help": start,
    "wake": wake,
    "status": status,
    "homelab": homelab,
    "net": net,
    "summary": summary,
    "week": week,
    "sleep": sleep,
    "reboot": reboot,
    "shutdown": shutdown,
    "schedule_shutdown": schedule_shutdown,
    "schedule_wake": schedule_wake,
    "timers": timers,
    "logs": logs,
    "about": about,
}
