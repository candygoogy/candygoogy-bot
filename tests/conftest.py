# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Fixtures shared by the test files. The fake config is in __init__.py."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from candygoogy import app as application
from candygoogy import (
    auth,
    config,
    handlers,
    history,
    homelab,
    internet,
    keyboard,
    messages,
    pctime,
    power,
    probes,
    system,
    timers,
    wol,
)
from candygoogy.settings import Machine
from tests.helpers import (
    AUTHORIZED_ID,
    SECOND_ID,
    FakeHomelab,
    FakeInternet,
    FakeRun,
    FakeSocket,
)


@pytest.fixture(autouse=True)
def no_real_processes_or_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test would launch a real process or open a connection."""

    async def refuse(*args: object, **_kwargs: object) -> None:
        raise AssertionError(f"test tried to touch the real system: {args}")

    def refuse_socket() -> None:
        raise AssertionError("test tried to open a real UDP socket (Wake-on-LAN)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(asyncio, "open_connection", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    monkeypatch.setattr(wol, "open_socket", refuse_socket)


# Every module that reads the allowlist; test_allowlist.py checks none is missing.
ALLOWLIST_READERS = (config, auth, messages, application, timers)


@pytest.fixture
def two_allowed_users(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """AUTHORIZED_ID and SECOND_ID may both use the bot."""
    ids = (AUTHORIZED_ID, SECOND_ID)
    for module in ALLOWLIST_READERS:
        monkeypatch.setattr(module, "AUTHORIZED_USER_IDS", ids)
    return ids


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(system, "run", fake)

    async def port22(machine: Machine, timeout: float = 3) -> bool:
        assert timeout > 0 and machine in config.MACHINES
        return fake.port22_open

    monkeypatch.setattr(probes, "ssh_port_open", port22)
    # Magic packets are recorded as ("wol", mac, broadcast) calls; the sender
    # itself is tested with a fake socket in test_machines.py.
    monkeypatch.setattr(wol, "send_magic_packet", fake.send_magic_packet)
    return fake


@pytest.fixture
def sent_packets(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, tuple[str, int]]]:
    """The real Wake-on-LAN sender, on a fake socket: (packet, destination) per send."""
    sent: list[tuple[bytes, tuple[str, int]]] = []
    monkeypatch.setattr(wol, "open_socket", lambda: FakeSocket(sent))
    return sent


@pytest.fixture
def workstation_on(fake_run: FakeRun) -> FakeRun:
    fake_run.results["ping"] = system.RunResult(0)
    fake_run.port22_open = True
    return fake_run


@pytest.fixture(autouse=True)
def keyboard_already_shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """As on a running install: startup doesn't send the keyboard again."""
    path = tmp_path / "keyboard.json"
    _ = path.write_text(json.dumps(keyboard.LABELS))
    monkeypatch.setattr(application, "KEYBOARD_FILE", path)
    return path


@pytest.fixture(autouse=True)
def fresh_prompts() -> None:
    handlers._used_prompts.clear()  # pyright: ignore[reportPrivateUsage]
    auth._ignored.clear()  # pyright: ignore[reportPrivateUsage]
    timers._asking_when.clear()  # pyright: ignore[reportPrivateUsage]
    handlers._last_refresh.clear()  # pyright: ignore[reportPrivateUsage]
    history._last_actions.clear()  # pyright: ignore[reportPrivateUsage]
    power.release_all()


@pytest.fixture(autouse=True)
def isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "last_action.json")


@pytest.fixture(autouse=True)
def fast_follow_ups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll without delay and give up after milliseconds instead of minutes."""
    monkeypatch.setattr(power, "POLL_SECONDS", 0)
    monkeypatch.setattr(power, "WOL_INTERVAL_SECONDS", 0)
    for name in ["WAKE_BUDGET_SECONDS", "DOWN_BUDGET_SECONDS", "REBOOT_UP_BUDGET_SECONDS"]:
        monkeypatch.setattr(power, name, 0.02)


@pytest.fixture
def awake_sequence(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Script successive is_awake() answers; the last one repeats forever."""

    def script(*answers: bool) -> None:
        queue = list(answers)

        async def is_awake(machine: Machine) -> bool:
            assert machine in config.MACHINES
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(probes, "is_awake", is_awake)

    return script


# --- timers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_timers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timers, "TIMERS_FILE", tmp_path / "timers.json")
    timers._timers.clear()  # pyright: ignore[reportPrivateUsage]
    timers._timer_tasks.clear()  # pyright: ignore[reportPrivateUsage]


# --- /homelab ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_homelab_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(homelab, "ALERTS_FILE", tmp_path / "homelab_alerts.json")
    monkeypatch.setattr(homelab, "REBOOT_FILE", tmp_path / "homelab_reboot.json")
    monkeypatch.setattr(homelab, "INCIDENTS_FILE", tmp_path / "homelab_history.json")
    homelab._open_alerts.clear()  # pyright: ignore[reportPrivateUsage]
    homelab._incidents.clear()  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun) -> FakeHomelab:
    return FakeHomelab(tmp_path, monkeypatch, fake_run)


# --- Internet outages --------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_internet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh outage history, kept in tmp_path; never touches the real network."""
    monkeypatch.setattr(internet, "STATE_FILE", tmp_path / "internet.json")
    monkeypatch.setattr(internet, "_state", internet.State())
    monkeypatch.setattr(internet, "reachable", AsyncMock(return_value=True))


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> FakeInternet:
    return FakeInternet(monkeypatch)


# --- PC on-time -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_pc_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pctime, "STATE_FILE", tmp_path / "pc_time.json")
    monkeypatch.setattr(pctime, "_states", {})
