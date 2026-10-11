# pyright: reportAny=false
# MagicMock attributes are Any by design.
import asyncio
import json
import os
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram.constants import ChatType

from candygoogy import (
    config,
    dockerapi,
    homelab,
    internet,
    power,
    system,
    timers,
)
from candygoogy.settings import Machine

AUTHORIZED_ID = 1111
STRANGER_ID = 2222
# Allowed too where a test turns on two_allowed_users (see conftest.py).
SECOND_ID = 3333


def make_update(
    user_id: int | None = AUTHORIZED_ID, chat_type: str | None = ChatType.PRIVATE
) -> MagicMock:
    """A message from `user_id` (None: no user) in a chat of `chat_type`
    (None: no chat). A private chat's ID is its user's."""
    update = MagicMock()
    if user_id is None:
        update.effective_user = None
    else:
        update.effective_user.id = user_id
        update.effective_chat.id = user_id
    if chat_type is None:
        update.effective_chat = None
    else:
        update.effective_chat.type = chat_type
        update.effective_chat.send_action = AsyncMock()
    sent = MagicMock()
    sent.edit_text = AsyncMock()
    update.effective_message.reply_text = AsyncMock(return_value=sent)
    return update


def replies(update: MagicMock) -> list[str]:
    mock: AsyncMock = update.effective_message.reply_text
    return [c.args[0] for c in mock.call_args_list]


def capturing_context() -> tuple[MagicMock, list[Coroutine[Any, Any, None]]]:
    """A handler context whose background tasks are collected instead of scheduled."""
    tasks: list[Coroutine[Any, Any, None]] = []
    context = MagicMock()
    context.args = None  # as PTB sets it for anything but a command

    def create_task(coro: Coroutine[Any, Any, None], **_kwargs: object) -> None:
        tasks.append(coro)

    context.application.create_task = create_task
    return context, tasks


def call(handler: Callable[[Any, Any], Coroutine[Any, Any, None]], update: MagicMock) -> None:
    """Run a handler, then any background tasks it started (follow-ups)."""

    async def main() -> None:
        context, tasks = capturing_context()
        await handler(update, context)
        for task in tasks:
            await task

    asyncio.run(main())


def sent_edits(update: MagicMock) -> list[str]:
    """Texts a follow-up wrote into the bot's own reply message."""
    mock: AsyncMock = update.effective_message.reply_text.return_value.edit_text
    return [c.args[0] for c in mock.call_args_list]


def make_button_press(
    data: str,
    message_id: int = 1,
    user_id: int | None = AUTHORIZED_ID,
    chat_type: str | None = ChatType.PRIVATE,
) -> MagicMock:
    update = make_update(user_id, chat_type)
    query = update.callback_query
    query.data = data
    if user_id is not None:
        query.from_user.id = user_id
        query.message.chat.id = user_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.message_id = message_id
    return update


def edits(update: MagicMock) -> list[str]:
    mock: AsyncMock = update.callback_query.edit_message_text
    return [c.args[0] for c in mock.call_args_list]


def button_data(update: MagicMock) -> list[str]:
    """callback_data of every inline button in the last reply."""
    mock: AsyncMock = update.effective_message.reply_text
    markup = mock.call_args.kwargs["reply_markup"]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


POWER_COMMANDS = ["shutdown", "reboot", "sleep"]


class FakeRun:
    """Stand-in for system.run: returns a scripted RunResult per program name.

    Defaults model a workstation that is off: ping fails, port 22 is closed,
    and every other command succeeds.
    """

    def __init__(self) -> None:
        self.results: dict[str, system.RunResult] = {"ping": system.RunResult(1)}
        # Checked first: a result for one exact command, keyed by its last argument
        # (e.g. the SSH command string), for programs called several ways.
        self.by_last_arg: dict[str, system.RunResult] = {}
        self.calls: list[tuple[str, ...]] = []
        self.port22_open: bool = False

    async def __call__(self, *args: str, timeout: float) -> system.RunResult:
        assert timeout > 0
        self.calls.append(args)
        if args[-1] in self.by_last_arg:
            return self.by_last_arg[args[-1]]
        return self.results.get(args[0], system.RunResult(0))

    async def send_magic_packet(self, machine: Machine) -> system.RunResult:
        """Stands in for wol.send_magic_packet: recorded like a command."""
        self.calls.append(("wol", machine.mac, machine.wol_broadcast or "255.255.255.255"))
        return self.results.get("wol", system.RunResult(0))

    def programs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def ssh_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if c[0] == "ssh"]


