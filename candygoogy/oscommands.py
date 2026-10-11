"""What the bot runs on each kind of machine over SSH, and how it reads the answers.

Every command is a constant: nothing from chat ever reaches one (see system.py
and tests/test_subprocess_invariant.py). Windows is what the bot has always run;
Linux and macOS are experimental (see docs/linux-target.md and docs/macos-target.md).

Linux and macOS commands run in the target's login shell, which may be bash, zsh,
dash, fish or tcsh. So each one is `/bin/sh -c '<script>'`, and the script has no
single quote, backslash, "!" or newline (tests enforce this). Login shells may
print from startup files, so each script prints a marker line first and the
parsers read only what follows the last marker.
"""

import json
import math
import re
from dataclasses import dataclass
from html import escape
from typing import Literal, Protocol, cast

from candygoogy.messages import duration
from candygoogy.state import number


class RunResult(Protocol):
    """What a command returned: system.RunResult. Described here rather than
    imported, since system.py runs these commands and so imports this module."""

    @property
    def code(self) -> int: ...
    @property
    def err(self) -> str: ...
    @property
    def out(self) -> str: ...
    @property
    def timed_out(self) -> bool: ...
    @property
    def ok(self) -> bool: ...


Action = Literal["shutdown", "reboot", "sleep", "details", "sessions"]
ACTIONS: tuple[Action, ...] = ("shutdown", "reboot", "sleep", "details", "sessions")

DETAILS_MARKER = "candygoogy-details-v1"
SESSIONS_MARKER = "candygoogy-sessions-v1"

# --- Windows (cmd.exe is Windows OpenSSH's default shell) -------------------------

WINDOWS_SHUTDOWN = "shutdown /s /t 0"
WINDOWS_REBOOT = "shutdown /r /t 0"
# S3 sleep via the Win32 SetSuspendState API with an explicit "Suspend", so it
# can never hibernate (unlike `rundll32 powrprof.dll,SetSuspendState`, which
# hibernates whenever hibernation is enabled). Runs under cmd.exe, which passes
# the double-quoted script to PowerShell untouched.
WINDOWS_SLEEP = (
    'powershell -NoProfile -NonInteractive -Command "'
    "Add-Type -AssemblyName System.Windows.Forms; "
    "if (-not [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false))"
    ' { exit 1 }"'
)
# One PowerShell call, printed as compact JSON. Runs under cmd.exe, which hands
# the double-quoted script to PowerShell untouched (it has no % ^ & < >).
# "boot" is seconds since Windows last booted: sleep doesn't reset it, and
# neither does a Start-menu shutdown with Fast Startup on (`shutdown /s` does).
WINDOWS_DETAILS = (
    'powershell -NoProfile -NonInteractive -Command "'
    "$o = Get-CimInstance Win32_OperatingSystem; "
    "$d = Get-CimInstance Win32_LogicalDisk -Filter 'DeviceID=''C:'''; "
    "$c = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
    "[pscustomobject]@{boot = [int]((Get-Date) - $o.LastBootUpTime).TotalSeconds; cpu = $c; "
    "memFree = [long]$o.FreePhysicalMemory * 1KB; memTotal = [long]$o.TotalVisibleMemorySize * 1KB; "
    "diskFree = $d.FreeSpace; diskTotal = $d.Size} | ConvertTo-Json -Compress"
    '"'
)
WINDOWS_SESSIONS = "quser"

# --- Linux (systemd; power via a polkit rule on the target) -----------------------

