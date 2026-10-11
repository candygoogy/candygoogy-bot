"""Validated settings from environment variables.

Pure: the caller passes in the environment, and nothing happens at import, so
bot.py can check the configuration before anything else loads. load_settings() collects
every problem first and raises one ConfigError naming the variables, never
their values (a token pasted into the wrong variable must not end up in a log).
"""

import ipaddress
import math
import os
import posixpath
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple, cast

# Explicit [0-9] throughout: the regex digit class and Python's digit checks
# also accept other scripts' digits ("١", "２"), which int() then reads as numbers.
TOKEN = re.compile(r"[0-9]+:[A-Za-z0-9_-]{30,}")
# Only 0-9. Also for chat and button input that becomes a number: see is_number().
NUMBER = re.compile(r"[0-9]+")
MAX_USER_ID = 2**63 - 1
# Commas and/or ASCII whitespace between IDs. "1,,2" leaves an empty entry.
ID_SEPARATOR = re.compile(r"[ \t\r\n]*,[ \t\r\n]*|[ \t\r\n]+")
MAC = re.compile(r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}")
# Can never start with "-", so it can't be read as an ssh option.
SSH_USER = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# The one clock-time pattern: these settings and typed "when?" answers (timers.py).
CLOCK = re.compile(r"([01]?[0-9]|2[0-3])[:.]([0-5][0-9])")
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# A host name: dot-separated labels of letters, digits and inner hyphens.
HOSTNAME = re.compile(
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
# Only digits and dots: meant as an IPv4 address, so it must be a valid one.
DIGITS_AND_DOTS = re.compile(r"[0-9.]+")
MAX_DISKS = 10
MAX_TARGETS = 5
# CANDYGOOGY_COMMIT: the commit an image was built from, as git writes it.
COMMIT = re.compile(r"[0-9a-f]{7,40}")

DEFAULT_SSH_KEY_PATH = "~/.ssh/id_ed25519"
DEFAULT_BACKUP_MAX_AGE_HOURS = "26"
DEFAULT_DIGEST_TIME = "08:00"
DEFAULT_WEEKLY_REPORT = "sun 20:00"
DEFAULT_HOMELAB_DISKS = "/"
# Two anycast DNS providers: public, and reachable from almost anywhere.
DEFAULT_INTERNET_CHECK_TARGETS = "1.1.1.1:443,9.9.9.9:443"
# Where Docker Engine listens unless DOCKER_HOST says otherwise.
DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"

# Every variable load_settings() reads.
KNOWN_VARIABLES = (
    "BOT_TOKEN",
    "AUTHORIZED_USER_IDS",
    "MACHINES_FILE",
    "WORKSTATION_MAC",
    "WORKSTATION_IP",
    "SSH_USER",
    "SSH_KEY_PATH",
    "SSH_KNOWN_HOSTS",
    "BACKUP_MAX_AGE_HOURS",
    "DIGEST_TIME",
    "WEEKLY_REPORT",
    "HOMELAB_DISKS",
    "HOMELAB_HOST_ROOT",
    "BACKUP_DIR",
    "INTERNET_CHECK_TARGETS",
    "DOCKER_HOST",
    "CANDYGOOGY_IN_CONTAINER",
    "CANDYGOOGY_COMMIT",
)


class ConfigError(Exception):
    """The configuration is unusable. The message lists every problem, by variable name."""

    def __init__(self, problems: list[str]) -> None:
        self.problems: list[str] = problems
        lines = ["The configuration has problems:", *(f"- {p}" for p in problems)]
        lines.append("See .env.example for what each variable expects.")
        super().__init__("\n".join(lines))


class MachineLabels(NamedTuple):
    """What to call each machine field in problems, e.g. "WORKSTATION_MAC" or
    "machines.desk.mac"."""

    mac: str
    ip: str
    ssh_user: str
    ssh_key_path: str
    ssh_port: str = "ssh_port"
    wol_broadcast: str = "wol_broadcast"


def file_labels(name: str) -> MachineLabels:
    """Labels for a machine from MACHINES_FILE: "machines.<name>.<key>"."""
    keys = ("mac", "ip", "ssh_user", "ssh_key_path", "ssh_port", "wol_broadcast")
    return MachineLabels(*(f"machines.{name}.{key}" for key in keys))


# The single machine configured by environment variables.
WORKSTATION_LABELS = MachineLabels("WORKSTATION_MAC", "WORKSTATION_IP", "SSH_USER", "SSH_KEY_PATH")
# Its name and OS: the env vars describe one Windows PC.
ENV_MACHINE_NAME = "workstation"
ENV_MACHINE_VARS = ("WORKSTATION_MAC", "WORKSTATION_IP", "SSH_USER")
NO_MACHINE = (
    "No machine is configured: set WORKSTATION_MAC, WORKSTATION_IP and SSH_USER "
    + "(one Windows PC), or MACHINES_FILE."
)

OS_NAMES = ("windows", "linux", "macos")
# A machine's name is shown on buttons and travels in callback data, so it's
# short, ASCII (bytes = characters) and has no ":" (the callback separator).
MACHINE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,14}[a-z0-9])?")
RESERVED_NAMES = ("all",)  # kept free for an "all machines" button
MAX_MACHINES = 10
MACHINES_FILE_MAX_BYTES = 64 * 1024
MACHINE_KEYS = ("os", "mac", "ip", "ssh_user", "ssh_key_path", "ssh_port", "wol_broadcast")
TEXT_KEYS = ("os", "mac", "ip", "ssh_user", "ssh_key_path", "wol_broadcast")
# tomllib's message ends "(at line N, column M)"; only that position is reported,
# since the rest of the message may quote the file.
TOML_POSITION = re.compile(r"\(at line ([0-9]+), column ([0-9]+)\)")
# An unknown key is named in a problem only if it looks like a key name.
KEY_NAME = re.compile(r"[a-z_]{1,32}")


