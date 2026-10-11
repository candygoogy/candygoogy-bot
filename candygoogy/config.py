"""Configuration from the environment, logging and the audit trail."""

import json
import logging
import os
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from telegram import (
    Update,
)
from telegram.ext import (
    Application,
    ContextTypes,
    ExtBot,
    JobQueue,
)

from candygoogy.settings import load_settings

log = logging.getLogger(__name__)

# Checked as a whole; bot.py has already loaded .env and reported any problem
# without a traceback, so a ConfigError here only happens when imported directly.
SETTINGS = load_settings(os.environ)

BOT_TOKEN = SETTINGS.bot_token
# Who may use the bot. Unsolicited messages (alerts, summaries) go to each of them.
AUTHORIZED_USER_IDS = SETTINGS.authorized_user_ids

# The machines the bot controls (MACHINES_FILE, or one from WORKSTATION_* env vars).
MACHINES = SETTINGS.machines
# The first configured machine, as loaded. The code reads machines.first() (which
# follows config.MACHINES at call time); this constant is for tests and tooling.
FIRST_MACHINE = MACHINES[0]
# ssh's UserKnownHostsFile, or None for ssh's default (~/.ssh/known_hosts).
SSH_KNOWN_HOSTS = SETTINGS.ssh_known_hosts

# /homelab: the disks to watch ("/" is the system disk; the others are mounts
# that must stay mounted).
HOMELAB_DISKS = SETTINGS.homelab_disks
# Where your backup job writes its snapshots (None: backup monitoring is off),
# and how old the newest complete one may get before the bot warns.
BACKUP_DIR = Path(SETTINGS.backup_dir) if SETTINGS.backup_dir is not None else None
BACKUP_MAX_AGE_HOURS = SETTINGS.backup_max_age_hours
# /net: (host, port) pairs the internet check connects to, in order.
INTERNET_CHECK_TARGETS = SETTINGS.internet_targets
# Running in a container: systemd units, apt and reboots belong to the host there.
IN_CONTAINER = SETTINGS.in_container
# The host's / as mounted in a container (e.g. /host), for disks and
# /run/reboot-required; None: this process's own /.
HOMELAB_HOST_ROOT = SETTINGS.homelab_host_root
# Docker Engine's API, which /homelab asks for the containers: "unix:///<path>"
# or "tcp://<host>:<port>".
DOCKER_HOST = SETTINGS.docker_host
# The commit a container image was built from (CANDYGOOGY_COMMIT), or None.
BUILD_COMMIT = SETTINGS.commit
# When the daily summary is sent: (hour, minute) in local time, or None for "off".
DIGEST_AT = SETTINGS.digest_at
# When the weekly report is sent: (weekday, hour, minute), Monday being 0, or None.
REPORT_AT = SETTINGS.weekly_report_at

REPO_DIR = Path(__file__).resolve().parent.parent
# Where files that must survive a restart go (timers, the last power action).
# systemd sets STATE_DIRECTORY from the unit's StateDirectory=. Without it
# (local development), they go in the repo root, next to bot.py.
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "").split(":")[0] or REPO_DIR)
# When this process started (wall clock); shown by /about and /logs.
STARTED_AT = time.time()

audit_log = logging.getLogger("candygoogy.audit")


def check_ssh_key() -> bool:
    """At startup: warn for each machine whose SSH key can't be read, since every
    command over SSH to it would then fail. The path isn't logged."""
    readable = True
    for machine in MACHINES:
        path = Path(machine.ssh_key_path)
        if not (path.is_file() and os.access(path, os.R_OK)):
            log.warning(
                "The SSH key for %s isn't a readable file; commands over SSH to it will fail",
                machine.name,
            )
            readable = False
    return readable


def audit_value(value: object) -> str:
    """key=value stays one token: values with spaces, quotes or = are JSON-quoted,
    e.g. due="tomorrow 06:45"."""
    text = str(value)
    if not text or any(c.isspace() or c in '"=' for c in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def audit(action: str, event: str, **details: object) -> None:
    """One greppable line per power event: `journalctl -u candygoogy-bot | grep audit`."""
    extra = "".join(f" {key}={audit_value(value)}" for key, value in details.items())
    audit_log.info("audit action=%s event=%s%s", action, event, extra)


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]
# The concrete type ApplicationBuilder().build() produces with PTB's defaults.
App = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    dict[Any, Any],
    dict[Any, Any],
    dict[Any, Any],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]


# An old "Confirm" button left in the chat must not fire days later.
CONFIRM_TTL_SECONDS = 60
