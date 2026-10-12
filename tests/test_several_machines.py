# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Several machines in chat: the picker, machine arguments, per-machine
timers, history and on-time, the status overview, reports, migrations of the old
single-machine files, and crafted names that must never reach anything."""

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from telegram import InlineKeyboardMarkup

from candygoogy import app as application
from candygoogy import (
    config,
    digest,
    handlers,
    history,
    machines,
    oscommands,
    pctime,
    power,
    probes,
    system,
    timers,
    weekly,
)
from candygoogy.settings import Machine
from tests.helpers import (
    AUTHORIZED_ID,
    SECOND_ID,
    FakeRun,
    call,
    call_with_args,
    capturing_context,
    edits,
    fake_app,
    make_button_press,
    make_update,
    replies,
    sent_texts,
)

DESK = dataclasses.replace(config.FIRST_MACHINE, name="desk")
NAS = dataclasses.replace(
    DESK, name="nas", os="linux", mac="00:00:5e:00:53:02", ip="192.0.2.20", ssh_port=2222
)
MINI = dataclasses.replace(
    DESK,
    name="mini",
    os="macos",
    mac="00:00:5e:00:53:03",
    ip="192.0.2.30",
    wol_broadcast="192.0.2.255",
)
THREE = (DESK, NAS, MINI)


@pytest.fixture(autouse=True)
def three_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """machines.example.toml's three machines, as MACHINES_FILE would configure them."""
    monkeypatch.setattr(config, "MACHINES", THREE)


def run_with_args(handler: Callable[..., Any], update: MagicMock, *args: str) -> None:
    """A command with arguments, then any follow-up it started (like helpers.call)."""

    async def main() -> None:
        context, tasks = capturing_context()
        context.args = list(args)
        await handler(update, context)
        for task in tasks:
            await task

    asyncio.run(main())


def button_data(markup: InlineKeyboardMarkup | None) -> list[str]:
    if markup is None:
        return []
    return [str(b.callback_data) for row in markup.inline_keyboard for b in row]


def reply_buttons(update: MagicMock) -> list[str]:
    markup = update.effective_message.reply_text.call_args.kwargs.get("reply_markup")
    return button_data(markup if isinstance(markup, InlineKeyboardMarkup) else None)


def wol_calls(fake_run: FakeRun) -> list[tuple[str, ...]]:
    return [c for c in fake_run.calls if c[0] == "wol"]


def awake_by_name(monkeypatch: pytest.MonkeyPatch, **awake: bool) -> None:
    async def is_awake(machine: Machine) -> bool:
        return awake.get(machine.name, False)

    monkeypatch.setattr(probes, "is_awake", is_awake)


# --- Naming a machine ---------------------------------------------------------------------


def test_names_are_looked_up_case_insensitively() -> None:
    assert machines.named("desk") is DESK
    assert machines.named("NAS") is NAS
    assert machines.named("Mini") is MINI
    assert machines.named("") is None
    assert machines.named("laptop") is None


@pytest.mark.usefixtures("fake_run")
def test_wake_with_a_machine_argument(monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun) -> None:
    awake_by_name(monkeypatch)
    run_with_args(handlers.wake, make_update(), "NAS")
    assert wol_calls(fake_run) == [("wol", NAS.mac, "255.255.255.255")] * 3  # 3 packets
    fake_run.calls.clear()
    run_with_args(handlers.wake, make_update(), "mini")
    assert wol_calls(fake_run) == [("wol", MINI.mac, "192.0.2.255")] * 3  # 3 packets


@pytest.mark.parametrize("name", ["shutdown", "reboot", "sleep"])
def test_power_prompt_names_the_machine(workstation_on: FakeRun, name: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[name], update, "mini")
    [prompt] = replies(update)
    assert "mini" in prompt.splitlines()[0] and "workstation" not in prompt
    confirm, cancel = reply_buttons(update)
    assert confirm.startswith(f"confirm:{name}:") and confirm.endswith(":mini")
    assert cancel == "cancel"
    # Only the read-only session check (mini's own command) ran: nothing was sent yet.
    assert workstation_on.ssh_commands() in ([], [oscommands.MACOS_SESSIONS])