@dataclass(frozen=True)
class Machine:
    name: str  # 1-16 of a-z 0-9 -; "workstation" for the one from env vars
    os: str  # "windows", "linux" or "macos"
    mac: str  # lower case, colon-separated
    ip: str  # as ipaddress writes it
    ssh_user: str
    ssh_key_path: str  # with ~ expanded
    ssh_port: int = 22  # for ssh -p and the port probe
    wol_broadcast: str | None = None  # IPv4 to send the magic packet to; None: 255.255.255.255


@dataclass(frozen=True, repr=False)  # printing it must never show the token
class Settings:
    bot_token: str
    authorized_user_ids: tuple[int, ...]
    machines: tuple[Machine, ...]  # 1-10, in file order
    backup_max_age_hours: float
    digest_at: tuple[int, int] | None  # (hour, minute), None for "off"
    weekly_report_at: tuple[int, int, int] | None  # (weekday, hour, minute), Monday is 0
    homelab_disks: tuple[str, ...]  # "/" is the system disk, the others must stay mounted
    backup_dir: str | None  # None: backup monitoring is off
    internet_targets: tuple[tuple[str, int], ...]  # (host, port), tried in order
    in_container: bool  # host-only readings (units, updates, reboot) aren't taken
    ssh_known_hosts: str | None  # ssh's UserKnownHostsFile; None: ssh's default
    # Where Docker Engine's API is: "unix:///<path>" or "tcp://<host>:<port>".
    docker_host: str = DEFAULT_DOCKER_HOST
    # The host's / as mounted in a container (e.g. /host); None: read / itself.
    homelab_host_root: str | None = None
    commit: str | None = None  # CANDYGOOGY_COMMIT: the image's commit, for /about


def key_path(value: str | None, label: str, default: str, problems: list[str]) -> str | None:
    """An SSH key path, ~ expanded; `default` if not set. None (and a problem) if bad."""
    text = default if value is None else value.strip()
    if not text:
        problems.append(f"{label} is empty; leave it out to use the default.")
        return None
    if text.startswith("-"):
        problems.append(f"{label} can't start with -.")
        return None
    if any(c in text for c in "%$") or any(unicodedata.category(c) == "Cc" for c in text):
        # ssh expands %-tokens and ${VARS} in key paths, even from -i.
        problems.append(f"{label} can't contain %, $ or control characters.")
        return None
    path = os.path.expanduser(text)
    # A POSIX path on the homelab; os.path.isabs as well, so ~ works on any OS.
    if value is not None and not (path.startswith("/") or os.path.isabs(path)):
        problems.append(f"{label} must be an absolute path (~ is allowed).")
        return None
    return path