class FakeSocket:
    """Stands in for the UDP socket wol.py opens: records each packet sent, or
    raises `error` from sendto."""

    def __init__(self, sent: list[tuple[bytes, tuple[str, int]]], error: OSError | None = None):
        self.sent: list[tuple[bytes, tuple[str, int]]] = sent
        self.error: OSError | None = error
        self.options: list[tuple[int, int, int]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def sendto(self, data: bytes, address: tuple[str, int]) -> int:
        if self.error is not None:
            raise self.error
        self.sent.append((data, address))
        return len(data)


def confirm_data(name: str, age_seconds: float = 0) -> str:
    return f"confirm:{name}:{int(time.time() - age_seconds)}"


# --- run() -----------------------------------------------------------------


def fake_process(
    returncode: int = 0, stderr: bytes = b"", stdout: bytes = b"", hang: bool = False
) -> MagicMock:
    proc = MagicMock()

    async def communicate() -> tuple[bytes, bytes]:
        if hang:
            await asyncio.sleep(10)
        return stdout, stderr

    proc.communicate = communicate
    proc.wait = AsyncMock(return_value=returncode)
    return proc


# --- one power operation at a time -----------------------------------------


def busy() -> bool:
    return power.busy_message(config.FIRST_MACHINE) is not None


# --- timers ------------------------------------------------------------------


def local(hour: int, minute: int = 0, day: int = 15) -> float:
    """A wall-clock timestamp in the local time zone, mid-month."""
    return time.mktime((2026, 9, day, hour, minute, 0, 0, 0, -1))


def call_with_args(handler: Callable[..., Any], update: MagicMock, *args: str) -> None:
    context, _ = capturing_context()
    context.args = list(args)
    asyncio.run(handler(update, context))


def fake_app() -> tuple[MagicMock, list[Coroutine[Any, Any, None]]]:
    """An Application whose bot records sent messages and whose tasks are collected."""
    app = MagicMock()
    sent = MagicMock()
    sent.edit_text = AsyncMock()
    app.bot.send_message = AsyncMock(return_value=sent)
    tasks: list[Coroutine[Any, Any, None]] = []
    app.create_task = tasks.append
    return app, tasks


def sent_texts(app: MagicMock) -> list[str]:
    return [c.args[1] for c in app.bot.send_message.call_args_list]


def set_timer(action: str, due: float, chat_id: int = AUTHORIZED_ID) -> timers.Timer:
    """Register a timer without starting its task (as if it were waiting)."""
    timer = timers.Timer(action, due, chat_id)
    timers._timers[timer.key] = timer  # pyright: ignore[reportPrivateUsage]
    return timer


# --- richer awake /status ------------------------------------------------------


GIB = 1024**3


# --- /homelab ------------------------------------------------------------------


MEMINFO = "MemTotal:       33554432 kB\nMemFree:         2097152 kB\nMemAvailable:   25165824 kB\n"


def container(
    name: str, state: str = "running", health: str = "none", status: str = "Up 3 days"
) -> dict[str, object]:
    """One entry of Docker's GET /containers/json; Health only if it has a healthcheck."""
    entry: dict[str, object] = {
        "Id": "0" * 64,
        "Names": [f"/{name}"],
        "Image": "x",
        "State": state,
        "Status": status,
    }
    if health != "none":
        entry["Health"] = {"Status": health, "FailingStreak": 0}
    return entry


HEALTHY_CONTAINERS = (
    container("web"),
    container("app", health="healthy", status="Up 3 days (healthy)"),
    container("app-db"),
)


class FakeHomelab:
    """The homelab as /homelab sees it: /proc files, disks, systemctl and backups.

    Defaults model a healthy homelab with a backup that finished 7 hours ago.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: FakeRun) -> None:
        self.run: FakeRun = run
        self.proc: Path = tmp_path / "proc"
        self.proc.mkdir()
        _ = (self.proc / "uptime").write_text("533000.12 1000.00\n")
        _ = (self.proc / "loadavg").write_text("0.19 0.76 1.39 1/700 1234\n")
        _ = (self.proc / "meminfo").write_text(MEMINFO)
        self.disks: dict[str, tuple[float, float] | None] = {
            "/": (150 * GIB, 250 * GIB),
            "/srv/data": (300 * GIB, 500 * GIB),
        }
        self.unmounted: set[str] = set()
        self.backups: Path = tmp_path / "backups"
        self.backups.mkdir()
        monkeypatch.setattr(homelab, "PROC", self.proc)
        monkeypatch.setattr(homelab, "BACKUP_DIR", self.backups)
        monkeypatch.setattr(homelab, "disk_space", self.disks.get)
        monkeypatch.setattr(homelab, "is_mount", self.is_mount)
        self.run_dir: Path = tmp_path / "run"
        self.run_dir.mkdir()
        monkeypatch.setattr(homelab, "RUN", str(self.run_dir))
        # Docker's API: what it answers, and every request it got.
        self.docker_answer: httpx.Response | Exception = httpx.Response(200, json=[])
        self.docker_requests: list[httpx.Request] = []
        monkeypatch.setattr(dockerapi, "transport", lambda: httpx.MockTransport(self.docker_api))
        self.failed()
        self.containers(*HEALTHY_CONTAINERS)
        self.updates()
        _ = self.snapshot("20261005-021500", age_hours=7, size=10 * 1024**2)

    def is_mount(self, path: str) -> bool:
        return path not in self.unmounted

    def failed(self, *units: str) -> None:
        listed = [{"unit": u, "load": "loaded", "active": "failed", "sub": "failed"} for u in units]
        self.run.results["systemctl"] = system.RunResult(0, out=json.dumps(listed))

    def docker_api(self, request: httpx.Request) -> httpx.Response:
        self.docker_requests.append(request)
        if isinstance(self.docker_answer, Exception):
            raise self.docker_answer
        return self.docker_answer

    def containers(self, *containers: dict[str, object]) -> None:
        """Docker's API lists these containers."""
        self.docker_answer = httpx.Response(200, json=list(containers))

    def updates(self, *packages: str) -> None:
        lines = [f"{p}/stable 2.0 amd64 [upgradable from: 1.0]" for p in packages]
        self.run.results["apt"] = system.RunResult(0, out="\n".join(["Listing...", *lines]))

    def reboot_required(self, *packages: str, listed: bool = True) -> None:
        (self.run_dir / "reboot-required").touch()
        if listed:
            _ = (self.run_dir / "reboot-required.pkgs").write_text(
                "".join(f"{p}\n" for p in packages)
            )

    def snapshot(
        self, name: str, age_hours: float = 1, size: int = 1024, complete: bool = True
    ) -> Path:
        folder = self.backups / name
        folder.mkdir()
        _ = (folder / "dump.tar.gz").write_bytes(b"x" * size)
        if complete:
            marker = folder / homelab.COMPLETE_MARKER
            marker.touch()
            when = time.time() - age_hours * 3600
            os.utime(marker, (when, when))
        return folder


# --- Internet outages --------------------------------------------------------------


class FakeInternet:
    """Scripted reachable() answers and a controllable clock for internet.check()."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.online: bool = True
        self.now: float = time.mktime((2026, 10, 7, 14, 0, 0, 0, 0, -1))
        self.app: MagicMock = fake_app()[0]

        async def reachable() -> bool:
            return self.online

        monkeypatch.setattr(internet, "reachable", reachable)
        monkeypatch.setattr(time, "time", lambda: self.now)

    def check(self, online: bool, minutes_later: float = 1) -> None:
        self.online = online
        asyncio.run(internet.check(self.app))
        self.now += minutes_later * 60

    def sent(self) -> list[str]:
        return sent_texts(self.app)
