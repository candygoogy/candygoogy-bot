"""Running local commands, and SSH on the machines."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from html import escape

from candygoogy.config import SSH_KNOWN_HOSTS
from candygoogy.messages import card, clip
from candygoogy.oscommands import Action, commands_for
from candygoogy.settings import Machine

log = logging.getLogger(__name__)

# --- Running commands ---------------------------------------------------------
# Invariant: nothing a chat sends ever reaches a command line. Every run() call
# passes only literals, module constants and validated config (settings.py:
# a configured Machine's fields), and never through a shell
# (create_subprocess_exec, not _shell). ssh() takes an action name, not a
# command: the command is that action's constant for the machine's OS
# (oscommands.py). tests/test_subprocess_invariant.py checks every call site.


@dataclass(frozen=True)
class RunResult:
    code: int
    err: str = ""
    timed_out: bool = False
    out: str = ""
    missing: bool = False  # the program isn't installed (as opposed to failing)

    @property
    def ok(self) -> bool:
        return self.code == 0

    def describe(self, action: str) -> str:
        """Failure message for `action`, e.g. "Shutdown"."""
        if self.timed_out:
            return card("⏱️", f"{action} timed out", escape(self.err))
        if detail := clip(self.err):
            return card("❌", f"{action} failed", f"<pre>{escape(detail)}</pre>")
        return card("❌", f"{action} failed", f"Exit code {self.code}.")


async def run(*args: str, timeout: float) -> RunResult:
    """Run a command without blocking the event loop. Never raises for process errors.

    The child gets this process's environment as is (e.g. TZ, LANG)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        # Expected on a homelab without e.g. apt; callers say "not installed".
        log.info("%s isn't installed", args[0])
        return RunResult(-1, str(e), missing=True)
    except (OSError, ValueError) as e:
        log.error("Could not start %s: %s", args[0], e)
        return RunResult(-1, str(e))
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):  # it may have just exited
            proc.kill()
        _ = await proc.wait()
        log.warning("%s timed out after %ss", args[0], timeout)
        return RunResult(-1, f"No response within {timeout:g}s.", timed_out=True)
    return RunResult(
        await proc.wait(),
        stderr.decode(errors="replace"),
        out=stdout.decode(errors="replace"),
    )


SSH_OPTIONS = (
    "-o", "BatchMode=yes",  # never hang on a password/passphrase prompt
    "-o", "StrictHostKeyChecking=accept-new",  # trust on first use, reject changed keys
    "-o", "ConnectTimeout=5",
    "-o", "IdentitiesOnly=yes",  # offer only the machine's key
    "-o", "LogLevel=ERROR",  # no "Permanently added …" in failure messages
    # A session frozen by the machine going to sleep ends after about 10s.
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
)  # fmt: skip
# SSH_KNOWN_HOSTS: a writable known_hosts file, e.g. on a volume in a container.
KNOWN_HOSTS_OPTIONS = (
    ("-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}") if SSH_KNOWN_HOSTS is not None else ()
)


async def ssh(machine: Machine, action: Action, timeout: float = 15) -> RunResult:
    """Run `action` (e.g. "shutdown") on `machine`: its OS's constant command
    for it, in the machine's default SSH shell. Host keys are stored under the
    machine's name (HostKeyAlias), so machines that swap IPs don't clash; "--"
    ends ssh's options, so the destination can never be read as one."""
    command = commands_for(machine.os).command(action)
    return await run(
        "ssh",
        "-i", machine.ssh_key_path,
        "-p", str(machine.ssh_port),
        *SSH_OPTIONS,
        *KNOWN_HOSTS_OPTIONS,
        "-o", f"HostKeyAlias=candygoogy-{machine.name}",
        "--", f"{machine.ssh_user}@{machine.ip}",
        command,
        timeout=timeout,
    )  # fmt: skip
