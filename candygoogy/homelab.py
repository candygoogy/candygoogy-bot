"""/homelab: the homelab's own health, and alerts when something needs attention."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from candygoogy import dockerapi, system
from candygoogy.config import (
    BACKUP_DIR,
    BACKUP_MAX_AGE_HOURS,
    HOMELAB_DISKS,
    HOMELAB_HOST_ROOT,
    HOMELAB_TEMP_ALERT_C,
    IN_CONTAINER,
    REPO_DIR,
    STATE_DIR,
    App,
)
from candygoogy.dockerapi import Container
from candygoogy.messages import card, duration, notify_allowed
from candygoogy.probes import LOW_DISK_FRACTION, size
from candygoogy.settings import is_number
from candygoogy.state import load_json, number, save_json
from candygoogy.tasks import repeat

log = logging.getLogger(__name__)

# --- Readings (all read-only) ---------------------------------------------------

PROC = Path("/proc")
# HOMELAB_DISKS: "/" is the system disk; the others are separate mounts that
# must stay mounted.
DISKS = HOMELAB_DISKS
# Your backup job writes each snapshot to a folder in BACKUP_DIR named after when
# it started, YYYYMMDD-HHMMSS (e.g. 20261006-021500), and creates this file in it
# once every step succeeded. A folder without it is a failed or running backup.
SNAPSHOT_NAME = re.compile(r"20[0-9]{6}-[0-9]{6}")
COMPLETE_MARKER = ".complete"
# Debian's packages create /run/reboot-required when an update (e.g. a new
# kernel) only takes effect after a reboot, and list themselves in .pkgs.
RUN = "/run"
REBOOT_REQUIRED = "reboot-required"
# HOMELAB_HOST_ROOT: where the host's / is mounted in a container (read-only),
# e.g. /host. Disks, mount checks and RUN are read under it; /proc needs no
# prefix, since uptime, load and memory are the host's anyway.
HOST_ROOT = HOMELAB_HOST_ROOT
# Temperature sensors, read from this process's own /sys and never under
# HOST_ROOT: sysfs is a mount of its own, so a bind mount of the host's / usually
# doesn't contain it, while Docker mounts the host's sysfs read-only into the
# container without masking class/thermal or class/hwmon.
SYS = Path("/sys")
# hwmon drivers for the CPU's (or SoC's) temperature, read only when no thermal
# zone has a reading (e.g. AMD's k10temp); others (nvme, iwlwifi, ...) never are.
CPU_HWMON_DRIVERS = frozenset({"coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal"})
# Above this a reading is junk; at or below 0 °C it's a sensor's "no value"
# (some zones say -273200).
MAX_PLAUSIBLE_C = 150
# HOMELAB_TEMP_ALERT_C: alert at or above it (None: never). An open alert clears
# only TEMP_HYSTERESIS_C below it, so a reading hovering there doesn't alert and
# clear every check.
TEMP_ALERT_C = HOMELAB_TEMP_ALERT_C
TEMP_HYSTERESIS_C = 5
# Sensor labels come from sysfs; longer ones are cut.
MAX_SENSOR_LABEL_CHARS = 40


def on_host(path: str) -> str:
    """Where a host path is read: under HOST_ROOT if that is set."""
    if HOST_ROOT is None or HOST_ROOT == "/":
        return path
    return HOST_ROOT if path == "/" else HOST_ROOT + path


@dataclass(frozen=True)
class Disk:
    path: str
    mounted: bool
    space: tuple[float, float] | None  # (free, total) bytes; None if unreadable


@dataclass(frozen=True)
class Backups:
    readable: bool
    last: str | None = None  # newest complete snapshot
    finished: float | None = None  # when it completed (the marker's mtime)
    size: float = 0
    unfinished: str | None = None  # a newer snapshot without the marker


@dataclass(frozen=True)
class Health:
    uptime: float | None
    load: tuple[float, float, float] | None
    memory: tuple[float, float] | None  # (used, total) bytes
    disks: list[Disk]
    failed: list[str] | None  # failed systemd units; None if they couldn't be listed
    backups: Backups | None  # None: backup monitoring is off (no BACKUP_DIR)
    containers: list[Container] | None = None  # None if Docker couldn't be asked
    updates: list[str] | None = None  # upgradable packages; None if apt couldn't be asked
    reboot: list[str] | None = None  # packages waiting for a reboot; None if none is
    temperature: tuple[str, float] | None = None  # (sensor label, °C); None: no sensor
    # Programs that aren't installed ("systemctl", "apt"), or "docker" if
    # nothing answers at DOCKER_HOST: their reading is None, and the card says
    # so instead of "Couldn't check".
    missing: frozenset[str] = frozenset()
    # In a container, units, updates and reboots belong to the host, so they
    # weren't read (failed, updates and reboot are None); reboots are, though,
    # through the host's / at HOMELAB_HOST_ROOT (host_root).
    in_container: bool = False
    host_root: bool = False


def read_proc(name: str) -> str | None:
    try:
        return (PROC / name).read_text()
    except OSError:
        return None


def read_uptime() -> float | None:
    try:
        return float((read_proc("uptime") or "").split()[0])
    except (IndexError, ValueError):
        return None


def read_load() -> tuple[float, float, float] | None:
    try:
        one, five, fifteen = (float(x) for x in (read_proc("loadavg") or "").split()[:3])
    except ValueError:
        return None
    return one, five, fifteen


def read_memory() -> tuple[float, float] | None:
    """(used, total) bytes, where used is what isn't available to new programs."""
    fields: dict[str, float] = {}
    for line in (read_proc("meminfo") or "").splitlines():
        name, _, value = line.partition(":")
        if value.strip().endswith(" kB") and is_number(value.split()[0]):
            fields[name] = float(value.split()[0]) * 1024
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return fields["MemTotal"] - fields["MemAvailable"], fields["MemTotal"]