def test_confirming_runs_on_the_named_machine(workstation_on: FakeRun) -> None:
    press = make_button_press(f"confirm:shutdown:{int(time.time())}:nas")
    call(handlers.on_button, press)
    [ssh] = [c for c in workstation_on.calls if c[0] == "ssh"]
    assert f"{DESK.ssh_user}@{DESK.ip}" not in ssh  # not the desk's address
    assert f"{NAS.ssh_user}@{NAS.ip}" in ssh and ssh[-1] == "systemctl poweroff --no-ask-password"


@pytest.mark.usefixtures("fake_run")
def test_schedule_with_a_machine_and_a_time() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_wake"], update, "desk", "7:30")
    [prompt] = replies(update)
    assert "<b>Schedule a wake of desk?</b>" in prompt
    [schedule, cancel] = reply_buttons(update)
    assert schedule.startswith("sched:wake:") and schedule.endswith(":desk")
    assert cancel == "cancel:sched"


@pytest.mark.usefixtures("fake_run")
def test_schedule_without_a_time_asks_when_for_that_machine() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "nas")
    assert "Shutdown of nas when?" in replies(update)[0]
    presets = reply_buttons(update)[:-1]
    assert presets and all(p.startswith("when:shutdown:") and p.endswith(":nas") for p in presets)
    typed = make_update()
    typed.effective_message.text = "45m"
    call(handlers.on_text, typed)
    assert "<b>Schedule a shutdown of nas?</b>" in replies(typed)[0]


@pytest.mark.usefixtures("fake_run", "two_allowed_users")
def test_another_users_answer_never_names_my_machine() -> None:
    call_with_args(handlers.COMMANDS["schedule_shutdown"], make_update(AUTHORIZED_ID), "nas")
    call_with_args(handlers.COMMANDS["schedule_wake"], make_update(SECOND_ID), "mini")
    b = make_update(SECOND_ID)
    b.effective_message.text = "30m"
    call(handlers.on_text, b)
    [text] = replies(b)
    assert "<b>Schedule a wake of mini?</b>" in text and "nas" not in text
    assert reply_buttons(b)[0].startswith("sched:wake:") and reply_buttons(b)[0].endswith(":mini")
    a = make_update(AUTHORIZED_ID)
    a.effective_message.text = "1h"
    call(handlers.on_text, a)
    assert "<b>Schedule a shutdown of nas?</b>" in replies(a)[0]
    assert reply_buttons(a)[0].endswith(":nas")


# --- The picker ----------------------------------------------------------------------------

PICKED = ["wake", "sleep", "reboot", "shutdown", "schedule_shutdown", "schedule_wake"]


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize("command", PICKED)
def test_without_a_machine_the_bot_asks(command: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[command], update)
    [text] = replies(update)
    assert "Which machine?" in text
    assert reply_buttons(update) == [f"pick:{command}:{m.name}" for m in THREE]


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize(("label", "command"), [("⚡ Wake", "wake"), ("😴 Sleep", "sleep")])
def test_keyboard_taps_ask_too(label: str, command: str) -> None:
    update = make_update()
    update.effective_message.text = label
    call(handlers.on_text, update)
    assert reply_buttons(update) == [f"pick:{command}:{m.name}" for m in THREE]


@pytest.mark.usefixtures("fake_run")
def test_menu_buttons_ask_too() -> None:
    press = make_button_press("menu:schedule_shutdown")
    call(handlers.on_button, press)
    assert reply_buttons(press)[0] == "pick:schedule_shutdown:desk"