LINUX_SHUTDOWN = "systemctl poweroff --no-ask-password"
LINUX_REBOOT = "systemctl reboot --no-ask-password"
LINUX_SLEEP = "systemctl suspend --no-ask-password"
# Raw facts as key=value lines; the arithmetic happens in parse_linux_details().
LINUX_DETAILS = (
    "/bin/sh -c '"
    "echo candygoogy-details-v1; "
    'read -r up x < /proc/uptime; echo "uptime=$up"; '
    'read -r l1 l5 l15 x < /proc/loadavg; echo "load=$l1 $l5 $l15"; '
    'echo "ncpu=$(getconf _NPROCESSORS_ONLN 2>/dev/null)"; '
    'while read -r k v x; do case "$k" in '
    'MemTotal:) echo "memtotal_kb=$v";; MemAvailable:) echo "memavail_kb=$v";; '
    "esac; done < /proc/meminfo; "
    "read -r c1 < /proc/stat; sleep 1; read -r c2 < /proc/stat; "
    'echo "cpu1=$c1"; echo "cpu2=$c2"; '
    'df -Pk / | { read -r h; read -r line; echo "df=$line"; }'
    "'"
)
# show-session's Key=Value properties are stable across systemd versions;
# list-sessions' columns aren't. `who` is the fallback without systemd.
LINUX_SESSIONS = (
    "/bin/sh -c '"
    "echo candygoogy-sessions-v1; "
    "if command -v loginctl >/dev/null 2>&1; then "
    'echo "self=$XDG_SESSION_ID"; '
    'ids=$(loginctl list-sessions --no-legend --no-pager | while read -r id x; do echo "$id"; done); '
    '[ -n "$ids" ] && loginctl show-session -p Id -p Name -p Class -p Type -p TTY '
    "-p Remote -p RemoteHost -p State -p IdleHint $ids; "
    "else echo mode=who; who; fi"
    "'"
)

# --- macOS (power via a sudoers drop-in on the target) ------------------------------

MACOS_SHUTDOWN = "sudo -n /sbin/shutdown -h now"
MACOS_REBOOT = "sudo -n /sbin/shutdown -r now"
MACOS_SLEEP = "sudo -n /usr/bin/pmset sleepnow"
MACOS_DETAILS = (
    "/bin/sh -c '"
    "echo candygoogy-details-v1; "
    'echo "now=$(date +%s)"; '
    'echo "boottime=$(sysctl -n kern.boottime)"; '
    'echo "waketime=$(sysctl -n kern.waketime 2>/dev/null)"; '
    'echo "load=$(sysctl -n vm.loadavg)"; '
    'echo "ncpu=$(sysctl -n hw.logicalcpu)"; '
    'echo "memsize=$(sysctl -n hw.memsize)"; '
    'vm_stat | while read -r line; do echo "vm=$line"; done; '
    'd=/System/Volumes/Data; [ -d "$d" ] || d=/; '
    'df -Pk "$d" | { read -r h; read -r line; echo "df=$line"; }; '
    'echo "cpuline=$(top -l 2 -n 0 -s 1 | grep "^CPU usage" | tail -n 1)"'
    "'"
)
MACOS_SESSIONS = (
    "/bin/sh -c '"
    "echo candygoogy-sessions-v1; who; "
    'echo "hididle=$(ioreg -c IOHIDSystem -d 4 | grep HIDIdleTime | head -n 1)"'
    "'"
)


@dataclass(frozen=True)
class OsCommands:
    """Everything that differs between operating systems."""

    shutdown: str
    reboot: str
    sleep: str
    details: str
    sessions: str
    # The sleep call only returns after the machine wakes, so a timeout means it slept.
    sleep_session_hangs: bool
    # Its network card answers ARP while asleep (ARP offload), so a REACHABLE
    # entry without ping or SSH means "probably asleep" rather than "off or asleep".
    arp_means_asleep: bool
    # While it sleeps, another device (a Bonjour Sleep Proxy) may answer ARP for
    # it with its own MAC, so a different MAC isn't necessarily an IP conflict.
    arp_proxy_possible: bool
    # Wake-on-LAN works from a full shutdown, not only from sleep.
    wakes_from_off: bool
    # Lower-case stderr substrings meaning "not allowed", and what to set up then.
    denials: tuple[str, ...]
    denial_hint: str
    # How /status names the disk in the details line.
    disk_label: str

    def command(self, action: Action) -> str:
        match action:
            case "shutdown":
                return self.shutdown
            case "reboot":
                return self.reboot
            case "sleep":
                return self.sleep
            case "details":
                return self.details
            case "sessions":
                return self.sessions