def read_sys(path: Path) -> str | None:
    """A sysfs file's text; None if unreadable (some zones answer EINVAL)."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def celsius(millidegrees: str | None) -> float | None:
    """A sensor's millidegrees as °C, or None if it isn't a plausible reading."""
    if millidegrees is None or not is_number(millidegrees) or len(millidegrees) > 9:
        return None  # also "-273200": is_number() takes digits only
    value = int(millidegrees) / 1000
    return value if 0 < value <= MAX_PLAUSIBLE_C else None


def read_temperature() -> tuple[str, float] | None:
    """The hottest plausible CPU temperature as (sensor label, °C): from the
    thermal zones, else from the CPU's hwmon driver. None if there's no sensor."""
    readings: list[tuple[str, float]] = []
    for zone in sorted((SYS / "class" / "thermal").glob("thermal_zone*")):
        if (value := celsius(read_sys(zone / "temp"))) is not None:
            readings.append((read_sys(zone / "type") or zone.name, value))
    if not readings:
        for hwmon in sorted((SYS / "class" / "hwmon").glob("hwmon*")):
            if (name := read_sys(hwmon / "name")) not in CPU_HWMON_DRIVERS:
                continue
            for sensor in sorted(hwmon.glob("temp*_input")):
                if (value := celsius(read_sys(sensor))) is not None:
                    readings.append((name, value))
    if not readings:
        return None
    label, value = max(readings, key=lambda reading: reading[1])
    return label[:MAX_SENSOR_LABEL_CHARS], value


def disk_space(path: str) -> tuple[float, float] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return float(usage.free), float(usage.total)


def is_mount(path: str) -> bool:
    return os.path.ismount(path)


async def failed_units(missing: set[str]) -> list[str] | None:
    """Failed systemd units; None (and "systemctl" in `missing` if it isn't
    installed) if they couldn't be listed."""
    result = await system.run(
        "systemctl", "list-units", "--state=failed", "--no-pager", "--output=json", timeout=10
    )
    if result.missing:
        missing.add("systemctl")
    if not result.ok:
        return None
    try:
        data = cast(object, json.loads(result.out))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    units: list[str] = []
    for entry in cast(list[object], data):
        unit = cast(dict[str, object], entry).get("unit") if isinstance(entry, dict) else None
        if isinstance(unit, str):
            units.append(unit)
    return sorted(units)


async def containers(missing: set[str]) -> list[Container] | None:
    """Every container, running or not, from Docker's API (see dockerapi.py).
    None (and "docker" in `missing` if nothing answers at DOCKER_HOST) if
    Docker couldn't be asked."""
    try:
        return await dockerapi.list_containers()
    except dockerapi.UnreachableError as e:
        log.info("Docker isn't reachable (%s)", e.__cause__ or "no answer")
        missing.add("docker")
        return None


