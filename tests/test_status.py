# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/status: probes, details, sessions and the last action."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from candygoogy import app as application
from candygoogy import (
    config,
    handlers,
    history,
    messages,
    oscommands,
    power,
    probes,
    system,
)
from tests.helpers import (
    GIB,
    FakeRun,
    button_data,
    call,
    confirm_data,
    edits,
    fake_app,
    make_button_press,
    make_update,
    replies,
)

# --- probes ----------------------------------------------------------------


def test_ssh_port_open_true(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(return_value=(MagicMock(), writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert asyncio.run(probes.ssh_port_open(config.FIRST_MACHINE))
    connect.assert_awaited_once_with(config.FIRST_MACHINE.ip, 22)
    writer.close.assert_called_once()


@pytest.mark.parametrize("error", [ConnectionRefusedError(), OSError("no route"), TimeoutError()])
def test_ssh_port_open_false(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=error))
    assert not asyncio.run(probes.ssh_port_open(config.FIRST_MACHINE))


def test_ssh_is_hardened(fake_run: FakeRun) -> None:
    machine = config.FIRST_MACHINE
    _ = asyncio.run(system.ssh(machine, "sessions"))
    [args] = fake_run.calls
    opts = " ".join(args)
    assert "StrictHostKeyChecking=accept-new" in opts
    assert "StrictHostKeyChecking=no" not in opts
    assert "BatchMode=yes" in opts
    assert args[1:5] == ("-i", machine.ssh_key_path, "-p", "22")
    # Host keys under the machine's name, quiet, keep-alives.
    for option in (
        "HostKeyAlias=candygoogy-workstation",
        "LogLevel=ERROR",
        "ServerAliveInterval=5",
        "ServerAliveCountMax=2",
    ):
        assert option in args
    assert "UserKnownHostsFile" not in opts  # only with SSH_KNOWN_HOSTS
    assert args[-3:] == ("--", f"{machine.ssh_user}@{machine.ip}", "quser")


# --- /status ---------------------------------------------------------------


MAC = "00:00:5e:00:53:01"  # matches WORKSTATION_MAC in conftest


def neigh(state: str, mac: str | None = MAC) -> system.RunResult:
    lladdr = f" lladdr {mac}" if mac else ""
    return system.RunResult(0, out=f"192.0.2.10 dev eth0{lladdr} {state}\n")


@pytest.mark.parametrize(
    ("ping_code", "port22", "expected"),
    [
        (0, True, "Ping ✅ · SSH ✅"),
        (1, True, "Ping ❌ · SSH ✅"),
        (0, False, "Ping ✅ · SSH ❌"),
    ],
)
def test_status_awake(fake_run: FakeRun, ping_code: int, port22: bool, expected: str) -> None:
    fake_run.results["ping"] = system.RunResult(ping_code)
    fake_run.port22_open = port22
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    assert "Awake" in text and expected in text
    assert "ip" not in fake_run.programs()  # no ARP lookup needed


def test_status_probably_asleep_when_only_arp_answers(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE")
    update = make_update()
    call(handlers.status, update)
    assert "Probably asleep" in replies(update)[0]


@pytest.mark.parametrize(
    "arp",
    [neigh("FAILED", None), neigh("INCOMPLETE", None), system.RunResult(0), system.RunResult(1)],
)
def test_status_off_or_asleep_when_nothing_answers(
    fake_run: FakeRun, arp: system.RunResult
) -> None:
    fake_run.results["ip"] = arp
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    assert "Off or asleep" in text and "can't reliably tell" in text


@pytest.mark.parametrize("state", ["STALE", "DELAY", "PROBE"])
def test_status_inconclusive_arp_state(fake_run: FakeRun, state: str) -> None:
    fake_run.results["ip"] = neigh(state)
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    assert state in text and "inconclusive" in text


def test_status_flags_ip_conflict(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE", "aa:bb:cc:dd:ee:ff")
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    assert "different MAC" in text
    assert "aa:bb" not in text and MAC not in text  # never echo MAC addresses


def test_status_has_refresh_button(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.status, update)
    assert button_data(update) == ["status"]
    assert "Checked" in replies(update)[0]
    assert fake_run.calls


def test_refresh_edits_status_and_can_repeat(
    workstation_on: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "REFRESH_COOLDOWN_SECONDS", 0)
    for _ in range(2):
        update = make_button_press("status", message_id=7)
        call(handlers.on_button, update)
        [text] = edits(update)
        assert "Awake" in text
        assert update.callback_query.edit_message_text.call_args.kwargs["reply_markup"] is (
            handlers.REFRESH_BUTTON
        )
    # Only the read-only uptime query, never a power command.
    assert set(workstation_on.ssh_commands()) == {oscommands.WINDOWS_DETAILS, "quser"}


def test_arp_entry_parses_ip_neigh(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE", "00-00-5E-00-53-01")
    state, mac = asyncio.run(probes.arp_entry(config.FIRST_MACHINE))
    assert state == "REACHABLE" and mac is not None and probes.same_mac(mac, MAC)
    assert ("ip", "neigh", "show", config.FIRST_MACHINE.ip) in fake_run.calls


# --- signed-in warning -----------------------------------------------------


QUSER_OUT = (
    " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
    ">alice                 console             1  Active      none   10/1/2026 9:00 AM\n"
    " bob                                       2  Disc          1:02  9/30/2026 8:45 PM\n"
)


def test_quser_parsing(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(0, out=QUSER_OUT)
    sessions = asyncio.run(probes.signed_in_sessions(config.FIRST_MACHINE))
    assert sessions == [
        oscommands.Session("alice", "active, in use now"),
        oscommands.Session("bob", "disconnected, idle 1:02"),
    ]
    assert fake_run.ssh_commands() == ["quser"]


def test_quser_nobody_signed_in(fake_run: FakeRun) -> None:
    fake_run.results["ssh"] = system.RunResult(1, "No User exists for *\r\n")
    assert asyncio.run(probes.signed_in_sessions(config.FIRST_MACHINE)) == []


@pytest.mark.parametrize(
    "result", [system.RunResult(1, "Access is denied."), system.RunResult(-1, "", timed_out=True)]
)
def test_quser_unknown(fake_run: FakeRun, result: system.RunResult) -> None:
    fake_run.results["ssh"] = result
    assert asyncio.run(probes.signed_in_sessions(config.FIRST_MACHINE)) is None


@pytest.mark.parametrize("name", ["shutdown", "reboot"])
def test_prompt_warns_about_signed_in_users(workstation_on: FakeRun, name: str) -> None:
    workstation_on.results["ssh"] = system.RunResult(0, out=QUSER_OUT)
    update = make_update()
    call(handlers.COMMANDS[name], update)
    [text] = replies(update)
    assert "<code>alice</code> (active, in use now)" in text
    assert "<code>bob</code> (disconnected, idle 1:02)" in text
    assert "Unsaved work will be lost" in text
    assert len(button_data(update)) == 2  # still offers Confirm/Cancel


def test_sleep_prompt_skips_session_check(workstation_on: FakeRun) -> None:
    workstation_on.results["ssh"] = system.RunResult(0, out=QUSER_OUT)
    update = make_update()
    call(handlers.sleep, update)
    assert workstation_on.ssh_commands() == []
    assert replies(update) == [power.POWER_ACTIONS["sleep"].question]


def test_prompt_says_when_sessions_are_unknown(workstation_on: FakeRun) -> None:
    workstation_on.results["ssh"] = system.RunResult(1, "Access is denied.")
    update = make_update()
    call(handlers.shutdown, update)
    assert "Couldn't check who's signed in" in replies(update)[0]


def test_session_names_are_escaped() -> None:
    warning = probes.sessions_warning([oscommands.Session("<b>x</b>", "active, in use now")])
    assert warning is not None and "&lt;b&gt;x&lt;/b&gt;" in warning


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (8.4, "8s"), (59.6, "1m"), (83, "1m 23s"), (180, "3m"), (-1, "0s")],
)
def test_duration_formatting(seconds: float, text: str) -> None:
    assert messages.duration(seconds) == text


# --- refresh spam (regression: BadRequest "Message is not modified") --------


def test_refresh_spam_is_throttled_not_an_error(workstation_on: FakeRun) -> None:
    first = make_button_press("status", message_id=9)
    call(handlers.on_button, first)
    probes_after_first = len(workstation_on.calls)
    spam = make_button_press("status", message_id=9)
    call(handlers.on_button, spam)
    assert edits(spam) == []  # no re-probe, no edit
    assert len(workstation_on.calls) == probes_after_first
    spam.callback_query.answer.assert_awaited_once_with("Just checked. Try again in a moment.")


@pytest.mark.usefixtures("workstation_on")
def test_refresh_cooldown_is_per_message() -> None:
    call(handlers.on_button, make_button_press("status", message_id=1))
    other = make_button_press("status", message_id=2)
    call(handlers.on_button, other)
    assert len(edits(other)) == 1


def test_unchanged_edit_is_not_an_error(workstation_on: FakeRun) -> None:
    update = make_button_press("status", message_id=3)
    update.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified: specified new message content and reply markup are "
        + "exactly the same as a current content and reply markup of the message"
    )
    call(handlers.on_button, update)  # must not raise, so on_error never fires
    assert workstation_on.calls


def test_other_bad_requests_still_raise(workstation_on: FakeRun) -> None:
    update = make_button_press("status", message_id=4)
    update.callback_query.edit_message_text.side_effect = BadRequest("Message to edit not found")
    with pytest.raises(BadRequest):
        call(handlers.on_button, update)
    assert workstation_on.calls


# --- last action in /status ------------------------------------------------


def test_status_says_when_no_action_is_known(fake_run: FakeRun) -> None:
    update = make_update()
    call(handlers.status, update)
    assert "No power action via the bot on record" in replies(update)[0]
    assert fake_run.calls


@pytest.mark.usefixtures("workstation_on")
def test_status_shows_confirmed_sleep(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    fake_run.results["ssh"] = system.RunResult(-1, "", timed_out=True)
    awake_sequence(True, False)
    call(handlers.on_button, make_button_press(confirm_data("sleep")))
    # Now it's off the network:
    fake_run.results["ping"] = system.RunResult(1)
    fake_run.port22_open = False
    update = make_update()
    call(handlers.status, update)
    text = replies(update)[0]
    assert "Last bot action: put it to sleep at" in text
    assert "(it went quiet)" in text


def test_unconfirmed_wake_is_labelled(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False)  # never comes up
    call(handlers.wake, make_update())
    assert "woke it at" in history.last_action_line(config.FIRST_MACHINE)
    assert "(not confirmed)" in history.last_action_line(config.FIRST_MACHINE)
    assert "wol" in fake_run.programs()


def test_awake_status_omits_last_action(workstation_on: FakeRun) -> None:
    history.remember(config.FIRST_MACHINE, "sleep")
    update = make_update()
    call(handlers.status, update)
    assert "Last bot action" not in replies(update)[0]
    assert workstation_on.calls


def test_settle_ignores_a_superseded_action() -> None:
    history.remember(config.FIRST_MACHINE, "sleep")
    history.remember(config.FIRST_MACHINE, "wake")
    history.settle(
        config.FIRST_MACHINE, "sleep", confirmed=True
    )  # late verdict for the older action
    assert "(still checking)" in history.last_action_line(config.FIRST_MACHINE)


def test_failed_action_is_not_remembered(workstation_on: FakeRun) -> None:
    workstation_on.results["ssh"] = system.RunResult(1, "denied")
    call(handlers.on_button, make_button_press(confirm_data("shutdown")))
    assert "No power action" in history.last_action_line(config.FIRST_MACHINE)
    assert not history.HISTORY_FILE.exists()


def restart_bot() -> None:
    """Forget everything in memory, then load from disk as at startup."""
    history._last_actions.clear()  # pyright: ignore[reportPrivateUsage]
    history.load_last_action()


def test_last_action_survives_a_restart() -> None:
    history.remember(config.FIRST_MACHINE, "sleep")
    history.settle(config.FIRST_MACHINE, "sleep", confirmed=True)
    line = history.last_action_line(config.FIRST_MACHINE)
    restart_bot()
    assert history.last_action_line(config.FIRST_MACHINE) == line
    assert "put it to sleep" in line and "(it went quiet)" in line


def test_last_action_cut_short_by_a_restart_is_not_confirmed() -> None:
    history.remember(config.FIRST_MACHINE, "shutdown")  # the follow-up was still watching
    restart_bot()
    assert "shut it down" in history.last_action_line(config.FIRST_MACHINE)
    assert "(not confirmed)" in history.last_action_line(config.FIRST_MACHINE)


def test_last_action_file_is_written_atomically() -> None:
    history.remember(config.FIRST_MACHINE, "wake")
    data = json.loads(history.HISTORY_FILE.read_text())
    assert data["workstation"]["name"] == "wake" and data["workstation"]["confirmed"] is None
    assert not history.HISTORY_FILE.with_suffix(".tmp").exists()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"name": "format_disk", "at": 1, "confirmed": true}',
        '{"name": "wake", "at": "yesterday", "confirmed": true}',
        '{"name": "wake", "at": true, "confirmed": true}',
        '{"name": "wake", "at": 1, "confirmed": "yes"}',
    ],
)
def test_bad_last_action_file_is_ignored(caplog: pytest.LogCaptureFixture, content: str) -> None:
    _ = history.HISTORY_FILE.write_text(content)
    restart_bot()
    assert "No power action" in history.last_action_line(config.FIRST_MACHINE)
    assert "Ignoring" in caplog.text


def test_last_action_save_failure_is_only_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "missing-dir" / "last_action.json")
    history.remember(config.FIRST_MACHINE, "wake")  # must not raise
    assert "woke it" in history.last_action_line(config.FIRST_MACHINE)
    assert "Could not save the last action" in caplog.text