def check_mac(mac: str, label: str, problems: list[str]) -> str | None:
    if not MAC.fullmatch(mac):
        problems.append(f"{label} must be 6 hex pairs separated by : or -, e.g. 00:00:5e:00:53:01.")
        return None
    mac = mac.replace("-", ":").lower()
    first = int(mac[:2], 16)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") or first & 1:
        problems.append(f"{label} can't be all zeros, broadcast or multicast.")
        return None
    return mac


def check_ip(text: str, label: str, problems: list[str]) -> str | None:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        problems.append(f"{label} must be an IPv4 or IPv6 address, e.g. 192.0.2.10.")
        return None
    if "%" in str(ip):  # fe80::1%eth0: a scope would reach ping and ssh as-is
        problems.append(f"{label} can't have a %scope; use an address without one.")
        return None
    if ip.is_unspecified or ip.is_multicast or str(ip) == "255.255.255.255":
        problems.append(f"{label} can't be unspecified, multicast or broadcast.")
        return None
    return str(ip)


def check_machine(
    mac: str | None,
    ip: str | None,
    ssh_user: str | None,
    ssh_key_path: str | None,
    labels: MachineLabels,
    *,
    name: str = ENV_MACHINE_NAME,
    os_name: str = "windows",
    ssh_port: int = 22,
    wol_broadcast: str | None = None,
    default_key: str = DEFAULT_SSH_KEY_PATH,
) -> tuple[Machine | None, list[str]]:
    """One machine's fields, checked; problems use the caller's labels.

    None means not set: required for the MAC, IP and SSH user, while the key
    path then defaults to `default_key`. Returns no Machine if anything is wrong.
    """
    problems: list[str] = []
    if (mac := required(mac, labels.mac, problems)) is not None:
        mac = check_mac(mac, labels.mac, problems)
    if (ip := required(ip, labels.ip, problems)) is not None:
        ip = check_ip(ip, labels.ip, problems)
    user = required(ssh_user, labels.ssh_user, problems)
    if user is not None and not SSH_USER.fullmatch(user):
        problems.append(
            f"{labels.ssh_user} must be 1-64 of A-Z a-z 0-9 . _ -, and can't start with -."
        )
    key = key_path(ssh_key_path, labels.ssh_key_path, default_key, problems)
    if not 1 <= ssh_port <= 65535:
        problems.append(f"{labels.ssh_port} must be from 1 to 65535.")
    broadcast = None
    if wol_broadcast is not None:
        try:
            address = ipaddress.IPv4Address(wol_broadcast.strip())
        except ValueError:
            problems.append(f"{labels.wol_broadcast} must be an IPv4 address, e.g. 192.0.2.255.")
        else:
            if address.is_unspecified:
                problems.append(f"{labels.wol_broadcast} can't be 0.0.0.0.")
            broadcast = str(address)
    if problems or mac is None or ip is None or user is None or key is None:
        return None, problems
    machine = Machine(name, os_name, mac, ip, user, key, ssh_port, broadcast)
    return machine, []


def read_machines_file(path: str, problems: list[str]) -> dict[str, object] | None:
    """MACHINES_FILE parsed as TOML; None (and a problem) if it can't be. Problems
    never include the path or the file's content."""
    try:
        with open(os.path.expanduser(path), "rb") as file:
            data = file.read(MACHINES_FILE_MAX_BYTES + 1)
    except OSError:
        problems.append("MACHINES_FILE can't be read.")
        return None
    if len(data) > MACHINES_FILE_MAX_BYTES:
        problems.append(f"MACHINES_FILE is larger than {MACHINES_FILE_MAX_BYTES // 1024} KiB.")
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("MACHINES_FILE isn't UTF-8 text.")
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        where = f" (line {m[1]}, column {m[2]})" if (m := TOML_POSITION.search(str(e))) else ""
        problems.append(f"MACHINES_FILE is not valid TOML{where}.")
        return None