async def upgradable(missing: set[str]) -> list[str] | None:
    """Packages with an update, from the lists apt-daily.timer last downloaded.
    None (and "apt" in `missing` if it isn't installed) if apt couldn't be asked."""
    result = await system.run("apt", "list", "--upgradable", timeout=30)
    if result.missing:
        missing.add("apt")
    if not result.ok:
        return None
    # "example/stable 1.2.3 amd64 [upgradable from: 1.2.2]", after a
    # "Listing..." header (localised, but never containing a slash).
    names = {line.split("/", 1)[0] for line in result.out.splitlines() if "/" in line}
    return sorted(names)


def read_reboot_required() -> list[str] | None:
    """Packages waiting for a reboot ([] if unnamed), or None if none is needed."""
    run = Path(on_host(str(RUN)))
    if not (run / REBOOT_REQUIRED).exists():
        return None
    try:
        return sorted(set((run / f"{REBOOT_REQUIRED}.pkgs").read_text().split()))
    except OSError:
        return []


def folder_size(folder: Path) -> float:
    return float(sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()))


@dataclass(frozen=True)
class Snapshot:
    name: str
    finished: float | None  # when it completed (the marker's mtime); None if it didn't


def list_snapshots(folder: Path) -> list[Snapshot]:
    """Every snapshot folder, newest first. Raises OSError if it can't be read."""
    names = sorted(
        (p.name for p in folder.iterdir() if p.is_dir() and SNAPSHOT_NAME.fullmatch(p.name)),
        reverse=True,  # the names are timestamps, so newest first
    )
    snapshots: list[Snapshot] = []
    for name in names:
        marker = folder / name / COMPLETE_MARKER
        snapshots.append(Snapshot(name, marker.stat().st_mtime if marker.exists() else None))
    return snapshots


def backups_monitored() -> bool:
    """Whether BACKUP_DIR is set. Unset, there are no backup lines or alerts anywhere."""
    return BACKUP_DIR is not None


def backup_snapshots() -> list[Snapshot]:
    """Every snapshot in BACKUP_DIR, newest first; none if backup monitoring
    is off. Raises OSError."""
    return list_snapshots(BACKUP_DIR) if BACKUP_DIR is not None else []


def snapshot_size(name: str) -> float:
    """Raises OSError (also if backup monitoring is off)."""
    if BACKUP_DIR is None:
        raise FileNotFoundError("backup monitoring is off")
    return folder_size(BACKUP_DIR / name)


def read_backups(folder: Path) -> Backups:
    try:
        unfinished: str | None = None
        for s in list_snapshots(folder):
            if s.finished is None:
                unfinished = unfinished or s.name
                continue
            return Backups(True, s.name, s.finished, folder_size(folder / s.name), unfinished)
    except OSError:
        return Backups(readable=False)
    return Backups(True, unfinished=unfinished)


async def read_health() -> Health:
    # Shown and keyed by the host path, read under HOST_ROOT.
    disks = [
        Disk(path, path == "/" or is_mount(on_host(path)), disk_space(on_host(path)))
        for path in DISKS
    ]
    missing: set[str] = set()
    if IN_CONTAINER:  # units, updates and reboots belong to the host
        failed, updates = None, None
        reboot = read_reboot_required() if HOST_ROOT is not None else None
        docker = await containers(missing)
    else:
        failed, docker, updates = await asyncio.gather(
            failed_units(missing), containers(missing), upgradable(missing)
        )
        reboot = read_reboot_required()
    return Health(
        uptime=read_uptime(),
        load=read_load(),
        memory=read_memory(),
        disks=disks,
        failed=failed,
        backups=read_backups(BACKUP_DIR) if BACKUP_DIR is not None else None,
        containers=docker,
        updates=updates,
        reboot=reboot,
        temperature=read_temperature(),
        missing=frozenset(missing),
        in_container=IN_CONTAINER,
        host_root=HOST_ROOT is not None,
    )


# --- Problems -----------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    title: str  # HTML, e.g. "<code>/srv/data</code> is almost full"
    detail: str


# How a stopped container's state reads in "<code>app</code> is …".
CONTAINER_STATES = {"exited": "stopped", "created": "not started"}


