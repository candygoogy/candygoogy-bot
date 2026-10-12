"""/about: which code is running, since when, whether timers persist, and in
which time zone.

The commit is read once at startup, because that is the code this process
actually loaded. /about also reads the checkout again, so a `git pull` that
wasn't followed by a restart shows up as "newer code checked out".
"""

import os
import re
import sys
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from candygoogy import __version__, system
from candygoogy.auth import authorized
from candygoogy.config import BUILD_COMMIT, REPO_DIR, STARTED_AT, STATE_DIR
from candygoogy.messages import card, duration, reply, typing
from candygoogy.settings import is_number

# Fields separated by the ASCII unit separator, which can't appear in a subject.
GIT_LOG_FORMAT = "--format=%h%x1f%s%x1f%ct"
GIT_DIRTY_ARG = "--untracked-files=no"
MAX_SUBJECT_CHARS = 80
# CANDYGOOGY_COMMIT is shown as long as git's usual short hash.
SHORT_COMMIT_CHARS = 7


@dataclass(frozen=True)
class Version:
    commit: str  # short hash
    subject: str
    committed: float  # commit time, epoch seconds
    dirty: bool  # tracked files differ from the commit


@dataclass(frozen=True)
class Build:
    commit: str  # CANDYGOOGY_COMMIT: a container image has no checkout to ask


# What `git` said when it couldn't tell us, for the /about message.
VersionResult = Version | Build | str


async def read_version() -> VersionResult:
    """The checkout's current commit; without a checkout, the commit the image
    was built from (CANDYGOOGY_COMMIT); otherwise a short reason it couldn't be read."""
    if BUILD_COMMIT is not None and not (REPO_DIR / ".git").exists():
        return Build(BUILD_COMMIT)
    head = await system.run("git", "-C", str(REPO_DIR), "log", "-1", GIT_LOG_FORMAT, timeout=5)
    if not head.ok:
        reason = head.err.strip().splitlines()[0] if head.err.strip() else f"exit {head.code}"
        return f"git: {reason}"
    parts = head.out.strip().split("\x1f")
    if len(parts) != 3 or not is_number(parts[2]):
        return "git: unexpected output"
    # --no-optional-locks: never try to write .git/index (the unit's home is read-only).
    status = await system.run(
        "git", "--no-optional-locks", "-C", str(REPO_DIR), "status", "--porcelain", GIT_DIRTY_ARG,
        timeout=5,
    )  # fmt: skip
    dirty = status.ok and bool(status.out.strip())
    return Version(parts[0], parts[1], float(parts[2]), dirty)


_startup_version: VersionResult = "not read yet"


async def record_startup_version() -> None:
    """Called once at startup: remember which commit this process loaded."""
    global _startup_version
    _startup_version = await read_version()


def stamp(when: float) -> str:
    """ "2 Oct 14:05" (strftime has no portable unpadded day)."""
    at = time.localtime(when)
    return f"{at.tm_mday} {time.strftime('%b %H:%M', at)}"


def version_line(version: VersionResult) -> str:
    """The release, then the commit it's running (or why that's unknown)."""
    release = f"candygoogy {__version__} · "
    if isinstance(version, str):
        return release + f"Commit unknown <i>({escape(version[:200])})</i>."
    if isinstance(version, Build):
        commit = escape(version.commit[:SHORT_COMMIT_CHARS])
        return release + (
            f"Running <code>{commit}</code> <i>(the commit the image was built from)</i>."
        )
    subject = version.subject
    if len(subject) > MAX_SUBJECT_CHARS:
        subject = subject[: MAX_SUBJECT_CHARS - 1] + "…"
    line = release + f"Running <code>{escape(version.commit)}</code> · {escape(subject)}"
    line += f" <i>(committed {stamp(version.committed)})</i>"
    if version.dirty:
        line += "\n⚠️ The checkout has uncommitted changes."
    return line


def drift_line(running: VersionResult, on_disk: VersionResult) -> str | None:
    """A warning if newer code was pulled but the bot wasn't restarted."""
    if (
        isinstance(running, Version)
        and isinstance(on_disk, Version)
        and on_disk.commit != running.commit
    ):
        return (
            f"⚠️ <code>{escape(on_disk.commit)}</code> is checked out but not running. "
            + "Restart the service to apply it."
        )
    return None


def timers_line() -> str:
    """Where timers and the last action are saved, and whether that survives a restart."""
    folder = STATE_DIR
    if not os.access(folder, os.W_OK):
        return (
            f"⚠️ Can't write to <code>{escape(str(folder))}</code>: timers and the last "
            + "power action won't survive a restart. Install the current unit file "
            + "(it adds StateDirectory=)."
        )
    if os.environ.get("STATE_DIRECTORY"):
        return (
            f"⏰ Timers and the last power action are saved in <code>{escape(str(folder))}</code>."
        )
    return (
        "⏰ Timers and the last power action are saved next to the code "
        + f"(<code>{escape(str(folder))}</code>)."
    )


def zone_name() -> str | None:
    """ "America/New_York", from TZ or the /etc/localtime symlink; None if unknown."""
    if tz := os.environ.get("TZ"):
        return tz.removeprefix(":")
    try:
        parts = Path("/etc/localtime").resolve(strict=True).parts
    except OSError:
        return None
    return "/".join(parts[parts.index("zoneinfo") + 1 :]) if "zoneinfo" in parts else None


def timezone_line(name: str | None, abbrev: str, offset: str) -> str:
    """Which clock "22:30" means. `offset` is strftime's %z, e.g. "+0200"."""
    label = abbrev
    if re.fullmatch(r"[+-][0-9]{4}", offset):
        label += f", UTC{offset[:3]}:{offset[3:]}"
    if name:
        label = f"{name} ({label})"
    line = f"🕒 Times are in {escape(label)}."
    if offset in ("+0000", "-0000") and not os.environ.get("TZ"):
        line += "\n<i>Not your zone? Set e.g. TZ=America/New_York in .env and restart.</i>"
    return line


async def about_message() -> str:
    on_disk = await read_version()
    lines = [version_line(_startup_version)]
    if drift := drift_line(_startup_version, on_disk):
        lines.append(drift)
    lines += [
        f"Up {duration(time.time() - STARTED_AT)}, since {stamp(STARTED_AT)}.",
        f"Python {sys.version.split()[0]} · python-telegram-bot {telegram.__version__}",
        timers_line(),
        timezone_line(zone_name(), time.strftime("%Z"), time.strftime("%z")),
    ]
    return card("ℹ️", "About candygoogy", *lines)


@authorized
async def about(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await typing(update)
    await reply(update, await about_message())