def file_machine(
    name: str, entry: dict[str, object], default_key: str, problems: list[str]
) -> Machine | None:
    """One [machines.<name>] table, checked (keys, types, then the field rules)."""
    prefix = f"machines.{name}"
    found: list[str] = []
    for key in entry:
        if key not in MACHINE_KEYS:
            named = f" {key!r}" if KEY_NAME.fullmatch(key) else ""
            found.append(f"{prefix} has an unknown key{named}.")
    text: dict[str, str | None] = {}
    for key in TEXT_KEYS:
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            found.append(f"{prefix}.{key} must be text in quotes.")
        text[key] = value if isinstance(value, str) else None
    port = entry.get("ssh_port", 22)
    if isinstance(port, bool) or not isinstance(port, int):
        found.append(f"{prefix}.ssh_port must be a whole number, without quotes.")
        port = 22
    os_name = text["os"]
    if os_name is None:
        if "os" not in entry:
            found.append(f"{prefix}.os is missing.")
    elif os_name not in OS_NAMES:
        found.append(f"{prefix}.os must be windows, linux or macos.")
    machine, machine_problems = check_machine(
        text["mac"],
        text["ip"],
        text["ssh_user"],
        text["ssh_key_path"],
        file_labels(name),
        name=name,
        os_name=os_name or "windows",
        ssh_port=port,
        wol_broadcast=text["wol_broadcast"],
        default_key=default_key,
    )
    # A field of the wrong type was reported above; don't also call it missing.
    wrong = {f"{prefix}.{key} is missing." for key in TEXT_KEYS if entry.get(key) is not None}
    found += [p for p in machine_problems if p not in wrong]
    problems += found
    return None if found else machine


def file_machines(
    data: dict[str, object], default_key: str, problems: list[str]
) -> tuple[Machine, ...]:
    """Every [machines.<name>] table, in file order, plus the rules across them."""
    if any(key != "machines" for key in data):
        problems.append("MACHINES_FILE may only contain [machines.<name>] tables.")
    table = data.get("machines")
    if not isinstance(table, dict) or not table:
        problems.append("MACHINES_FILE has no [machines.<name>] tables.")
        return ()
    entries = cast(dict[str, object], table)
    if len(entries) > MAX_MACHINES:
        problems.append(f"MACHINES_FILE lists more than {MAX_MACHINES} machines.")
    machines: list[Machine] = []
    for n, (name, entry) in enumerate(entries.items(), start=1):
        if not MACHINE_NAME.fullmatch(name) or name in RESERVED_NAMES:
            # Labelled by position: an invalid name isn't echoed.
            problems.append(
                f"machines entry {n} needs another name: 1-16 of a-z 0-9 and -, "
                + 'not starting or ending with -, and not "all".'
            )
            continue
        if not isinstance(entry, dict):
            problems.append(f"machines.{name} must be a table, [machines.{name}].")
            continue
        if machine := file_machine(name, cast(dict[str, object], entry), default_key, problems):
            machines.append(machine)
    for field in ("ip", "mac"):
        seen: dict[str, str] = {}
        for machine in machines:
            value = machine.ip if field == "ip" else machine.mac
            if value in seen:
                problems.append(
                    f"machines.{machine.name}.{field} is the same as machines.{seen[value]}.{field}."
                )
            _ = seen.setdefault(value, machine.name)
    return tuple(machines)


def known_hosts(value: str | None, problems: list[str]) -> str | None:
    """SSH_KNOWN_HOSTS: an absolute path for ssh's UserKnownHostsFile, or None."""
    if value is None or not value.strip():
        return None
    try:
        path = posix_path(value.strip())
    except ValueError as e:
        problems.append(f"SSH_KNOWN_HOSTS {e}.")
        return None
    # It's passed as -o UserKnownHostsFile=<path>: ssh splits that on spaces and
    # expands %-tokens and ${VARS} in it.
    if any(c.isspace() or c in "%$" for c in path):
        problems.append("SSH_KNOWN_HOSTS can't contain spaces, % or $.")
        return None
    return path


def is_number(text: str) -> bool:
    """Only the digits 0-9, so int() of it can't fail or read another script's
    digits ("\u00b2" and "\u0661" would pass the str method for digits)."""
    return NUMBER.fullmatch(text) is not None