def problems(health: Health, now: float) -> tuple[dict[str, Problem], set[str]]:
    """What needs attention, keyed for alerting, and the key prefixes that
    couldn't be checked (an alert under those is neither repeated nor resolved).
    A program that isn't installed, or a reading that needs the host while in a
    container, is such an unknown: never a problem of its own."""
    found: dict[str, Problem] = {}
    unknown: set[str] = set()
    for disk in health.disks:
        path = f"<code>{escape(disk.path)}</code>"
        if not disk.mounted:
            found[f"mount:{disk.path}"] = Problem(
                f"{path} is not mounted", "Anything written there fills the system disk instead."
            )
        elif disk.space is None:
            unknown.add(f"disk:{disk.path}")
        elif disk.space[1] and disk.space[0] / disk.space[1] < LOW_DISK_FRACTION:
            free, total = disk.space
            found[f"disk:{disk.path}"] = Problem(
                f"{path} is almost full", f"{size(free)} free of {size(total)}."
            )
    if health.failed is None:
        unknown.add("unit:")
    for unit in health.failed or []:
        found[f"unit:{unit}"] = Problem(
            f"<code>{escape(unit)}</code> failed",
            f"See <code>systemctl status {escape(unit)}</code>.",
        )
    if health.containers is None:
        unknown.add("container:")
    for c in health.containers or []:
        if c.state == "running" and c.health != "unhealthy":
            continue
        if c.state == "restarting":
            what = "keeps restarting"
        elif c.state == "running":
            what = "is unhealthy"
        else:
            what = f"is {escape(CONTAINER_STATES.get(c.state, c.state))}"
        status = f"{escape(c.status)}. " if c.status else ""
        found[f"container:{c.name}"] = Problem(
            f"<code>{escape(c.name)}</code> {what}",
            f"{status}See <code>docker logs {escape(c.name)}</code>.",
        )
    if health.in_container and not health.host_root:
        unknown.add("reboot")
    elif health.reboot is not None:
        found["reboot"] = Problem(
            "Reboot required",
            f"Needed by {', '.join(escape(p) for p in health.reboot)}."
            if health.reboot
            else "An update installed since the last boot needs it.",
        )
    if TEMP_ALERT_C is None or health.temperature is None:
        unknown.add("temp")  # no alert set, or no sensor: an open alert stays as it is
    else:
        label, value = health.temperature
        if value >= TEMP_ALERT_C:
            found["temp"] = Problem(
                "Running hot", f"{escape(label)} at {value:.0f} °C (alert at {TEMP_ALERT_C} °C)."
            )
        elif value >= TEMP_ALERT_C - TEMP_HYSTERESIS_C:
            unknown.add("temp")  # just below the alert: neither opens nor clears one
    b = health.backups
    if b is None:  # backup monitoring is off
        unknown.add("backup")
        return found, unknown
    folder = f"<code>{escape(str(BACKUP_DIR))}</code>"
    if not b.readable:
        found["backup"] = Problem("Can't read the backups folder", f"{folder}.")
    elif b.last is None or b.finished is None:
        found["backup"] = Problem("No completed backup", f"Nothing in {folder} is marked complete.")
    elif now - b.finished > BACKUP_MAX_AGE_HOURS * 3600:
        found["backup"] = Problem(
            "Backups are overdue",
            f"Last completed backup {duration(now - b.finished)} ago ({escape(b.last)}).",
        )
    return found, unknown


# --- The /homelab card ------------------------------------------------------------


def backup_size(num_bytes: float) -> str:
    """Snapshots are often megabytes, which size() would show as "0.0 GB"."""
    if num_bytes >= 1024**3:
        return size(num_bytes)
    mib = num_bytes / 1024**2
    return f"{mib:.1f} MB" if mib < 10 else f"{mib:.0f} MB"


# What the card says when a program isn't installed.
NOT_INSTALLED = {
    "systemctl": "<i>systemd isn't available, so services aren't checked.</i>",
    "docker": "<i>Docker isn't reachable, so containers aren't checked.</i>",
    "apt": "<i>apt isn't installed, so updates aren't checked.</i>",
}
IN_CONTAINER_LINE = "<i>Services, updates and reboots aren't checked inside a container.</i>"
# With the host's / mounted (HOMELAB_HOST_ROOT), a pending reboot is read from it.
IN_CONTAINER_HOST_ROOT_LINE = "<i>Services and updates aren't checked inside a container.</i>"