@pytest.mark.usefixtures("fake_run")
def test_on_startup_loads_the_last_action() -> None:
    history.remember(config.FIRST_MACHINE, "sleep")
    history._last_actions.clear()  # pyright: ignore[reportPrivateUsage]
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(return_value=[])
    asyncio.run(application.on_startup(app))
    assert "put it to sleep" in history.last_action_line(config.FIRST_MACHINE)


@pytest.mark.parametrize(
    ("seconds", "text"), [(3600, "1h"), (7500, "2h 5m"), (86400, "1d"), (97200, "1d 3h")]
)
def test_duration_hours_and_days(seconds: float, text: str) -> None:
    assert messages.duration(seconds) == text


# --- uptime in /status -------------------------------------------------------


def test_status_shows_last_boot(workstation_on: FakeRun) -> None:
    workstation_on.by_last_arg[oscommands.WINDOWS_DETAILS] = system.RunResult(
        0, out='{"boot":11520}\r\n'
    )
    update = make_update()
    call(handlers.status, update)
    assert "Last boot 3h 12m ago." in replies(update)[0]
    assert sorted(workstation_on.ssh_commands()) == sorted([oscommands.WINDOWS_DETAILS, "quser"])


@pytest.mark.parametrize(
    "result", [system.RunResult(255, "denied"), system.RunResult(0, out="n/a")]
)
def test_status_without_uptime(workstation_on: FakeRun, result: system.RunResult) -> None:
    workstation_on.results["ssh"] = result
    update = make_update()
    call(handlers.status, update)
    assert "Awake" in replies(update)[0]
    assert "Last boot" not in replies(update)[0]