def required(value: str | None, name: str, problems: list[str]) -> str | None:
    """The value without surrounding whitespace, or None (and a problem) if missing or empty."""
    if value is None:
        problems.append(f"{name} is missing.")
        return None
    if not (value := value.strip()):
        problems.append(f"{name} is empty.")
        return None
    return value


def user_ids(text: str) -> tuple[int, ...]:
    """Telegram user IDs, separated by commas and/or whitespace; duplicates
    dropped, order kept. Raises ValueError saying what's wrong, without the value."""
    ids: list[int] = []
    for n, entry in enumerate(ID_SEPARATOR.split(text.strip()), start=1):
        if not entry:
            raise ValueError(f"entry {n} is empty (two commas in a row, or one at an end)")
        # Over 19 digits is past MAX_USER_ID anyway, and int() would refuse very long ones.
        if not is_number(entry) or len(entry) > 19:
            raise ValueError(f"entry {n} is not a whole number made of the digits 0-9")
        if not 0 < (user_id := int(entry)) <= MAX_USER_ID:
            raise ValueError(f"entry {n} is out of range (1 to {MAX_USER_ID})")
        if user_id not in ids:
            ids.append(user_id)
    return tuple(ids)


def digest_clock(text: str) -> tuple[int, int] | None:
    """(hour, minute) from DIGEST_TIME, or None for "off"; ValueError if neither."""
    text = text.strip().lower()
    if text in ("off", ""):
        return None
    if match := CLOCK.fullmatch(text):
        return int(match[1]), int(match[2])
    raise ValueError('must be a time like 08:00, or "off"')


def report_clock(text: str) -> tuple[int, int, int] | None:
    """(weekday, hour, minute) from WEEKLY_REPORT, Monday being 0, or None for
    "off"; ValueError if neither."""
    text = " ".join(text.lower().split())
    if text in ("off", ""):
        return None
    day, _, clock = text.partition(" ")
    weekday = next((i for i, name in enumerate(DAY_NAMES) if day in (name, name[:3])), None)
    if weekday is not None and (match := CLOCK.fullmatch(clock)):
        return weekday, int(match[1]), int(match[2])
    raise ValueError('must be a day and time like "sun 20:00", or "off"')


def comma_entries(text: str) -> list[str]:
    """Comma-separated entries without surrounding whitespace. Raises ValueError
    naming an empty one by position (two commas in a row, or one at an end)."""
    found = [entry.strip() for entry in text.split(",")]
    for n, entry in enumerate(found, start=1):
        if not entry:
            raise ValueError(f"entry {n} is empty (two commas in a row, or one at an end)")
    return found


def posix_path(text: str) -> str:
    """An absolute path on the homelab (POSIX, whatever this machine is),
    normalised; ValueError saying what's wrong, without the value."""
    if not text.startswith("/"):
        raise ValueError("must be an absolute path, starting with /")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError("can't contain control characters")
    if ".." in text.split("/"):
        raise ValueError("can't contain ..")
    return posixpath.normpath(text)


def disk_paths(text: str) -> tuple[str, ...]:
    """HOMELAB_DISKS: up to MAX_DISKS absolute paths, duplicates dropped, order kept."""
    entries = comma_entries(text.strip())
    if len(entries) > MAX_DISKS:
        raise ValueError(f"lists more than {MAX_DISKS} paths")
    paths: list[str] = []
    for n, entry in enumerate(entries, start=1):
        try:
            path = posix_path(entry)
        except ValueError as e:
            raise ValueError(f"entry {n} {e}") from None
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def target(entry: str) -> tuple[str, int]:
    """One "host:port" or "[IPv6]:port"; ValueError saying what's wrong, without the value."""
    if entry.startswith("["):
        host, bracket, port_text = entry[1:].partition("]:")
        if not bracket:
            raise ValueError("must be [IPv6 address]:port")
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError:
            raise ValueError("has no valid IPv6 address in the [ ]") from None
        if "%" in host:
            raise ValueError("can't have a %scope")
    else:
        host, colon, port_text = entry.rpartition(":")
        if not colon or not host:
            raise ValueError("must be host:port, e.g. 1.1.1.1:443")
        if ":" in host:
            raise ValueError("needs [ ] around an IPv6 address, e.g. [2606:4700:4700::1111]:443")
        if DIGITS_AND_DOTS.fullmatch(host):
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                raise ValueError("has an invalid IPv4 address") from None
        elif len(host) > 253 or not HOSTNAME.fullmatch(host):
            raise ValueError("has an invalid host name")
    if not is_number(port_text) or len(port_text) > 5 or not 1 <= int(port_text) <= 65535:
        raise ValueError("needs a port from 1 to 65535")
    return host, int(port_text)