def containers_line(docker: list[Container] | None, missing: frozenset[str]) -> str:
    if docker is None:
        if "docker" in missing:
            return NOT_INSTALLED["docker"]
        return "<i>Couldn't check containers.</i>"
    up = sum(c.state == "running" for c in docker)
    if up == len(docker):
        return f"🐳 All {up} containers running." if up else "🐳 No containers."
    return f"🐳 {up} of {len(docker)} containers running."


# More package names than this are summed up as "and N more".
MAX_UPDATES_LISTED = 3


def updates_line(updates: list[str] | None) -> str:
    if updates is None:
        return "<i>Couldn't check for updates.</i>"
    if not updates:
        return "📦 No updates waiting."
    names = ", ".join(escape(u) for u in updates[:MAX_UPDATES_LISTED])
    if len(updates) > MAX_UPDATES_LISTED:
        names += f" and {len(updates) - MAX_UPDATES_LISTED} more"
    plural = "s" if len(updates) > 1 else ""
    return f"📦 {len(updates)} update{plural} available: {names}."


def backup_line(b: Backups | None, found: dict[str, Problem], now: float) -> str | None:
    """The last good backup, unless a warning about backups says it instead (or
    backup monitoring is off)."""
    if b is None or "backup" in found or b.last is None or b.finished is None:
        return None
    return f"💾 Last backup {duration(now - b.finished)} ago ({backup_size(b.size)})."


def warning_lines(found: dict[str, Problem]) -> list[str]:
    return [f"⚠️ {p.title}. {p.detail}" for p in found.values()]


def health_lines(health: Health, now: float) -> list[str]:
    found, _ = problems(health, now)
    lines: list[str] = []
    up: list[str] = []
    if health.uptime is not None:
        up.append(f"Up {duration(health.uptime)}")
    if health.load is not None:
        one, five, fifteen = health.load
        up.append(f"load {one:.2f} {five:.2f} {fifteen:.2f} ({os.cpu_count() or '?'} cores)")
    # A temperature that's alerting is described once, by the warning below.
    if health.temperature is not None and "temp" not in found:
        up.append(f"{health.temperature[1]:.0f} °C")
    if up:
        lines.append(" · ".join(up))
    if health.memory is not None:
        used, total = health.memory
        lines.append(f"RAM {size(used)} / {size(total)}")
    # Disks with a problem are described once, by the warning below.
    free = [
        f"<code>{escape(d.path)}</code> {size(d.space[0])} free"
        for d in health.disks
        if d.space is not None and f"disk:{d.path}" not in found and f"mount:{d.path}" not in found
    ]
    if free:
        lines.append(" · ".join(free))
    if health.in_container:
        lines.append(IN_CONTAINER_HOST_ROOT_LINE if health.host_root else IN_CONTAINER_LINE)
    elif health.failed == []:
        lines.append("✅ No failed services.")
    elif "systemctl" in health.missing:
        lines.append(NOT_INSTALLED["systemctl"])
    elif health.failed is None:
        lines.append("<i>Couldn't check services.</i>")
    lines.append(containers_line(health.containers, health.missing))
    if not health.in_container:
        if health.updates is None and "apt" in health.missing:
            lines.append(NOT_INSTALLED["apt"])
        else:
            lines.append(updates_line(health.updates))
    if backup := backup_line(health.backups, found, now):
        lines.append(backup)
    lines += warning_lines(found)
    if health.backups is not None and health.backups.unfinished is not None:
        name = escape(health.backups.unfinished)
        lines.append(f"<i>{name} didn't finish (failed, or still running).</i>")
    return lines


def summary_lines(health: Health, now: float) -> list[str]:
    """The homelab's part of the daily summary: problems, last backup, updates."""
    found, _ = problems(health, now)
    plural = "s" if len(found) > 1 else ""
    lines = [f"🖥 Homelab: {len(found)} problem{plural}." if found else "🖥 Homelab: all good."]
    lines += warning_lines(found)
    if backup := backup_line(health.backups, found, now):
        lines.append(backup)
    if health.updates:
        lines.append(updates_line(health.updates))
    return lines


async def homelab_message() -> str:
    return card("🖥", "Homelab", *health_lines(await read_health(), time.time()))