WINDOWS = OsCommands(
    shutdown=WINDOWS_SHUTDOWN,
    reboot=WINDOWS_REBOOT,
    sleep=WINDOWS_SLEEP,
    details=WINDOWS_DETAILS,
    sessions=WINDOWS_SESSIONS,
    sleep_session_hangs=True,
    arp_means_asleep=True,
    arp_proxy_possible=False,
    wakes_from_off=True,
    denials=(),
    denial_hint="",
    disk_label="C:",
)
LINUX = OsCommands(
    shutdown=LINUX_SHUTDOWN,
    reboot=LINUX_REBOOT,
    sleep=LINUX_SLEEP,
    details=LINUX_DETAILS,
    sessions=LINUX_SESSIONS,
    sleep_session_hangs=False,
    arp_means_asleep=False,
    arp_proxy_possible=False,
    wakes_from_off=True,
    # logind also says "access denied" when a block inhibitor is active.
    denials=("access denied", "interactive authentication required"),
    denial_hint=(
        "Not allowed, or blocked by an inhibitor (e.g. an update in progress). "
        + "The bot's user needs the polkit rule on that machine; see docs/linux-target.md."
    ),
    disk_label="/",
)
MACOS = OsCommands(
    shutdown=MACOS_SHUTDOWN,
    reboot=MACOS_REBOOT,
    sleep=MACOS_SLEEP,
    details=MACOS_DETAILS,
    sessions=MACOS_SESSIONS,
    sleep_session_hangs=False,
    arp_means_asleep=False,
    arp_proxy_possible=True,
    wakes_from_off=False,
    denials=("a password is required", "may not run sudo", "not allowed to execute"),
    denial_hint=(
        "Not allowed: the bot's user needs the sudoers rule on the Mac; "
        + "see docs/macos-target.md."
    ),
    disk_label="disk",
)
COMMANDS: dict[str, OsCommands] = {"windows": WINDOWS, "linux": LINUX, "macos": MACOS}


def commands_for(os_name: str) -> OsCommands:
    return COMMANDS[os_name]


# --- What a power command's result means (all OSes) ---------------------------------

Outcome = Literal[
    "started",  # exit 0: the target accepted it
    "unconfirmed",  # the session closed before the exit status arrived: likely started
    "no_answer",  # shutdown/reboot timed out: check with the follow-up
    "asleep",  # sleep: the session froze, as it does when the machine sleeps
    "returned_early",  # Windows sleep returned instead of freezing: may not have slept
    "denied",  # not allowed: os_commands.denial_hint says what to set up
    "host_key",  # ssh refused: the machine's host key changed
    "failed",  # anything else: RunResult.describe()
]
# ssh's exit 255 with one of these: the target tore the session down first.
CLOSED_EARLY = ("closed by remote host", "connection reset", "broken pipe")
# ssh's exit 255 when its keepalive (ServerAliveInterval/CountMax in system.py)
# gave up on a silent server: "Timeout, server <host> not responding." The
# session froze, e.g. because the machine fell asleep or is going down: the same
# as the bot's own timeout, so both read alike. Every substring must match.
KEEPALIVE_TIMEOUT = ("timeout, server", "not responding")


def outcome(commands: OsCommands, action: Action, result: RunResult) -> Outcome:
    err = result.err.lower()
    keepalive_gave_up = result.code == 255 and all(s in err for s in KEEPALIVE_TIMEOUT)
    if result.timed_out or keepalive_gave_up:
        if action == "sleep":
            return "asleep"
        return "no_answer"
    if result.ok:
        if action == "sleep" and commands.sleep_session_hangs:
            return "returned_early"
        return "started"
    if result.code == 255 and "host key verification failed" in err:
        return "host_key"
    if result.code == 255 and any(s in err for s in CLOSED_EARLY):
        return "unconfirmed"
    if any(s in err for s in commands.denials):
        return "denied"
    return "failed"