def test_status_skips_uptime_when_ssh_closed(fake_run: FakeRun) -> None:
    fake_run.results["ping"] = system.RunResult(0)
    call(handlers.status, make_update())
    assert fake_run.ssh_commands() == []


# --- richer awake /status ------------------------------------------------------


def details_json(**overrides: object) -> system.RunResult:
    data: dict[str, object] = {
        "boot": 11520,
        "cpu": 12,
        "memFree": 22 * GIB,
        "memTotal": 32 * GIB,
        "diskFree": 412 * GIB,
        "diskTotal": 953 * GIB,
    }
    data.update(overrides)
    return system.RunResult(0, out=json.dumps(data) + "\r\n")


def awake_status(_fake: FakeRun) -> str:
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    return text


def test_status_shows_usage_and_sessions(workstation_on: FakeRun) -> None:
    workstation_on.by_last_arg[oscommands.WINDOWS_DETAILS] = details_json()
    workstation_on.by_last_arg["quser"] = system.RunResult(0, out=QUSER_OUT)
    text = awake_status(workstation_on)
    assert "Last boot 3h 12m ago." in text
    assert "CPU 12% · RAM 10 GB / 32 GB · C: 412 GB free" in text
    assert "👤 Signed in: <code>alice</code> (active, in use now)" in text
    assert "<code>bob</code> (disconnected, idle 1:02)" in text
    assert "almost full" not in text