def test_pick_runs_the_command_for_that_machine(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    awake_by_name(monkeypatch)
    call(handlers.on_button, make_button_press("pick:wake:nas"))
    assert wol_calls(fake_run) == [("wol", NAS.mac, "255.255.255.255")] * 3  # 3 packets

    press = make_button_press("pick:schedule_wake:mini")
    call(handlers.on_button, press)
    assert "Wake of mini when?" in replies(press)[0]


def test_pick_shutdown_asks_for_confirmation(workstation_on: FakeRun) -> None:
    press = make_button_press("pick:shutdown:desk")
    call(handlers.on_button, press)
    assert "<b>Shut down desk?</b>" in replies(press)[0]
    assert workstation_on.ssh_commands() in ([], ["quser"])


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize("data", ["pick:wake:laptop", "pick:format:desk", "pick:", "pick:wake"])
def test_bad_pick_buttons_run_nothing(fake_run: FakeRun, data: str) -> None:
    press = make_button_press(data)
    call(handlers.on_button, press)
    press.callback_query.answer.assert_awaited_once_with("That machine isn't configured any more.")
    assert replies(press) == [] and fake_run.calls == []


# --- Crafted names, everywhere (they're only ever looked up) ---------------------------

CRAFTED = [
    "desk;rm -rf /",
    "-oProxyCommand=x",
    "x" * 5000,
    "dësk",
    "ＤＥＳＫ",  # fullwidth: lower() keeps it fullwidth
    "desk\nnas",
    "../desk",
    "$(id)",
]


@pytest.fixture
def nothing_runs(fake_run: FakeRun) -> Iterator[FakeRun]:
    yield fake_run
    assert fake_run.calls == []  # no ping, ssh, ip or magic packet for a crafted name
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("name", CRAFTED, ids=range(len(CRAFTED)))
@pytest.mark.parametrize("command", PICKED)
def test_crafted_names_as_command_arguments(nothing_runs: FakeRun, command: str, name: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[command], update, name, "45m")
    [text] = replies(update)
    assert "Unknown machine" in text
    assert name not in text  # never echoed
    assert nothing_runs.calls == []


@pytest.mark.parametrize("name", CRAFTED, ids=range(len(CRAFTED)))
@pytest.mark.parametrize(
    ("template", "said"),
    [
        ("pick:wake:{}", "That machine isn't configured any more."),
        ("confirm:shutdown:{now}:{}", "Expired"),
        ("sched:wake:{due}:{now}:{}", "Cancelled. No timer set."),
        ("when:shutdown:1h:{}", "Cancelled. No timer set."),
        ("timer:cancel:shutdown:{due}:{}", "That timer already ran or was changed."),
        ("status:{}", "That machine isn't configured any more."),
    ],
    ids=["pick", "confirm", "sched", "when", "timer", "status"],
)
def test_crafted_names_in_callback_data(
    nothing_runs: FakeRun, template: str, said: str, name: str
) -> None:
    """Treated as expired or cancelled, never as another machine, and never echoed."""
    now = int(time.time())
    data = template.format(name, now=now, due=now + 3600)
    press = make_button_press(data)
    call(handlers.on_button, press)  # never raises
    answers = [str(c.args[0]) for c in press.callback_query.answer.call_args_list if c.args]
    texts = edits(press) + replies(press) + answers
    assert any(said in t for t in texts), texts
    assert not any(name in t for t in texts)
    assert nothing_runs.calls == []


# --- Per-machine timers --------------------------------------------------------------------


def saved() -> dict[str, Any]:
    return json.loads(timers.TIMERS_FILE.read_text())


def set_and_confirm(action: str, machine: str, due: float) -> MagicMock:
    # Each on its own prompt message: a second tap on one message is ignored.
    message_id = 100 + len(timers._timers)  # pyright: ignore[reportPrivateUsage]
    data = f"sched:{action}:{int(due)}:{int(time.time())}:{machine}"
    press = make_button_press(data, message_id=message_id)
    call(handlers.on_button, press)
    return press


@pytest.mark.usefixtures("fake_run")
def test_one_timer_per_machine_and_action() -> None:
    due = time.time() + 3600
    for machine in ("desk", "nas"):
        assert "Shutdown scheduled" in edits(set_and_confirm("shutdown", machine, due))[0]
    _ = set_and_confirm("wake", "desk", due + 600)
    assert set(timers._timers) == {  # pyright: ignore[reportPrivateUsage]
        ("desk", "shutdown"),
        ("nas", "shutdown"),
        ("desk", "wake"),
    }
    assert saved() == {
        "desk": {
            "shutdown": {"due": int(due), "chat_id": AUTHORIZED_ID},
            "wake": {"due": int(due + 600), "chat_id": AUTHORIZED_ID},
        },
        "nas": {"shutdown": {"due": int(due), "chat_id": AUTHORIZED_ID}},
    }


@pytest.mark.usefixtures("fake_run")
def test_timers_list_names_and_buttons_carry_the_machine() -> None:
    due = time.time() + 3600
    _ = set_and_confirm("shutdown", "desk", due)
    _ = set_and_confirm("shutdown", "nas", due + 60)
    update = make_update()
    call(handlers.COMMANDS["timers"], update)
    [text] = replies(update)
    assert "Shutdown of desk at" in text and "Shutdown of nas at" in text
    data = reply_buttons(update)
    assert f"timer:cancel:shutdown:{int(due)}:desk" in data
    assert f"timer:snooze:shutdown:{int(due) + 60}:nas" in data


@pytest.mark.usefixtures("fake_run")
def test_cancel_and_snooze_touch_only_their_machine() -> None:
    due = int(time.time() + 3600)
    _ = set_and_confirm("shutdown", "desk", due)
    _ = set_and_confirm("shutdown", "nas", due)
    cancel = make_button_press(f"timer:cancel:shutdown:{due}:nas", message_id=7)
    call(handlers.on_button, cancel)
    assert "The shutdown of nas set for" in edits(cancel)[0]
    assert set(timers._timers) == {("desk", "shutdown")}  # pyright: ignore[reportPrivateUsage]
    snooze = make_button_press(f"timer:snooze:shutdown:{due}:desk", message_id=8)
    call(handlers.on_button, snooze)
    assert timers._timers[("desk", "shutdown")].due == due + timers.SNOOZE_SECONDS  # pyright: ignore[reportPrivateUsage]


def test_shutdown_warning_names_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    awake_by_name(monkeypatch, nas=True)
    app, _ = fake_app()
    timer = timers.Timer("shutdown", time.time() + 300, AUTHORIZED_ID, "nas")
    asyncio.run(timers.warn_before_shutdown(app, timer))
    [text] = sent_texts(app)
    assert text.startswith("⏰ <b>Shutting down nas in")
    data = button_data(app.bot.send_message.call_args.kwargs["reply_markup"])
    assert data[0].endswith(":nas") and data[0].startswith("timer:cancel:shutdown:")


def test_a_fired_wake_timer_wakes_its_machine(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    awake_by_name(monkeypatch)
    app, tasks = fake_app()

    async def fire() -> None:
        await timers.fire_timer(app, timers.Timer("wake", time.time(), AUTHORIZED_ID, "mini"))
        for task in tasks:
            await task

    asyncio.run(fire())
    assert "Scheduled wake of mini" in sent_texts(app)[0]
    assert wol_calls(fake_run)[0] == ("wol", MINI.mac, "192.0.2.255")


# --- Migrations of the single-machine files --------------------------------------------------


def restore() -> tuple[list[timers.Timer], list[timers.Timer]]:
    app, _ = fake_app()

    async def main() -> tuple[list[timers.Timer], list[timers.Timer]]:
        result = timers.restore_timers(app)
        for task in timers._timer_tasks.values():  # pyright: ignore[reportPrivateUsage]
            _ = task.cancel()
        return result

    return asyncio.run(main())


def test_old_timers_file_becomes_the_first_machines() -> None:
    due = time.time() + 3600
    _ = timers.TIMERS_FILE.write_text(
        json.dumps({"shutdown": {"due": due, "chat_id": AUTHORIZED_ID}})
    )
    assert restore() == ([], [])
    assert list(timers._timers) == [("desk", "shutdown")]  # pyright: ignore[reportPrivateUsage]
    _ = timers.save_timers()
    assert saved() == {"desk": {"shutdown": {"due": due, "chat_id": AUTHORIZED_ID}}}


def test_timers_of_a_removed_machine_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    due = time.time() + 3600
    _ = timers.TIMERS_FILE.write_text(
        json.dumps(
            {
                "laptop": {"wake": {"due": due, "chat_id": AUTHORIZED_ID}},
                "nas": {"wake": {"due": due, "chat_id": AUTHORIZED_ID}},
            }
        )
    )
    missed, [dropped] = restore()
    assert missed == [] and dropped.machine == "laptop"
    assert list(timers._timers) == [("nas", "wake")]  # pyright: ignore[reportPrivateUsage]
    assert saved() == {"nas": {"wake": {"due": due, "chat_id": AUTHORIZED_ID}}}
    assert "Dropped the saved wake timer of laptop" in caplog.text
    assert "event=timer_dropped reason=machine_not_configured machine=laptop" in caplog.text
    report = application.startup_report([], [], None, [dropped])
    assert report is not None and "Dropped the wake timer of laptop" in report


def test_missed_timers_name_their_machine() -> None:
    missed = timers.Timer("shutdown", time.time() - 3600, AUTHORIZED_ID, "nas")
    report = application.startup_report([], [missed])
    assert report is not None and "Missed the shutdown timer of nas for" in report


def test_old_last_action_file_becomes_the_first_machines() -> None:
    _ = history.HISTORY_FILE.write_text(
        json.dumps({"name": "sleep", "at": time.time(), "confirmed": True})
    )
    history.load_last_action()
    assert "put it to sleep" in history.last_action_line(DESK)
    assert "No power action" in history.last_action_line(NAS)
    data = json.loads(history.HISTORY_FILE.read_text())
    assert list(data) == ["desk"] and data["desk"]["name"] == "sleep"


def test_last_action_is_per_machine() -> None:
    history.remember(NAS, "shutdown")
    history.remember(DESK, "wake")
    history.settle(NAS, "shutdown", confirmed=True)
    assert "shut it down" in history.last_action_line(NAS)
    assert "woke it" in history.last_action_line(DESK)
    history._last_actions.clear()  # pyright: ignore[reportPrivateUsage]
    history.load_last_action()
    assert "(it went quiet)" in history.last_action_line(NAS)


def test_last_action_of_a_removed_machine_is_dropped() -> None:
    _ = history.HISTORY_FILE.write_text(
        json.dumps({"laptop": {"name": "wake", "at": 1, "confirmed": True}})
    )
    history.load_last_action()
    assert history._last_actions == {}  # pyright: ignore[reportPrivateUsage]


def test_old_pc_time_file_becomes_the_first_machines() -> None:
    _ = pctime.STATE_FILE.write_text(json.dumps({"since": 100.0, "sessions": [[200.0, 300.0]]}))
    pctime.load_state()
    assert pctime.tracked_since(DESK) == 100.0 and pctime.tracked_since(NAS) is None
    assert json.loads(pctime.STATE_FILE.read_text()) == {
        "desk": {"since": 100.0, "sessions": [[200.0, 300.0]]}
    }


def test_each_machine_is_checked_for_on_time(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run.by_last_arg[NAS.ip] = system.RunResult(0)  # nas answers ping
    monkeypatch.setattr(time, "time", lambda: 5000.0)
    asyncio.run(pctime.check())
    assert pctime.state_of(NAS).sessions == [pctime.Session(5000.0, 5000.0)]
    assert pctime.state_of(DESK).sessions == [] and pctime.tracked_since(DESK) == 5000.0
    assert set(json.loads(pctime.STATE_FILE.read_text())) == {"desk", "nas", "mini"}


def test_a_mac_put_to_sleep_gets_ping_only(fake_run: FakeRun) -> None:
    """Traffic to its SSH port could wake it through a Bonjour Sleep Proxy; ping doesn't."""
    fake_run.port22_open = True
    history.remember(MINI, "sleep")
    assert not asyncio.run(pctime.is_on(MINI))  # port 22 not tried
    assert asyncio.run(pctime.is_on(DESK))  # a Windows PC still is
    history.remember(MINI, "wake")
    assert asyncio.run(pctime.is_on(MINI))


# --- /status, the summary and the weekly report --------------------------------------------


def test_status_overview(fake_run: FakeRun) -> None:
    fake_run.by_last_arg[DESK.ip] = system.RunResult(0)  # desk answers ping
    update = make_update()
    call(handlers.status, update)
    [text] = replies(update)
    assert "📊 <b>Machines</b>" in text
    assert "🟢 <b>desk</b> · awake" in text
    assert "⚫ <b>nas</b> · off or asleep" in text and "⚫ <b>mini</b> · off or asleep" in text
    assert reply_buttons(update) == ["status:desk", "status:nas", "status:mini", "status"]
    assert all(c[0] in ("ping", "ip") for c in fake_run.calls)  # nothing ran on a machine


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize(("columns", "rows"), [(1, [1, 1, 1, 1]), (2, [2, 1, 1]), (3, [3, 1])])
def test_status_overview_rows_follow_picker_columns(
    monkeypatch: pytest.MonkeyPatch, columns: int, rows: list[int]
) -> None:
    """The machine buttons are laid out like the picker's (PICKER_COLUMNS)."""
    monkeypatch.setattr(machines, "PICKER_COLUMNS", columns)
    _, markup = asyncio.run(handlers.overview_card())
    assert [len(row) for row in markup.inline_keyboard] == rows  # the last row: Refresh


def test_status_overview_shows_a_busy_machine(fake_run: FakeRun) -> None:
    power.claim(NAS, "reboot")
    update = make_update()
    call(handlers.status, update)
    assert "<b>nas</b> · off or asleep · ⏳ busy" in replies(update)[0]
    assert fake_run.calls


@pytest.mark.usefixtures("workstation_on")
def test_a_machines_card_from_the_overview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "REFRESH_COOLDOWN_SECONDS", 0)
    timers._timers[("nas", "wake")] = timers.Timer("wake", time.time() + 3600, 1, "nas")  # pyright: ignore[reportPrivateUsage]
    timers._timers[("desk", "wake")] = timers.Timer("wake", time.time() + 60, 1, "desk")  # pyright: ignore[reportPrivateUsage]
    press = make_button_press("status:nas")
    call(handlers.on_button, press)
    [text] = edits(press)
    assert text.startswith("🖥 <b>nas</b>\n🟢 <b>Awake</b>")
    assert "Wake of nas at" in text and "of desk" not in text  # only its own timers
    markup = press.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert button_data(markup) == ["status:nas", "status"]
    back = make_button_press("status", message_id=2)
    call(handlers.on_button, back)
    assert "📊 <b>Machines</b>" in edits(back)[0]


@pytest.mark.usefixtures("fake_run", "lab")
def test_daily_summary_has_a_line_per_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    awake_by_name(monkeypatch, desk=True)
    text = asyncio.run(digest.digest_message())
    assert "🟢 desk is on." in text
    assert "⚫ nas is off or asleep." in text and "⚫ mini is off or asleep." in text
    assert "The PC" not in text


@pytest.mark.usefixtures("lab")
def test_weekly_report_has_a_line_per_machine() -> None:
    now = time.time()
    desk = pctime.state_of(DESK)
    desk.since, desk.sessions = now - 30 * 86400, [pctime.Session(now - 7200, now - 60)]
    pctime.state_of(NAS).since = now - 30 * 86400
    text = weekly.week_message(now)
    assert "💻 desk on 2h" in text
    assert "💻 nas wasn't on." in text
    assert "<i>💻 mini's on-time isn't tracked yet.</i>" in text
    assert "The PC" not in text


@pytest.mark.usefixtures("fake_run")
def test_start_mentions_the_machines() -> None:
    update = make_update()
    call(handlers.start, update)
    [text] = replies(update)
    assert "Machines: <code>desk</code>, <code>nas</code>, <code>mini</code>" in text
    assert "/wake desk" in text


# --- Busy lanes through the chat --------------------------------------------------------


def test_a_busy_machine_doesnt_block_another_in_chat(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    awake_by_name(monkeypatch)
    power.claim(DESK, "shutdown")
    blocked = make_update()
    run_with_args(handlers.wake, blocked, "desk")
    assert "Already shutting desk down" in replies(blocked)[0]
    run_with_args(handlers.wake, make_update(), "nas")
    assert wol_calls(fake_run) == [("wol", NAS.mac, "255.255.255.255")] * 3  # 3 packets


# --- Callback data budget on the real builders ------------------------------------------

LONG = dataclasses.replace(NAS, name="a" * 16, ip="192.0.2.40", mac="00:00:5e:00:53:04")


def every_callback(monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun) -> list[str]:
    """Callback data from every builder that carries a machine, for a 16-char name."""
    monkeypatch.setattr(config, "MACHINES", (DESK, LONG))
    fake_run.results["ping"] = system.RunResult(0)
    fake_run.port22_open = True
    far = 9_999_999_999.0
    timer = timers.Timer("shutdown", far, AUTHORIZED_ID, LONG.name)
    data = [b.callback_data for b in timers.timer_buttons(timer)]
    data += button_data(timers.when_buttons("shutdown", LONG))
    data += button_data(timers.schedule_prompt("shutdown", LONG, far, far)[1])
    data += button_data(machines.picker("schedule_shutdown")[1])
    for command in ("shutdown", "reboot", "sleep"):
        update = make_update()
        call_with_args(handlers.COMMANDS[command], update, LONG.name)
        data += reply_buttons(update)
    data += button_data(asyncio.run(handlers.overview_card())[1])
    data += button_data(asyncio.run(handlers.machine_card(LONG))[1])
    return [str(d) for d in data]


def test_callback_data_fits_with_the_longest_name(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeRun
) -> None:
    data = every_callback(monkeypatch, fake_run)
    assert len(data) >= 20
    assert any(d.endswith(LONG.name) for d in data)
    longest = max(data, key=lambda d: len(d.encode()))
    assert len(longest.encode()) <= 64, longest


# --- One machine: exactly as before ---------------------------------------------------


@pytest.fixture
def one_machine(monkeypatch: pytest.MonkeyPatch) -> Machine:
    monkeypatch.setattr(config, "MACHINES", (config.FIRST_MACHINE,))
    return config.FIRST_MACHINE


def run_everything(fake_run: FakeRun, awake_sequence: Callable[..., None]) -> list[str]:
    """Every command a user can send, with a timer set; all text and button data."""
    fake_run.results["ping"] = system.RunResult(0)
    fake_run.port22_open = True
    awake_sequence(True)
    out: list[str] = []
    for command in ("start", "status", "wake", "sleep", "reboot", "shutdown",
                    "schedule_shutdown", "schedule_wake", "summary", "timers"):  # fmt: skip
        update = make_update()
        call_with_args(handlers.COMMANDS[command], update)
        out += replies(update) + reply_buttons(update)
    press = make_button_press(f"sched:wake:{int(time.time()) + 3600}:{int(time.time())}")
    call(handlers.on_button, press)
    out += edits(press)
    update = make_update()
    call(handlers.COMMANDS["timers"], update)
    out += replies(update) + reply_buttons(update)
    return out


@pytest.mark.usefixtures("one_machine", "lab")
def test_one_machine_has_no_picker_and_no_names(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    out = run_everything(fake_run, awake_sequence)
    joined = "\n".join(out)
    assert "Which machine" not in joined and "pick:" not in joined
    assert "Unknown machine" not in joined and "Machines:" not in joined
    assert not any(d.endswith(":workstation") for d in out)
    assert "<b>workstation</b>" not in joined and "of workstation" not in joined
    assert "Put the workstation to sleep?" in joined  # today's wording