# --- Details while awake ---------------------------------------------------------------


@dataclass(frozen=True)
class Details:
    boot: float | None = None  # seconds since last boot
    cpu: float | None = None  # load percent
    mem_free: float | None = None  # bytes
    mem_total: float | None = None
    disk_free: float | None = None  # bytes
    disk_total: float | None = None
    woke: float | None = None  # seconds since the last wake from sleep (macOS only)


def after_marker(out: str, marker: str) -> list[str] | None:
    """The lines after the last marker line (startup files may print before it)."""
    lines = out.splitlines()
    marks = [n for n, line in enumerate(lines) if line.strip() == marker]
    return lines[marks[-1] + 1 :] if marks else None


def key_values(lines: list[str]) -> dict[str, str]:
    """The first value of each key in key=value lines."""
    found: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() not in found:
            found[key.strip()] = value.strip()
    return found


DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
INTEGER = re.compile(r"[0-9]+")
# A `df -P` data line, read from the right: a filesystem name may contain spaces.
DF_LINE = re.compile(r"[ \t]([0-9]+)[ \t]+[0-9]+[ \t]+([0-9]+)[ \t]+[0-9]+%[ \t]+[^ \t].*$")
SYSCTL_SEC = re.compile(r"sec = ([0-9]+)")
PAGE_SIZE = re.compile(r"page size of ([0-9]+) bytes")
VM_PAGES = re.compile(r"^(.+):[ \t]+([0-9]+)\.$")
CPU_IDLE = re.compile(r"([0-9]+(?:\.[0-9]+)?)% idle")


def decimal(text: str | None) -> float | None:
    """A plain ASCII decimal, finite; None for anything else (missing, garbled, huge)."""
    if text is None or not DECIMAL.fullmatch(text.strip()):
        return None
    value = float(text.strip())
    return value if math.isfinite(value) else None


def disk(line: str | None) -> tuple[float | None, float | None]:
    """(free, total) bytes from a `df -Pk` line."""
    if line is None or not (match := DF_LINE.search(line)):
        return None, None
    return float(match[2]) * 1024, float(match[1]) * 1024


def parse_windows_details(out: str) -> Details | None:
    try:
        data = cast(object, json.loads(out))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    return Details(
        boot=number(fields.get("boot")),
        cpu=number(fields.get("cpu")),
        mem_free=number(fields.get("memFree")),
        mem_total=number(fields.get("memTotal")),
        disk_free=number(fields.get("diskFree")),
        disk_total=number(fields.get("diskTotal")),
    )


def cpu_percent(first: str | None, second: str | None) -> float | None:
    """Busy share between two /proc/stat "cpu" lines: user nice system idle iowait
    irq softirq steal; idle counts idle + iowait."""

    def counters(line: str | None) -> list[int] | None:
        fields = (line or "").split()
        if not fields or fields[0] != "cpu":
            return None
        values = fields[1:9]
        if len(values) < 4 or not all(INTEGER.fullmatch(v) and len(v) <= 20 for v in values):
            return None
        return [int(v) for v in values]

    a, b = counters(first), counters(second)
    if a is None or b is None or len(a) != len(b):
        return None
    total = sum(b) - sum(a)
    idle = sum(b[3:5]) - sum(a[3:5])
    return 100 * (total - idle) / total if total > 0 else None