def test_status_warns_when_disk_is_almost_full(workstation_on: FakeRun) -> None:
    workstation_on.by_last_arg[oscommands.WINDOWS_DETAILS] = details_json(diskFree=50 * GIB)
    text = awake_status(workstation_on)
    assert "⚠️ C: is almost full: 50 GB free of 953 GB." in text
    assert "C: 50 GB free" not in text  # said once, as the warning


def test_status_nobody_signed_in(workstation_on: FakeRun) -> None:
    workstation_on.by_last_arg["quser"] = system.RunResult(1, "No User exists for *\r\n")
    assert "👤 Nobody signed in." in awake_status(workstation_on)


@pytest.mark.parametrize(
    "overrides",
    [{"cpu": None}, {"cpu": "busy"}, {"memTotal": 0}, {"diskTotal": None}, {"boot": True}],
)
def test_status_skips_missing_or_odd_fields(
    workstation_on: FakeRun, overrides: dict[str, object]
) -> None:
    workstation_on.by_last_arg[oscommands.WINDOWS_DETAILS] = details_json(**overrides)
    text = awake_status(workstation_on)  # must not raise
    assert "Awake" in text and "None" not in text and "busy" not in text


@pytest.mark.parametrize(
    "result",
    [
        system.RunResult(255, "Connection reset"),
        system.RunResult(0, out="not json"),
        system.RunResult(0, out="[1, 2]"),
        system.RunResult(-1, "", timed_out=True),
    ],
)
def test_status_without_details(workstation_on: FakeRun, result: system.RunResult) -> None:
    workstation_on.by_last_arg[oscommands.WINDOWS_DETAILS] = result
    text = awake_status(workstation_on)
    assert "Awake" in text and "Last boot" not in text and "CPU" not in text


