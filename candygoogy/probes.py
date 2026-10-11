"""Is a machine awake, asleep or off? Read-only checks."""

import asyncio
import contextlib
from html import escape

from candygoogy import machines, system
from candygoogy.history import last_action_line
from candygoogy.messages import card, duration
from candygoogy.oscommands import (
    PARSE_DETAILS,
    Details,
    Session,
    commands_for,
    describe_sessions,
    parse_sessions,
)
from candygoogy.settings import Machine
from candygoogy.system import ssh

# --- Probes -------------------------------------------------------------------


async def ping(machine: Machine) -> bool:
    return (await system.run("ping", "-c", "1", "-W", "2", machine.ip, timeout=5)).ok


async def ssh_port_open(machine: Machine, timeout: float = 3) -> bool:
    """TCP-connect to its SSH port and hang up; nothing runs on the machine."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(machine.ip, machine.ssh_port), timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def is_awake(machine: Machine) -> bool:
    # Either signal is enough: a firewall may drop pings while sshd answers.
    responds_to_ping, ssh_open = await asyncio.gather(ping(machine), ssh_port_open(machine))
    return responds_to_ping or ssh_open


async def ssh_unavailable_reason(machine: Machine) -> str | None:
    """None if SSH can be attempted, otherwise a message explaining why not."""
    responds_to_ping, ssh_open = await asyncio.gather(ping(machine), ssh_port_open(machine))
    if ssh_open:
        return None
    if responds_to_ping:
        return card(
            "⚠️",
            "SSH is closed",
            f"{machines.capitalised(machine)} answers ping, but port {machine.ssh_port} is closed. "
            + "Is sshd running?",
        )
    return card("💤", "Unreachable", "It's already asleep or off. Nothing sent.")


async def arp_entry(machine: Machine) -> tuple[str | None, str | None]:
    """(state, MAC) of the machine's entry in the homelab's neighbour table.

    Read right after ping/port probes, which make the kernel send fresh ARP
    requests. State is e.g. REACHABLE, STALE, INCOMPLETE or FAILED.
    """
    result = await system.run("ip", "neigh", "show", machine.ip, timeout=3)
    tokens = result.out.split() if result.ok else []
    if not tokens:
        return None, None
    mac = tokens[tokens.index("lladdr") + 1] if "lladdr" in tokens[:-1] else None
    return tokens[-1], mac


# --- Details while awake (read-only, over SSH) ---------------------------------

# Below this share of the disk free, /status warns.
LOW_DISK_FRACTION = 0.10


async def machine_details(machine: Machine) -> Details | None:
    result = await ssh(machine, "details", timeout=15)
    if not result.ok:
        return None
    return PARSE_DETAILS[machine.os](result.out)


def size(num_bytes: float) -> str:
    """Bytes as Windows shows them (1 GB = 1024³): "9.8 GB", "412 GB", "1.8 TB"."""
    gib = num_bytes / 1024**3
    if gib >= 1000:
        return f"{gib / 1024:.1f} TB"
    return f"{gib:.1f} GB" if gib < 10 else f"{gib:.0f} GB"


def details_lines(d: Details, disk_label: str = "C:") -> list[str]:
    lines: list[str] = []
    if d.boot is not None:
        lines.append(f"Last boot {duration(d.boot)} ago.")
    usage: list[str] = []
    if d.cpu is not None:
        usage.append(f"CPU {d.cpu:.0f}%")
    if d.mem_free is not None and d.mem_total:
        usage.append(f"RAM {size(d.mem_total - d.mem_free)} / {size(d.mem_total)}")
    low_disk = False
    if d.disk_free is not None and d.disk_total:
        low_disk = d.disk_free / d.disk_total < LOW_DISK_FRACTION
        if not low_disk:
            usage.append(f"{disk_label} {size(d.disk_free)} free")
    if usage:
        lines.append(" · ".join(usage))
    if low_disk and d.disk_free is not None and d.disk_total:
        lines.append(
            f"⚠️ {disk_label} is almost full: {size(d.disk_free)} free of {size(d.disk_total)}."
        )
    return lines


# --- Who's signed in (read-only, over SSH) -------------------------------------


async def signed_in_sessions(machine: Machine) -> list[Session] | None:
    """Who's signed in; None if it couldn't be checked."""
    result = await ssh(machine, "sessions", timeout=10)
    return parse_sessions(machine.os, result, machine.ssh_user)


def sessions_warning(sessions: list[Session] | None) -> str | None:
    """For shutdown/reboot prompts."""
    if sessions is None:
        return "<i>Couldn't check who's signed in.</i>"
    if not sessions:
        return None
    return f"⚠️ Signed in: {describe_sessions(sessions)}. Unsaved work will be lost."


def sessions_line(sessions: list[Session]) -> str:
    """For /status."""
    if not sessions:
        return "👤 Nobody signed in."
    return f"👤 Signed in: {describe_sessions(sessions)}."


def same_mac(a: str, b: str) -> bool:
    return a.lower().replace("-", ":") == b.lower().replace("-", ":")


async def machine_status(machine: Machine) -> str:
    """Best-effort awake/asleep/off verdict. Says so when it cannot tell."""
    commands = commands_for(machine.os)
    responds_to_ping, ssh_open = await asyncio.gather(ping(machine), ssh_port_open(machine))
    if responds_to_ping or ssh_open:
        lines = [f"Ping {'✅' if responds_to_ping else '❌'} · SSH {'✅' if ssh_open else '❌'}"]
        if ssh_open:
            # Two independent read-only SSH calls, in parallel.
            details, sessions = await asyncio.gather(
                machine_details(machine), signed_in_sessions(machine)
            )
            if details is not None:
                lines += details_lines(details, commands.disk_label)
            if sessions is not None:
                lines.append(sessions_line(sessions))
            if details is None and sessions is None:
                lines.append("<i>Couldn't read details over SSH.</i>")
        return card("🟢", "Awake", *lines)

    state, mac = await arp_entry(machine)
    if mac and not same_mac(mac, machine.mac):
        if commands.arp_proxy_possible:
            # A Bonjour Sleep Proxy answers ARP for a sleeping Mac with its own MAC.
            return card(
                "🌙",
                "Maybe asleep",
                "No ping or SSH, and another device answers ARP for its IP: perhaps a "
                + "Bonjour Sleep Proxy (e.g. an Apple TV or HomePod), which does that "
                + "while a Mac sleeps. If there's none, check your DHCP leases.",
                last_action_line(machine),
            )
        return card(
            "⚠️",
            "Possible IP conflict",
            f"{machines.capitalised(machine)} doesn't respond, and its IP answers ARP with a different "
            + "MAC address. Check your DHCP leases.",
        )
    if not commands.arp_means_asleep:
        # Linux and macOS don't answer ARP while asleep, so ARP can't tell the two apart.
        return card(
            "⚫",
            "Off or asleep",
            "No ping or SSH. This machine doesn't answer ARP while asleep, so I can't "
            + "tell asleep from off.",
            last_action_line(machine),
        )
    if state == "REACHABLE":
        # The NIC's ARP offload answers ARP for the PC in S3 sleep but not when off.
        return card(
            "🌙",
            "Probably asleep",
            "No ping or SSH, but its network card still answers ARP, which it does in sleep.",
            "<i>If it went down under a minute ago, this may be a stale ARP entry.</i>",
            last_action_line(machine),
        )
    if state in (None, "FAILED", "INCOMPLETE"):
        return card(
            "⚫",
            "Off or asleep",
            "No ping, SSH or ARP reply, so I can't reliably tell which. Its network card "
            + "normally answers ARP while asleep, so <b>off</b> is more likely, but not certain.",
            last_action_line(machine),
        )
    return card(
        "❔",
        "Off or asleep",
        f"No ping or SSH, and the ARP entry is <code>{escape(state)}</code>, which is "
        + "inconclusive. Try again in a few seconds.",
        last_action_line(machine),
    )


async def machine_summary(machine: Machine) -> tuple[str, str]:
    """(icon, a few words) for one line of the several-machines /status overview.
    Ping, the SSH port and ARP only: nothing runs on the machine."""
    responds_to_ping, ssh_open = await asyncio.gather(ping(machine), ssh_port_open(machine))
    if responds_to_ping or ssh_open:
        return "🟢", "awake"
    state, mac = await arp_entry(machine)
    if mac and not same_mac(mac, machine.mac) and not commands_for(machine.os).arp_proxy_possible:
        return "⚠️", "possible IP conflict"
    if commands_for(machine.os).arp_means_asleep and state == "REACHABLE":
        return "🌙", "probably asleep"
    return "⚫", "off or asleep"
