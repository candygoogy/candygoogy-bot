"""Wake-on-LAN: the magic packet, sent from Python (no external program).

A magic packet is 6 bytes of FF and then the MAC 16 times, sent as one UDP
broadcast to port 9. It goes to the machine's wol_broadcast if set (e.g. the
directed broadcast of one subnet on a homelab with several), else 255.255.255.255.
"""

import socket
from collections.abc import Callable

from candygoogy.settings import Machine
from candygoogy.system import RunResult

WOL_PORT = 9
LIMITED_BROADCAST = "255.255.255.255"


def magic_packet(mac: str) -> bytes:
    """6×FF, then the MAC's 6 bytes 16 times (102 bytes). `mac` is validated config."""
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# Tests replace this (conftest.py refuses real sockets).
open_socket: Callable[[], socket.socket] = udp_socket


def destination(machine: Machine) -> tuple[str, int]:
    return machine.wol_broadcast or LIMITED_BROADCAST, WOL_PORT


async def send_magic_packet(machine: Machine) -> RunResult:
    """One packet. A failure is a RunResult with a plain message: never the MAC."""
    try:
        with open_socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            _ = sock.sendto(magic_packet(machine.mac), destination(machine))
    except OSError as e:
        # strerror is the OS's text ("Network is unreachable"); str(e) could
        # carry more, so it isn't used.
        reason = e.strerror or type(e).__name__
        return RunResult(-1, f"Couldn't send the magic packet: {reason}.")
    return RunResult(0)
