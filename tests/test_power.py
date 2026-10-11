# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/wake, /shutdown, /reboot, /sleep: prompts, buttons, follow-ups."""

import asyncio
import logging
import time
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from telegram.error import TelegramError

from candygoogy import (
    config,
    handlers,
    oscommands,
    power,
    system,
    wol,
)
from candygoogy.settings import Machine
from tests.helpers import (
    POWER_COMMANDS,
    FakeRun,
    busy,
    button_data,
    call,
    capturing_context,
    confirm_data,
    edits,
    fake_process,
    make_button_press,
    make_update,
    replies,
    sent_edits,
)

# --- /wake -----------------------------------------------------------------


def test_wake_sends_magic_packet(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.wake, update)
    assert ("wol", config.FIRST_MACHINE.mac, "255.255.255.255") in fake_run.calls
    assert "Sending 3 magic packets" in replies(update)[0]


@pytest.mark.parametrize(("ping_code", "port22"), [(0, False), (1, True), (0, True)])
def test_wake_skips_packet_when_already_on(fake_run: FakeRun, ping_code: int, port22: bool) -> None:
    fake_run.results["ping"] = system.RunResult(ping_code)
    fake_run.port22_open = port22
    update = make_update()
    call(handlers.wake, update)
    assert "wol" not in fake_run.programs()
    assert "Already on" in replies(update)[0]


def test_wake_reports_failure(fake_run: FakeRun) -> None:
    fake_run.results["wol"] = system.RunResult(1, "Network is unreachable")
    update = make_update()
    call(handlers.wake, update)
    assert "Network is unreachable" in replies(update)[0]


# --- /shutdown, /reboot, /sleep: confirmation prompt ------------------------


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_power_command_only_asks_for_confirmation(workstation_on: FakeRun, name: str) -> None:
    update = make_update()
    call(handlers.COMMANDS[name], update)
    assert set(workstation_on.ssh_commands()) <= {"quser"}  # read-only session check only
    assert replies(update) == [power.POWER_ACTIONS[name].question]
    confirm, cancel = button_data(update)
    assert confirm.startswith(f"confirm:{name}:") and cancel == "cancel"
    assert all(len(d.encode()) <= 64 for d in (confirm, cancel))  # Telegram's limit


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_power_command_unreachable(fake_run: FakeRun, name: str) -> None:
    update = make_update()
    call(handlers.COMMANDS[name], update)
    assert "ssh" not in fake_run.programs()
    assert "Unreachable" in replies(update)[0]


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_power_command_ssh_port_closed(fake_run: FakeRun, name: str) -> None:
    fake_run.results["ping"] = system.RunResult(0)
    update = make_update()
    call(handlers.COMMANDS[name], update)
    assert "ssh" not in fake_run.programs()
    assert "port 22" in replies(update)[0]


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_power_command_prompts_when_only_ssh_answers(fake_run: FakeRun, name: str) -> None:
    fake_run.port22_open = True  # ping dropped by the firewall, sshd up
    update = make_update()
    call(handlers.COMMANDS[name], update)
    assert len(button_data(update)) == 2


# --- buttons ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("shutdown", "shutdown /s /t 0"),
        ("reboot", "shutdown /r /t 0"),
        ("sleep", oscommands.WINDOWS_SLEEP),
    ],
)
def test_confirm_runs_the_action(workstation_on: FakeRun, name: str, command: str) -> None:
    update = make_button_press(confirm_data(name))
    call(handlers.on_button, update)
    assert workstation_on.ssh_commands() == [command]
    progress, result, *_follow_up = edits(update)
    assert progress == power.POWER_ACTIONS[name].progress
    assert "failed" not in result.lower()
    update.callback_query.answer.assert_awaited_once()


def test_cancel_sends_nothing(workstation_on: FakeRun) -> None:
    update = make_button_press("cancel")
    call(handlers.on_button, update)
    assert workstation_on.calls == []
    assert "Cancelled" in edits(update)[0]


@pytest.mark.parametrize("data", ["", "bogus", "confirm:format-c:123", "confirm:shutdown:soon"])
def test_malformed_button_data_sends_nothing(workstation_on: FakeRun, data: str) -> None:
    update = make_button_press(data)
    call(handlers.on_button, update)
    assert workstation_on.ssh_commands() == []