# --- Alerts -------------------------------------------------------------------
# Every CHECK_INTERVAL_SECONDS the bot re-reads the homelab's health and sends
# one message when a problem appears and one when it clears. Open alerts are
# saved, so a restart neither repeats them nor forgets to say they cleared.

ALERTS_FILE = STATE_DIR / "homelab_alerts.json"
CHECK_INTERVAL_SECONDS = 600.0
# The first check waits a little after startup, e.g. for disks to mount after a reboot.
FIRST_CHECK_DELAY_SECONDS = 60.0

_open_alerts: dict[str, str] = {}  # problem key -> title, alerted and not yet cleared


def save_alerts() -> None:
    """A failure only risks a repeated alert after a restart."""
    _ = save_json(ALERTS_FILE, _open_alerts, "homelab alerts")


def load_alerts() -> None:
    if (data := load_json(ALERTS_FILE, "homelab alerts")) is None:
        return
    if not isinstance(data, dict) or not all(
        isinstance(v, str) for v in cast(dict[str, object], data).values()
    ):
        log.warning("Ignoring malformed homelab alerts file %s", ALERTS_FILE)
        return
    _open_alerts.clear()
    _open_alerts.update(cast(dict[str, str], data))


# --- Problem history ----------------------------------------------------------------
# When each alert went out and when it cleared, for the weekly report. Saved, so a
# restart keeps the week.

INCIDENTS_FILE = STATE_DIR / "homelab_history.json"
# Problems that cleared longer ago than this are forgotten.
INCIDENTS_KEPT_SECONDS = 8 * 24 * 3600


@dataclass
class Incident:
    key: str
    title: str  # HTML, like Problem.title
    start: float  # when the alert went out
    end: float | None = None  # when "back to normal" went out; None while open


_incidents: list[Incident] = []  # oldest first


def save_incidents() -> None:
    """A failure only loses history after a restart."""
    data = [[i.key, i.title, i.start, i.end] for i in _incidents]
    _ = save_json(INCIDENTS_FILE, data, "the homelab's problem history")


def parse_incidents(data: object) -> list[Incident] | None:
    """What save_incidents() wrote, or None if it isn't that."""
    if not isinstance(data, list):
        return None
    incidents: list[Incident] = []
    for entry in cast(list[object], data):
        if not isinstance(entry, list) or len(cast(list[object], entry)) != 4:
            return None
        key, title, start, end = cast(list[object], entry)
        if (
            not isinstance(key, str)
            or not isinstance(title, str)
            or (start := number(start)) is None
        ):
            return None
        if end is not None and number(end) is None:
            return None
        incidents.append(Incident(key, title, start, number(end)))
    return incidents


def load_incidents() -> None:
    if (data := load_json(INCIDENTS_FILE, "homelab problem history")) is None:
        return
    if (incidents := parse_incidents(data)) is None:
        log.warning("Ignoring malformed homelab problem history file %s", INCIDENTS_FILE)
        return
    _incidents[:] = incidents


def incident_opened(key: str, title: str, now: float) -> None:
    _incidents.append(Incident(key, title, now))


def incident_cleared(key: str, now: float) -> None:
    for incident in reversed(_incidents):
        if incident.key == key and incident.end is None:
            incident.end = now
            return


def incidents_since(start: float) -> list[Incident]:
    """Problems open at any point from `start` on, oldest first."""
    return [i for i in _incidents if i.end is None or i.end >= start]


def switched_off(key: str) -> bool:
    """An alert whose check is turned off: backups without BACKUP_DIR; the
    temperature without HOMELAB_TEMP_ALERT_C; units, and reboots without
    HOMELAB_HOST_ROOT, in a container."""
    if key == "backup":
        return BACKUP_DIR is None
    if key == "temp":
        return TEMP_ALERT_C is None
    if key == "reboot":
        return IN_CONTAINER and HOST_ROOT is None
    return IN_CONTAINER and key.startswith("unit:")


def drop_switched_off_alerts(now: float) -> None:
    """At startup: forget open alerts whose check is now off, without a message.
    They'd otherwise stay open for good, and hold back the first real alert once
    the check is turned back on. Their incidents end now."""
    dropped = [key for key in _open_alerts if switched_off(key)]
    for key in dropped:
        del _open_alerts[key]
        incident_cleared(key, now)
    if dropped:
        log.info("Dropped %d open homelab alert(s) for checks that are off", len(dropped))
        save_alerts()
        save_incidents()