def test_status_says_when_ssh_details_fail_entirely(workstation_on: FakeRun) -> None:
    workstation_on.results["ssh"] = system.RunResult(255, "Connection reset")
    assert "Couldn't read details over SSH." in awake_status(workstation_on)


def test_status_only_runs_read_only_ssh(workstation_on: FakeRun) -> None:
    _ = awake_status(workstation_on)
    assert sorted(workstation_on.ssh_commands()) == sorted([oscommands.WINDOWS_DETAILS, "quser"])


def test_details_command_is_cmd_safe() -> None:
    cmd = oscommands.WINDOWS_DETAILS
    assert cmd.count('"') == 2  # one quoted script for cmd.exe to hand to PowerShell
    assert not set("%^&<>") & set(cmd)  # nothing cmd.exe would interpret
    for word in ("Stop-", "Restart-", "Set-", "Remove-", "shutdown", "Suspend"):
        assert word not in cmd  # read-only


@pytest.mark.parametrize(
    ("num_bytes", "text"),
    [(9.84 * GIB, "9.8 GB"), (412 * GIB, "412 GB"), (1863 * GIB, "1.8 TB"), (0, "0.0 GB")],
)
def test_size_formatting(num_bytes: float, text: str) -> None:
    assert probes.size(num_bytes) == text
