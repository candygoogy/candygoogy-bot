# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""run() and the audit log."""

import asyncio
import logging
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from candygoogy import (
    config,
    handlers,
    system,
)
from tests.helpers import (
    FakeRun,
    call,
    confirm_data,
    fake_process,
    make_button_press,
    make_update,
)

# --- run() -----------------------------------------------------------------


def test_run_returns_exit_code_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_process(3, b"boom", b"out"))
    )
    assert asyncio.run(system.run("x", timeout=1)) == system.RunResult(3, "boom", out="out")


def test_run_reports_missing_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("no x"))
    )
    result = asyncio.run(system.run("x", timeout=1))
    assert not result.ok and "no x" in result.err and not result.timed_out


def test_run_kills_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = fake_process(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = asyncio.run(system.run("x", timeout=0.01))
    assert result.timed_out and not result.ok
    proc.kill.assert_called_once()


def test_run_survives_process_exiting_during_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = fake_process(hang=True)
    proc.kill.side_effect = ProcessLookupError
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    assert asyncio.run(system.run("x", timeout=0.01)).timed_out


def test_empty_stderr_falls_back_to_exit_code() -> None:
    assert "Exit code 7" in system.RunResult(7).describe("X")


# --- audit log -------------------------------------------------------------


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "candygoogy.audit"]


@pytest.mark.usefixtures("workstation_on")
def test_audit_trail_for_confirmed_shutdown(
    caplog: pytest.LogCaptureFixture, awake_sequence: Callable[..., None]
) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    awake_sequence(True, False)
    call(handlers.on_button, make_button_press(confirm_data("shutdown")))
    lines = audit_lines(caplog)
    assert lines[:2] == [
        "audit action=shutdown event=confirmed",
        "audit action=shutdown event=sent",
    ]
    assert lines[2].startswith("audit action=shutdown event=down after=")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("cancel", "audit action=unknown event=cancelled"),
        (confirm_data("reboot", age_seconds=999), "audit action=reboot event=expired"),
    ],
)
@pytest.mark.usefixtures("workstation_on")
def test_audit_cancel_and_expiry(
    caplog: pytest.LogCaptureFixture, data: str, expected: str
) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    call(handlers.on_button, make_button_press(data))
    assert audit_lines(caplog) == [expected]


def test_audit_wake_and_never_logs_secrets(
    fake_run: FakeRun, caplog: pytest.LogCaptureFixture, awake_sequence: Callable[..., None]
) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    awake_sequence(False, True)
    call(handlers.wake, make_update())
    lines = audit_lines(caplog)
    assert lines[0] == "audit action=wake event=packet_sent"
    assert lines[1].startswith("audit action=wake event=up after=")
    joined = "\n".join(lines)
    for secret in (
        config.FIRST_MACHINE.mac,
        config.FIRST_MACHINE.ip,
        config.FIRST_MACHINE.ssh_user,
    ):
        assert secret not in joined
    assert "wol" in fake_run.programs()


def test_audit_failed_action(workstation_on: FakeRun, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    workstation_on.results["ssh"] = system.RunResult(1, "denied")
    call(handlers.on_button, make_button_press(confirm_data("sleep")))
    assert audit_lines(caplog) == [
        "audit action=sleep event=confirmed",
        "audit action=sleep event=failed",
    ]


# --- audit line format ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12s", "12s"),
        (True, "True"),
        (3, "3"),
        ("tomorrow 06:50", '"tomorrow 06:50"'),
        ("Mon 5 Oct 06:50", '"Mon 5 Oct 06:50"'),
        ('a"b', '"a\\"b"'),
        ("x=y", '"x=y"'),
        ("", '""'),
    ],
)
def test_audit_values_stay_one_token(value: object, expected: str) -> None:
    assert config.audit_value(value) == expected


def test_audit_line_with_spaced_value(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    config.audit("wake", "timer_set", due="tomorrow 06:50", saved=True)
    assert (
        caplog.messages[-1] == 'audit action=wake event=timer_set due="tomorrow 06:50" saved=True'
    )