def targets(text: str) -> tuple[tuple[str, int], ...]:
    """INTERNET_CHECK_TARGETS: 1 to MAX_TARGETS entries, duplicates dropped, order kept."""
    entries = comma_entries(text.strip())
    if len(entries) > MAX_TARGETS:
        raise ValueError(f"lists more than {MAX_TARGETS} targets")
    found: list[tuple[str, int]] = []
    for n, entry in enumerate(entries, start=1):
        try:
            host_port = target(entry)
        except ValueError as e:
            raise ValueError(f"entry {n} {e}") from None
        if host_port not in found:
            found.append(host_port)
    return tuple(found)


def docker_host(text: str) -> str:
    """DOCKER_HOST: "unix:///<path>" or "tcp://<host>:<port>", normalised;
    ValueError saying what's wrong, without the value."""
    scheme, separator, rest = text.partition("://")
    if separator and scheme == "unix":
        try:
            return "unix://" + posix_path(rest)
        except ValueError as e:
            raise ValueError(f"has a socket path that {e}") from None
    if separator and scheme == "tcp":
        host, port = target(rest.removesuffix("/"))
        return f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"
    raise ValueError("must be unix:///<socket path> or tcp://<host>:<port>")


def commit(text: str) -> str | None:
    """CANDYGOOGY_COMMIT: 7 to 40 of 0-9 a-f, or None if empty; ValueError if neither."""
    text = text.strip()
    if not text:
        return None
    if not COMMIT.fullmatch(text):
        raise ValueError("must be a commit hash: 7 to 40 of 0-9 a-f")
    return text


def flag(text: str) -> bool:
    """1/true or 0/false (any case; empty is false); ValueError if neither."""
    value = text.strip().lower()
    if value in ("1", "true"):
        return True
    if value in ("", "0", "false"):
        return False
    raise ValueError("must be 1, true, 0 or false")