async def check_and_alert(app: App) -> None:
    now = time.time()
    found, unknown = problems(await read_health(), now)
    changed = False
    for key, problem in found.items():
        if key in _open_alerts:
            continue
        text = card("⚠️", f"Homelab: {problem.title}", problem.detail, "<i>Details: /homelab</i>")
        # No reboot button where the bot can't reboot (in a container).
        buttons = REBOOT_BUTTONS if key == "reboot" and reboot_refusal() is None else None
        if await notify_allowed(app, text, "a homelab alert", buttons):  # else retried next check
            _open_alerts[key] = problem.title
            incident_opened(key, problem.title, now)
            changed = True
    for key, title in list(_open_alerts.items()):
        if key in found or any(key.startswith(prefix) for prefix in unknown):
            continue
        back = card("✅", "Homelab: back to normal", f"Was: {title}.")
        if await notify_allowed(app, back, "a homelab alert"):
            del _open_alerts[key]
            incident_cleared(key, now)
            changed = True
    if changed:
        _incidents[:] = [
            i for i in _incidents if i.end is None or i.end >= now - INCIDENTS_KEPT_SECONDS
        ]
        save_alerts()
        save_incidents()


# --- Rebooting -------------------------------------------------------------------
# A button under /homelab and the "Reboot required" alert asks to reboot the
# homelab (see handlers.py). The bot runs as a normal user, so it asks
# systemd-logind, and deploy/50-candygoogy-reboot.rules lets polkit allow it.
# Not in a container: the reboot is the host's (REBOOT_UNAVAILABLE says so).

# Callback data: "ask:homelab_reboot" asks, "confirm:homelab_reboot:<issued>" reboots.
HOMELAB_REBOOT = "homelab_reboot"
REBOOT_BUTTON = InlineKeyboardButton("🔄 Reboot homelab", callback_data=f"ask:{HOMELAB_REBOOT}")
REBOOT_BUTTONS = InlineKeyboardMarkup([[REBOOT_BUTTON]])
REBOOT_UNAVAILABLE = "The bot runs in a container, so it can't reboot the homelab."
# When a reboot was asked for, so the next start can say how it went.
REBOOT_FILE = STATE_DIR / "homelab_reboot.json"
POLKIT_RULE = REPO_DIR / "deploy" / "50-candygoogy-reboot.rules"


def reboot_refusal() -> str | None:
    """Why the homelab can't be rebooted from here, or None if it can."""
    return REBOOT_UNAVAILABLE if IN_CONTAINER else None


async def request_reboot() -> str | None:
    """None if logind accepted (this process is then stopped within seconds),
    otherwise a message saying why not."""
    if refusal := reboot_refusal():
        return card("✖️", "Not rebooted", refusal)
    _ = save_json(REBOOT_FILE, {"at": time.time()}, "the reboot time")
    result = await system.run("systemctl", "reboot", "--no-ask-password", timeout=15)
    if result.ok:
        return None
    with contextlib.suppress(OSError):
        REBOOT_FILE.unlink()
    if "access denied" in result.err.lower() or "authentication required" in result.err.lower():
        return card(
            "🔒",
            "Not allowed to reboot the homelab",
            "Install the polkit rule once, on the homelab:",
            f"<code>sudo install -m 644 {escape(str(POLKIT_RULE))} /etc/polkit-1/rules.d/</code>",
        )
    return result.describe("Homelab reboot")


def reboot_outcome() -> str | None:
    """After a reboot the bot asked for: one line for the startup report."""
    data = load_json(REBOOT_FILE, "reboot time")
    with contextlib.suppress(OSError):
        REBOOT_FILE.unlink()
    asked = number(cast(dict[str, object], data).get("at")) if isinstance(data, dict) else None
    uptime = read_uptime()
    if asked is None or uptime is None:
        return None
    now = time.time()
    if now - uptime < asked:
        return f"⚠️ The homelab reboot asked for {duration(now - asked)} ago didn't happen."
    return f"✅ Homelab rebooted: back {duration(now - asked)} after you confirmed."


async def watch(app: App) -> None:
    """Runs from startup until the bot stops (see app.py)."""
    load_alerts()
    load_incidents()
    drop_switched_off_alerts(time.time())
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    await repeat(lambda: check_and_alert(app), CHECK_INTERVAL_SECONDS, "Homelab check")