def test_expired_confirmation_sends_nothing(workstation_on: FakeRun) -> None:
    update = make_button_press(confirm_data("shutdown", age_seconds=config.CONFIRM_TTL_SECONDS + 5))
    call(handlers.on_button, update)
    assert workstation_on.calls == []
    assert "Expired" in edits(update)[0]


def test_double_tap_runs_once(workstation_on: FakeRun) -> None:
    data = confirm_data("shutdown")
    call(handlers.on_button, make_button_press(data, message_id=42))
    second = make_button_press(data, message_id=42)
    call(handlers.on_button, second)
    assert workstation_on.ssh_commands() == ["shutdown /s /t 0"]
    assert edits(second) == []


def test_confirm_rechecks_reachability(fake_run: FakeRun) -> None:
    # Prompt was shown while it was on; it went to sleep before the tap.
    update = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, update)
    assert fake_run.ssh_commands() == []
    assert "Unreachable" in edits(update)[-1]


# --- perform_* results -----------------------------------------------------


def perform(name: power.PowerName) -> tuple[str, bool]:
    return asyncio.run(power.perform(config.FIRST_MACHINE, name))


def test_shutdown_failure(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(255, "Connection refused")
    assert "Connection refused" in perform("shutdown")[0]


def test_shutdown_timeout_checks_with_the_follow_up(fake_run: FakeRun) -> None:
    """No answer isn't a failure (the machine may drop the connection as it goes
    down); the
    follow-up checks whether it went down."""
    fake_run.results["ssh"] = system.RunResult(-1, "no response within 15s", timed_out=True)
    text, follow = perform("shutdown")
    assert "No answer over SSH; checking" in text and follow


def test_reboot_failure(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(1, "Access is denied.")
    assert "Access is denied." in perform("reboot")[0]


def test_sleep_command_suspends_and_never_hibernates() -> None:
    cmd = oscommands.WINDOWS_SLEEP
    assert "SetSuspendState('Suspend', $false, $false)" in cmd
    assert "Hibernate" not in cmd and "rundll32" not in cmd
    assert cmd.count('"') == 2  # one quoted script for cmd.exe to hand to PowerShell


def test_sleep_timeout_means_it_went_to_sleep(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(-1, "no response", timed_out=True)
    text, follow = perform("sleep")
    assert "SSH session froze" in text and follow


@pytest.mark.usefixtures("fake_run")
def test_sleep_early_return() -> None:
    text, follow = perform("sleep")
    assert "may not have slept" in text and not follow


def test_sleep_failure(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(1, "Access is denied.")
    text, follow = perform("sleep")
    assert "Access is denied." in text and not follow


# --- follow-ups ------------------------------------------------------------


@pytest.mark.parametrize(
    ("details", "says"),
    [
        (system.RunResult(0, out='{"boot": 20}'), "Booted in"),  # booted just now
        (system.RunResult(0, out='{"boot": 86400}'), "Woke from sleep in"),  # boot predates /wake
        (system.RunResult(255, "Connection refused"), "Up in"),  # sshd not up yet
    ],
)
def test_wake_follow_up_says_booted_or_resumed(
    fake_run: FakeRun, awake_sequence: Callable[..., None], details: system.RunResult, says: str
) -> None:
    fake_run.by_last_arg[oscommands.WINDOWS_DETAILS] = details
    awake_sequence(False, False, True)  # off at /wake, still off, then up
    update = make_update()
    call(handlers.wake, update)
    assert "wol" in fake_run.programs()
    *_, final = sent_edits(update)
    assert final.startswith("🟢 <b>Awake</b>") and says in final
    tries = 1 if details.ok else power.WAKE_DETAILS_TRIES
    assert fake_run.ssh_commands() == [oscommands.WINDOWS_DETAILS] * tries


@pytest.mark.usefixtures("fake_run")
def test_wake_follow_up_gives_up(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(False)
    update = make_update()
    call(handlers.wake, update)
    assert "Still not up" in sent_edits(update)[-1]


def test_failed_wake_is_not_followed(fake_run: FakeRun) -> None:
    fake_run.results["wol"] = system.RunResult(1, "boom")
    update = make_update()
    call(handlers.wake, update)
    assert sent_edits(update) == []


@pytest.mark.parametrize(
    ("name", "done"),
    [("shutdown", "⚫ <b>Off</b>\nShut down in"), ("sleep", "🌙 <b>Asleep</b>\nFell asleep in")],
)
def test_down_follow_up(
    workstation_on: FakeRun, awake_sequence: Callable[..., None], name: str, done: str
) -> None:
    if name == "sleep":
        workstation_on.results["ssh"] = system.RunResult(-1, "", timed_out=True)
    awake_sequence(True, False)
    update = make_button_press(confirm_data(name))
    call(handlers.on_button, update)
    assert done in edits(update)[-1]


@pytest.mark.usefixtures("workstation_on")
def test_down_follow_up_gives_up(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(True)
    update = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, update)
    assert "Still responding" in edits(update)[-1]


@pytest.mark.usefixtures("workstation_on")
def test_reboot_follow_up_down_then_up(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(True, False, False, True)
    update = make_button_press(confirm_data("reboot"))
    call(handlers.on_button, update)
    final = edits(update)[-1]
    assert final.startswith("🟢 <b>Back up</b>") and "went down after" in final


@pytest.mark.usefixtures("workstation_on")
def test_reboot_follow_up_never_went_down(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(True)
    update = make_button_press(confirm_data("reboot"))
    call(handlers.on_button, update)
    assert "may not be rebooting" in edits(update)[-1]


@pytest.mark.usefixtures("workstation_on")
def test_reboot_follow_up_never_came_back(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(False)
    update = make_button_press(confirm_data("reboot"))
    call(handlers.on_button, update)
    final = edits(update)[-1]
    assert "Went down after" in final and "Not back" in final


def test_failed_action_is_not_followed(workstation_on: FakeRun) -> None:
    workstation_on.results["ssh"] = system.RunResult(1, "Access is denied.")
    update = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, update)
    assert len(edits(update)) == 2  # progress, then the failure; no follow-up


@pytest.mark.usefixtures("workstation_on")
def test_follow_up_survives_deleted_message(awake_sequence: Callable[..., None]) -> None:
    awake_sequence(True, False)
    update = make_button_press(confirm_data("shutdown"))
    edit_mock = update.callback_query.edit_message_text
    # progress + result succeed, then the message is gone
    edit_mock.side_effect = [None, None, TelegramError("message to edit not found")] * 3
    call(handlers.on_button, update)  # must not raise


# --- one power operation at a time -----------------------------------------


@pytest.mark.usefixtures("workstation_on")
def test_wake_refused_while_busy(fake_run: FakeRun) -> None:
    power.claim(config.FIRST_MACHINE, "sleep")
    update = make_update()
    call(handlers.wake, update)
    [text] = replies(update)
    assert "Busy" in text and "putting it to sleep" in text
    assert "wol" not in fake_run.programs()


@pytest.mark.parametrize("name", POWER_COMMANDS)
def test_power_prompt_refused_while_busy(workstation_on: FakeRun, name: str) -> None:
    power.claim(config.FIRST_MACHINE, "wake")
    update = make_update()
    call(handlers.COMMANDS[name], update)
    assert "Already waking it" in replies(update)[0]
    assert workstation_on.ssh_commands() == []


def test_confirm_refused_if_busy_since_prompt(workstation_on: FakeRun) -> None:
    power.claim(config.FIRST_MACHINE, "reboot")
    update = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, update)
    assert "Already rebooting it" in edits(update)[-1]
    assert workstation_on.ssh_commands() == []


def test_lane_released_after_follow_up(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False, True)
    call(handlers.wake, make_update())  # call() also runs the follow-up to completion
    assert not busy()
    assert "wol" in fake_run.programs()


def test_lane_released_when_follow_up_times_out(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False)
    call(handlers.wake, make_update())
    assert not busy()
    assert fake_run.calls


def test_lane_released_after_failed_wake(fake_run: FakeRun) -> None:
    fake_run.results["wol"] = system.RunResult(1, "boom")
    call(handlers.wake, make_update())
    assert not busy()


@pytest.mark.parametrize("scenario", ["unreachable", "ssh_fails"])
def test_lane_released_when_action_does_not_start(fake_run: FakeRun, scenario: str) -> None:
    if scenario == "ssh_fails":
        fake_run.results["ping"] = system.RunResult(0)
        fake_run.port22_open = True
        fake_run.results["ssh"] = system.RunResult(1, "denied")
    call(handlers.on_button, make_button_press(confirm_data("shutdown")))
    assert not busy()


@pytest.mark.usefixtures("workstation_on")
def test_lane_released_when_action_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(_machine: object, _name: object) -> power.Result:
        raise RuntimeError("boom")

    monkeypatch.setattr(power, "perform", explode)
    with pytest.raises(RuntimeError):
        call(handlers.on_button, make_button_press(confirm_data("reboot")))
    assert not busy()


def test_busy_does_not_block_status(fake_run: FakeRun) -> None:
    power.claim(config.FIRST_MACHINE, "shutdown")
    update = make_update()
    call(handlers.status, update)
    assert "Busy" not in replies(update)[0]
    assert fake_run.calls


# --- /wake sends 3 magic packets (processes and the UDP socket faked) ---------


@pytest.fixture
def subprocess_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Mock the real subprocess layer: every command 'succeeds' and is recorded."""
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **_kwargs: object) -> MagicMock:
        calls.append(args)
        return fake_process(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


Packets = list[tuple[bytes, tuple[str, int]]]


@pytest.mark.usefixtures("subprocess_calls")
def test_wake_sends_exactly_three_packets(
    sent_packets: Packets, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False, True)  # off at /wake, then up
    call(handlers.wake, make_update())
    packet = wol.magic_packet(config.FIRST_MACHINE.mac)
    assert sent_packets == [(packet, ("255.255.255.255", 9))] * 3


@pytest.mark.usefixtures("subprocess_calls")
def test_wake_sends_no_packets_when_already_on(
    sent_packets: Packets, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(True)
    update = make_update()
    call(handlers.wake, update)
    assert sent_packets == []
    assert "Already on" in replies(update)[0]


@pytest.mark.usefixtures("subprocess_calls")
def test_extra_packets_are_sent_in_the_background(
    sent_packets: Packets, awake_sequence: Callable[..., None]
) -> None:
    """The handler returns after packet 1; packets 2-3 come from the background task."""
    awake_sequence(False, True)
    context, tasks = capturing_context()

    async def scenario() -> None:
        await handlers.wake(make_update(), context)
        assert len(sent_packets) == 1  # handler didn't wait
        for task in tasks:
            await task

    asyncio.run(scenario())
    assert len(sent_packets) == 3


@pytest.mark.usefixtures("subprocess_calls")
def test_second_wake_during_packets_sends_nothing(
    sent_packets: Packets, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False, False, True)
    context, tasks = capturing_context()

    second = make_update()

    async def scenario() -> None:
        await handlers.wake(make_update(), context)  # packet 1, rest pending
        await handlers.wake(second, context)  # arrives while packets 2-3 pending
        for task in tasks:
            await task

    asyncio.run(scenario())
    assert len(sent_packets) == 3  # not 6
    assert "Already waking it" in replies(second)[0]


@pytest.mark.usefixtures("subprocess_calls")
def test_packets_are_spaced_with_async_sleep(
    sent_packets: Packets,
    awake_sequence: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(power, "WOL_INTERVAL_SECONDS", 1.0)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def blocking_sleep(_seconds: float) -> None:
        raise AssertionError("time.sleep would block the whole bot")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(time, "sleep", blocking_sleep)
    awake_sequence(False, True)
    call(handlers.wake, make_update())
    assert sleeps[:2] == [1.0, 1.0]  # before packets 2 and 3
    assert len(sent_packets) == 3


def test_failed_extra_packet_does_not_stop_the_rest(
    fake_run: FakeRun,
    awake_sequence: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="candygoogy.audit")
    results = iter([system.RunResult(0), system.RunResult(1, "net down"), system.RunResult(0)])

    async def flaky(machine: Machine) -> system.RunResult:
        fake_run.calls.append(("wol", machine.mac))
        return next(results)

    monkeypatch.setattr(wol, "send_magic_packet", flaky)
    awake_sequence(False, True)
    call(handlers.wake, make_update())
    assert fake_run.programs().count("wol") == 3
    assert "audit action=wake event=extra_packet_failed packet=2" in caplog.text


# --- _used_prompts is pruned -------------------------------------------------


@pytest.mark.usefixtures("workstation_on")
def test_used_prompts_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    call(handlers.on_button, make_button_press("cancel", message_id=1))
    clock[0] += config.CONFIRM_TTL_SECONDS + 1
    call(handlers.on_button, make_button_press("cancel", message_id=2))
    assert list(handlers._used_prompts) == [2]  # pyright: ignore[reportPrivateUsage]