def hours(text: str) -> float:
    """A plain decimal number of hours above 0; ValueError if not."""
    text = text.strip()
    if not DECIMAL.fullmatch(text) or not math.isfinite(value := float(text)) or value <= 0:
        raise ValueError("must be a number of hours above 0, e.g. 26 or 1.5")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    """Every setting, checked. Raises ConfigError listing all problems at once."""
    problems: list[str] = []

    token = required(env.get("BOT_TOKEN"), "BOT_TOKEN", problems)
    if token is not None and not TOKEN.fullmatch(token):
        problems.append(
            "BOT_TOKEN doesn't look like a bot token "
            + "(digits, a colon, then 30 or more of A-Z a-z 0-9 _ -)."
        )

    ids: tuple[int, ...] = ()
    if "AUTHORIZED_USER_IDS" not in env and "AUTHORIZED_USER_ID" in env:
        problems.append(
            "AUTHORIZED_USER_IDS is missing. AUTHORIZED_USER_ID was renamed to "
            + "AUTHORIZED_USER_IDS, which takes one or more IDs separated by commas."
        )
    elif ids_text := required(env.get("AUTHORIZED_USER_IDS"), "AUTHORIZED_USER_IDS", problems):
        try:
            ids = user_ids(ids_text)
        except ValueError as e:
            problems.append(f"AUTHORIZED_USER_IDS: {e}.")

    machines: tuple[Machine, ...] = ()
    if machines_path := env.get("MACHINES_FILE", "").strip():
        if any(env.get(name, "").strip() for name in ENV_MACHINE_VARS):
            problems.append(
                "Use either MACHINES_FILE or WORKSTATION_MAC, WORKSTATION_IP and SSH_USER, "
                + "not both."
            )
        # SSH_KEY_PATH, if set, is every file machine's default key.
        default_key = key_path(
            env.get("SSH_KEY_PATH"), "SSH_KEY_PATH", DEFAULT_SSH_KEY_PATH, problems
        )
        if (data := read_machines_file(machines_path, problems)) is not None:
            machines = file_machines(data, default_key or DEFAULT_SSH_KEY_PATH, problems)
    elif not any(env.get(name, "").strip() for name in ENV_MACHINE_VARS):
        # Nothing at all: one problem that names both ways, rather than three
        # "missing" lines that hide MACHINES_FILE.
        problems.append(NO_MACHINE)
        if "SSH_KEY_PATH" in env:  # still checked if set
            _ = key_path(env["SSH_KEY_PATH"], "SSH_KEY_PATH", DEFAULT_SSH_KEY_PATH, problems)
    else:
        workstation, machine_problems = check_machine(
            env.get("WORKSTATION_MAC"),
            env.get("WORKSTATION_IP"),
            env.get("SSH_USER"),
            env.get("SSH_KEY_PATH"),
            WORKSTATION_LABELS,
        )
        problems += machine_problems
        machines = (workstation,) if workstation is not None else ()
    ssh_known_hosts = known_hosts(env.get("SSH_KNOWN_HOSTS"), problems)

    max_age = 0.0
    try:
        max_age = hours(env.get("BACKUP_MAX_AGE_HOURS", DEFAULT_BACKUP_MAX_AGE_HOURS))
    except ValueError as e:
        problems.append(f"BACKUP_MAX_AGE_HOURS {e}.")
    digest_at = None
    try:
        digest_at = digest_clock(env.get("DIGEST_TIME", DEFAULT_DIGEST_TIME))
    except ValueError as e:
        problems.append(f"DIGEST_TIME {e}.")
    report_at = None
    try:
        report_at = report_clock(env.get("WEEKLY_REPORT", DEFAULT_WEEKLY_REPORT))
    except ValueError as e:
        problems.append(f"WEEKLY_REPORT {e}.")

    disks: tuple[str, ...] = ()
    disks_text = env.get("HOMELAB_DISKS", DEFAULT_HOMELAB_DISKS)
    if not disks_text.strip():
        problems.append("HOMELAB_DISKS is empty; leave it out to check just /.")
    else:
        try:
            disks = disk_paths(disks_text)
        except ValueError as e:
            problems.append(f"HOMELAB_DISKS: {e}.")
    backup_dir = None
    if backup_text := env.get("BACKUP_DIR", "").strip():  # unset or empty: monitoring off
        try:
            backup_dir = posix_path(backup_text)
        except ValueError as e:
            problems.append(f"BACKUP_DIR {e}.")
    host_root = None
    if root_text := env.get("HOMELAB_HOST_ROOT", "").strip():  # unset or empty: /
        try:
            host_root = posix_path(root_text)
        except ValueError as e:
            problems.append(f"HOMELAB_HOST_ROOT {e}.")
    docker = DEFAULT_DOCKER_HOST
    if docker_text := env.get("DOCKER_HOST", "").strip():
        try:
            docker = docker_host(docker_text)
        except ValueError as e:
            problems.append(f"DOCKER_HOST {e}.")
    internet: tuple[tuple[str, int], ...] = ()
    targets_text = env.get("INTERNET_CHECK_TARGETS", DEFAULT_INTERNET_CHECK_TARGETS)
    if not targets_text.strip():
        problems.append("INTERNET_CHECK_TARGETS is empty; leave it out to use the defaults.")
    else:
        try:
            internet = targets(targets_text)
        except ValueError as e:
            problems.append(f"INTERNET_CHECK_TARGETS: {e}.")
    in_container = False
    try:
        in_container = flag(env.get("CANDYGOOGY_IN_CONTAINER", ""))
    except ValueError as e:
        problems.append(f"CANDYGOOGY_IN_CONTAINER {e}.")
    build_commit = None
    try:
        build_commit = commit(env.get("CANDYGOOGY_COMMIT", ""))
    except ValueError as e:
        problems.append(f"CANDYGOOGY_COMMIT {e}.")

    if problems or token is None or not machines:
        raise ConfigError(problems)
    return Settings(
        token,
        ids,
        machines,
        max_age,
        digest_at,
        report_at,
        disks,
        backup_dir,
        internet,
        in_container,
        ssh_known_hosts,
        docker_host=docker,
        homelab_host_root=host_root,
        commit=build_commit,
    )