def parse_linux_details(out: str) -> Details | None:
    if (lines := after_marker(out, DETAILS_MARKER)) is None:
        return None
    facts = key_values(lines)
    total_kb, avail_kb = decimal(facts.get("memtotal_kb")), decimal(facts.get("memavail_kb"))
    disk_free, disk_total = disk(facts.get("df"))
    return Details(
        boot=decimal(facts.get("uptime")),
        cpu=cpu_percent(facts.get("cpu1"), facts.get("cpu2")),
        mem_free=avail_kb * 1024 if avail_kb is not None else None,
        mem_total=total_kb * 1024 if total_kb is not None else None,
        disk_free=disk_free,
        disk_total=disk_total,
    )


def sysctl_seconds(text: str | None) -> float | None:
    """`{ sec = 1791570600, usec = 512345 } …` → 1791570600."""
    match = SYSCTL_SEC.search(text or "")
    return float(match[1]) if match and len(match[1]) <= 12 else None


def parse_macos_details(out: str) -> Details | None:
    if (lines := after_marker(out, DETAILS_MARKER)) is None:
        return None
    facts = key_values(lines)
    now = decimal(facts.get("now"))
    boot_at, wake_at = sysctl_seconds(facts.get("boottime")), sysctl_seconds(facts.get("waketime"))
    boot = now - boot_at if now is not None and boot_at is not None and now >= boot_at else None
    woke = None
    if now is not None and wake_at and boot_at is not None and boot_at < wake_at <= now:
        woke = now - wake_at
    # Memory: (anonymous - purgeable + wired + compressed) pages, close to
    # Activity Monitor's "Memory Used". The page size is in vm_stat's header.
    vm = [line.partition("=")[2].strip() for line in lines if line.startswith("vm=")]
    page = next((int(m[1]) for v in vm if (m := PAGE_SIZE.search(v)) and len(m[1]) <= 9), None)
    pages = {
        m[1].strip('" '): int(m[2]) for v in vm if (m := VM_PAGES.match(v)) and len(m[2]) <= 15
    }
    mem_total = decimal(facts.get("memsize"))
    mem_free = None
    needed = (
        "Anonymous pages",
        "Pages purgeable",
        "Pages wired down",
        "Pages occupied by compressor",
    )
    if page and mem_total is not None and all(k in pages for k in needed):
        used = (
            pages["Anonymous pages"]
            - pages["Pages purgeable"]
            + pages["Pages wired down"]
            + pages["Pages occupied by compressor"]
        ) * page
        mem_free = mem_total - used if 0 <= used <= mem_total else None
    idle = CPU_IDLE.search(facts.get("cpuline") or "")
    cpu = 100 - float(idle[1]) if idle and float(idle[1]) <= 100 else None
    disk_free, disk_total = disk(facts.get("df"))
    return Details(boot, cpu, mem_free, mem_total, disk_free, disk_total, woke)


PARSE_DETAILS = {
    "windows": parse_windows_details,
    "linux": parse_linux_details,
    "macos": parse_macos_details,
}


# --- Who's signed in --------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    user: str
    how: str  # e.g. "active, in use now", "graphical, active", "remote, idle"


def describe_idle(idle: str) -> str:
    """quser's IDLE TIME column: "none" or "." means in use right now."""
    return "in use now" if idle in ("none", ".") else f"idle {idle}"


def parse_windows_sessions(result: RunResult) -> list[Session] | None:
    """`quser`. Exit 1 with "No User exists for *" means nobody is signed in."""
    if not result.ok:
        return [] if "No User exists" in result.err else None
    sessions: list[Session] = []
    for line in result.out.splitlines()[1:]:  # skip the header row
        tokens = line.lstrip(" >").split()  # ">" marks quser's own session
        state = next((t for t in tokens[1:] if t in ("Active", "Disc")), None)
        if not tokens or state is None:
            continue
        after = tokens.index(state) + 1
        kind = "active" if state == "Active" else "disconnected"
        idle = tokens[after] if after < len(tokens) else "?"
        sessions.append(Session(tokens[0], f"{kind}, {describe_idle(idle)}"))
    return sessions


def who_sessions(lines: list[str]) -> list[Session]:
    """`who` lines: user, terminal, date, then "(host)" for a remote login. The
    bot's own SSH commands have no terminal, so they're never listed."""
    sessions: list[Session] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) < 2:
            continue
        host = tokens[-1] if tokens[-1].startswith("(") and tokens[-1].endswith(")") else ""
        host = host.strip("()")
        if tokens[1] == "console":
            how = "console"
        elif host and not host.startswith(("tty", ":")):
            how = "remote"
        else:
            how = "terminal"
        sessions.append(Session(tokens[0], how))
    return sessions


GRAPHICAL = ("x11", "wayland", "mir")


def parse_linux_sessions(result: RunResult, ssh_user: str) -> list[Session] | None:
    """loginctl show-session blocks (or `who` lines). Leaves out the bot's own
    sessions (its user, remote, no terminal), greeters and closing sessions.
    The remote host is never shown."""
    if not result.ok or (lines := after_marker(result.out, SESSIONS_MARKER)) is None:
        return None
    if any(line.strip() == "mode=who" for line in lines):
        return who_sessions([line for line in lines if line.strip() != "mode=who"])
    self_id = next(
        (line.partition("=")[2].strip() for line in lines if line.startswith("self=")), ""
    )
    blocks: list[dict[str, str]] = [{}]
    for line in lines:
        if not line.strip():
            blocks.append({})
        elif not line.startswith("self="):
            key, sep, value = line.partition("=")
            if sep:
                blocks[-1][key.strip()] = value.strip()
    sessions: list[Session] = []
    for b in blocks:
        name = b.get("Name", "")
        if not name or not b.get("Id") or b["Id"] == self_id:
            continue
        if name == ssh_user and b.get("Remote") == "yes" and not b.get("TTY"):
            continue  # the bot's own session, e.g. the details call running beside this one
        if not b.get("Class", "").startswith("user") or b.get("State") not in ("active", "online"):
            continue
        if b.get("Type") in GRAPHICAL:
            kind = "graphical"
        elif b.get("Remote") == "yes":
            kind = "remote"
        else:
            kind = "console"
        sessions.append(
            Session(name, f"{kind}, {'idle' if b.get('IdleHint') == 'yes' else 'active'}")
        )
    return sessions


HID_IDLE = re.compile(r"([0-9]+)[^0-9]*$")
# Less keyboard/mouse idle time than this means someone is at the Mac.
IN_USE_SECONDS = 60


def parse_macos_sessions(result: RunResult) -> list[Session] | None:
    """`who` lines, and HIDIdleTime (nanoseconds since the last input) for the console."""
    if not result.ok or (lines := after_marker(result.out, SESSIONS_MARKER)) is None:
        return None
    hid_line = next((line for line in lines if line.startswith("hididle=")), "")
    match = HID_IDLE.search(hid_line.partition("=")[2])
    idle = int(match[1]) / 1e9 if match and len(match[1]) <= 25 else None
    sessions = who_sessions([line for line in lines if not line.startswith("hididle=")])
    if idle is not None:
        activity = "in use now" if idle < IN_USE_SECONDS else f"idle {duration(idle)}"
        sessions = [
            Session(s.user, f"{s.how}, {activity}") if s.how == "console" else s for s in sessions
        ]
    return sessions


def parse_sessions(os_name: str, result: RunResult, ssh_user: str) -> list[Session] | None:
    match os_name:
        case "linux":
            return parse_linux_sessions(result, ssh_user)
        case "macos":
            return parse_macos_sessions(result)
        case _:
            return parse_windows_sessions(result)


def describe_sessions(sessions: list[Session]) -> str:
    """ "<code>alice</code> (active, in use now), <code>bob</code> (disconnected, idle 1:02)"."""
    return ", ".join(f"<code>{escape(s.user)}</code> ({escape(s.how)})" for s in sessions)
