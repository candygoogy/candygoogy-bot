# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Scheduled shutdown and wake, and the "when?" prompt."""

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from candygoogy import app as application
from candygoogy import (
    config,
    handlers,
    power,
    timers,
)
from tests.helpers import (
    AUTHORIZED_ID,
    STRANGER_ID,
    FakeRun,
    button_data,
    call,
    call_with_args,
    edits,
    fake_app,
    local,
    make_button_press,
    make_update,
    replies,
    sent_texts,
    set_timer,
)

# --- timers ------------------------------------------------------------------


# The machine every timer here is for (the only one configured).
FIRST = config.FIRST_MACHINE.name


def saved_timers() -> dict[str, object]:
    """The first machine's part of timers.json ({machine: {action: …}})."""
    return json.loads(timers.TIMERS_FILE.read_text()).get(FIRST, {})


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("45m", 45 * 60),
        ("90", 90 * 60),
        ("2h", 7200),
        ("1h30m", 5400),
        ("1h 30m", 5400),
        ("1D", 86400),
        ("7d", 7 * 86400),
        ("1m", 60),
    ],
)
def test_parse_durations(text: str, seconds: int) -> None:
    assert timers.parse_when(text, 1000.0) == 1000.0 + seconds


@pytest.mark.parametrize("text", ["", "soon", "30s", "h", "0", "0m", "8d", "1h-5m", "25:00", "7:5"])
def test_parse_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        _ = timers.parse_when(text, 1000.0)


@pytest.mark.parametrize(
    ("now", "text", "due"),
    [
        (local(20), "21:45", local(21, 45)),
        (local(20), "21.45", local(21, 45)),
        (local(22, 30), "21:45", local(21, 45, day=16)),
        (local(21, 45), "21:45", local(21, 45, day=16)),  # not "right now"
        (local(5), "6:50", local(6, 50)),
    ],
)
def test_parse_clock_time_is_next_occurrence(now: float, text: str, due: float) -> None:
    assert timers.parse_when(text, now) == due


def test_parse_clock_time_rolls_over_month_end() -> None:
    now = time.mktime((2026, 9, 30, 22, 15, 0, 0, 0, -1))
    assert timers.parse_when("06:50", now) == time.mktime((2026, 10, 1, 6, 50, 0, 0, 0, -1))


def test_when_text() -> None:
    now = local(20)
    assert timers.when_text(local(21, 45), now) == "21:45"
    assert timers.when_text(local(6, 50, day=16), now) == "tomorrow 06:50"
    assert timers.when_text(local(6, 50, day=18), now) == "Fri 18 Sep 06:50"


@pytest.mark.parametrize("args", [("soon",), ("8d",)])
def test_schedule_without_valid_time_explains(fake_run: FakeRun, args: tuple[str, ...]) -> None:
    update = make_update()
    call_with_args(timers.schedule_shutdown, update, *args)
    [text] = replies(update)
    assert "/schedule_shutdown 45m" in text
    assert update.effective_message.reply_text.call_args.kwargs["reply_markup"] is None
    assert fake_run.calls == []
    assert not timers.TIMERS_FILE.exists()


@pytest.mark.parametrize("name", ["shutdown", "wake"])
def test_schedule_asks_first_and_sets_nothing(fake_run: FakeRun, name: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[f"schedule_{name}"], update, "1h", "30m")
    assert "in 1h 30m" in replies(update)[0]
    [confirm, cancel] = button_data(update)
    assert confirm.startswith(f"sched:{name}:")
    assert cancel == "cancel:sched"
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]
    assert fake_run.calls == []


def test_schedule_prompt_mentions_replaced_timer() -> None:
    _ = set_timer("wake", time.time() + 3600)
    update = make_update()
    call_with_args(timers.schedule_wake, update, "2h")
    assert "Replaces the one set for" in replies(update)[0]


def sched_data(name: str, due: float, age_seconds: float = 0) -> str:
    return f"sched:{name}:{int(due)}:{int(time.time() - age_seconds)}"


def test_confirming_sets_and_saves_timer(fake_run: FakeRun) -> None:
    due = time.time() + 3600
    update = make_button_press(sched_data("shutdown", due))
    update.callback_query.message.chat.id = AUTHORIZED_ID
    call(handlers.on_button, update)
    [text] = edits(update)
    assert "Shutdown scheduled" in text
    assert "warn you 5m before" in text
    assert timers._timers[(FIRST, "shutdown")].due == int(due)  # pyright: ignore[reportPrivateUsage]
    assert saved_timers() == {"shutdown": {"due": int(due), "chat_id": AUTHORIZED_ID}}
    assert fake_run.calls == []  # nothing happens until it's due


def test_expired_schedule_confirmation_sets_nothing() -> None:
    update = make_button_press(
        sched_data("wake", time.time() + 3600, config.CONFIRM_TTL_SECONDS + 5)
    )
    call(handlers.on_button, update)
    assert "Expired" in edits(update)[0]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("due_in", "age"),
    [
        (-1, 0),  # already past (e.g. "22:30" asked at 22:29:30, confirmed after 22:30)
        (0, 0),
        (timers.MAX_TIMER_SECONDS + 1, 0),  # further than any answer to "when?"
        (timers.MAX_TIMER_SECONDS, -30),  # "issued" in the future: forged, no wider window
        (10 * 365 * 86400, 0),
    ],
)
def test_forged_schedule_times_set_nothing(
    caplog: pytest.LogCaptureFixture, due_in: int, age: int
) -> None:
    """The Schedule button's due time is checked again: callback data can be forged."""
    caplog.set_level(logging.INFO)
    now = int(time.time())
    update = make_button_press(f"sched:shutdown:{now + due_in}:{now - age}")
    call(handlers.on_button, update)
    assert edits(update) == ["✖️ Cancelled. No timer set."]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]
    assert "event=timer_rejected" in caplog.text


@pytest.mark.parametrize("due_in", [2, 120, timers.MAX_TIMER_SECONDS])
def test_schedule_times_inside_the_window_are_set(due_in: int) -> None:
    now = int(time.time())
    update = make_button_press(f"sched:wake:{now + due_in}:{now}")
    call(handlers.on_button, update)
    assert "Wake scheduled" in edits(update)[0]
    assert timers._timers[(FIRST, "wake")].due == now + due_in  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("data", ["cancel:sched", "sched:reboot:1:1", "sched:wake:soon:1"])
def test_cancelled_or_bad_schedule_sets_nothing(data: str) -> None:
    update = make_button_press(data)
    call(handlers.on_button, update)
    assert "No timer set" in edits(update)[0]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


def test_unsaved_timer_is_flagged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(timers, "TIMERS_FILE", tmp_path / "missing-dir" / "timers.json")
    update = make_button_press(sched_data("wake", time.time() + 3600))
    update.callback_query.message.chat.id = AUTHORIZED_ID
    call(handlers.on_button, update)
    assert "Couldn't save it to disk" in edits(update)[0]
    assert (FIRST, "wake") in timers._timers  # pyright: ignore[reportPrivateUsage]  # still armed


def test_timers_lists_pending_with_buttons() -> None:
    _ = set_timer("wake", time.time() + 7200)
    shutdown = set_timer("shutdown", time.time() + 3600)
    update = make_update()
    call(timers.timers, update)
    text = replies(update)[0]
    assert text.index("Shutdown at") < text.index("Wake at")  # soonest first
    ref = f"shutdown:{int(shutdown.due)}"
    assert button_data(update)[:2] == [f"timer:cancel:{ref}", f"timer:snooze:{ref}"]


def test_timers_when_none() -> None:
    update = make_update()
    call(timers.timers, update)
    assert "No timers" in replies(update)[0]


@pytest.mark.usefixtures("fake_run")
def test_status_lists_timers() -> None:
    _ = set_timer("wake", time.time() + 3600)
    update = make_update()
    call(handlers.status, update)
    assert "Wake at" in replies(update)[0]


def test_cancel_timer_button() -> None:
    timer = set_timer("shutdown", time.time() + 3600)
    _ = timers.save_timers()
    update = make_button_press(f"timer:cancel:shutdown:{int(timer.due)}")
    call(handlers.on_button, update)
    assert "Timer cancelled" in edits(update)[0]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]
    assert saved_timers() == {}


def test_snooze_moves_timer_by_30_minutes() -> None:
    timer = set_timer("shutdown", time.time() + 240)
    update = make_button_press(f"timer:snooze:shutdown:{int(timer.due)}")
    call(handlers.on_button, update)
    assert "postponed" in edits(update)[0]
    moved = timers._timers[(FIRST, "shutdown")]  # pyright: ignore[reportPrivateUsage]
    assert moved.due == timer.due + timers.SNOOZE_SECONDS
    assert saved_timers()["shutdown"] == {"due": moved.due, "chat_id": AUTHORIZED_ID}


@pytest.mark.parametrize("op", ["cancel", "snooze"])
def test_stale_timer_button_does_nothing(op: str) -> None:
    timer = set_timer("shutdown", time.time() + 3600)
    update = make_button_press(f"timer:{op}:shutdown:{int(timer.due) - 60}")
    call(handlers.on_button, update)
    assert timers._timers == {(FIRST, "shutdown"): timer}  # pyright: ignore[reportPrivateUsage]
    update.callback_query.answer.assert_awaited_once_with("That timer already ran or was changed.")


def test_timer_buttons_can_be_used_twice_on_one_message() -> None:
    wake_timer = set_timer("wake", time.time() + 3600)
    shutdown_timer = set_timer("shutdown", time.time() + 7200)
    call(handlers.on_button, make_button_press(f"timer:cancel:wake:{int(wake_timer.due)}", 5))
    call(
        handlers.on_button, make_button_press(f"timer:cancel:shutdown:{int(shutdown_timer.due)}", 5)
    )
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


def run_fired(app: MagicMock, tasks: list[Coroutine[Any, Any, None]], timer: timers.Timer) -> None:
    async def main() -> None:
        await timers.fire_timer(app, timer)
        for task in tasks:
            await task

    asyncio.run(main())


def test_wake_timer_sends_magic_packet(fake_run: FakeRun) -> None:
    app, tasks = fake_app()
    run_fired(app, tasks, timers.Timer("wake", time.time(), AUTHORIZED_ID))
    assert ("wol", config.FIRST_MACHINE.mac, "255.255.255.255") in fake_run.calls
    header, waking = sent_texts(app)[:2]
    assert "Scheduled wake" in header
    assert "Waking it up" in waking
    assert {c.args[0] for c in app.bot.send_message.call_args_list} == {AUTHORIZED_ID}


def test_wake_timer_skips_when_already_on(workstation_on: FakeRun) -> None:
    app, tasks = fake_app()
    run_fired(app, tasks, timers.Timer("wake", time.time(), AUTHORIZED_ID))
    assert "wol" not in workstation_on.programs()
    assert "Already on" in sent_texts(app)[-1]


def test_shutdown_timer_shuts_down(workstation_on: FakeRun) -> None:
    app, tasks = fake_app()
    run_fired(app, tasks, timers.Timer("shutdown", time.time(), AUTHORIZED_ID))
    assert "shutdown /s /t 0" in workstation_on.ssh_commands()
    edited = [c.args[0] for c in app.bot.send_message.return_value.edit_text.call_args_list]
    assert any("Shutting down" in t for t in edited)


def test_shutdown_timer_does_nothing_when_off(fake_run: FakeRun) -> None:
    app, tasks = fake_app()
    run_fired(app, tasks, timers.Timer("shutdown", time.time(), AUTHORIZED_ID))
    assert fake_run.ssh_commands() == []
    edited = [c.args[0] for c in app.bot.send_message.return_value.edit_text.call_args_list]
    assert "Unreachable" in edited[-1]


def test_shutdown_timer_refused_while_busy(workstation_on: FakeRun) -> None:
    power.claim(config.FIRST_MACHINE, "wake")
    app, tasks = fake_app()
    run_fired(app, tasks, timers.Timer("shutdown", time.time(), AUTHORIZED_ID))
    assert workstation_on.ssh_commands() == []


@pytest.mark.usefixtures("workstation_on")
def test_run_timer_warns_then_fires_and_forgets(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def instant(when: float) -> None:
        slept.append(when)

    fired: list[timers.Timer] = []

    async def fire(_app: object, timer: timers.Timer) -> None:
        fired.append(timer)

    monkeypatch.setattr(timers, "sleep_until", instant)
    monkeypatch.setattr(timers, "fire_timer", fire)
    app, _ = fake_app()
    timer = timers.Timer("shutdown", time.time() + 3600, AUTHORIZED_ID)

    async def main() -> None:
        timers.start_timer(app, timer)
        _ = timers.save_timers()
        await timers._timer_tasks[(FIRST, "shutdown")]  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())
    assert slept == [timer.due - timers.TIMER_WARNING_SECONDS, timer.due]
    [warning] = sent_texts(app)
    assert "Shutting down in" in warning
    markup = app.bot.send_message.call_args.kwargs["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        f"timer:cancel:shutdown:{int(timer.due)}",
        f"timer:snooze:shutdown:{int(timer.due)}",
    ]
    assert fired == [timer]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]
    assert saved_timers() == {}


def test_shutdown_warning_when_already_off(fake_run: FakeRun) -> None:
    app, _ = fake_app()
    timer = timers.Timer("shutdown", time.time() + timers.TIMER_WARNING_SECONDS, AUTHORIZED_ID)
    asyncio.run(timers.warn_before_shutdown(app, timer))
    [warning] = sent_texts(app)
    assert "already asleep or off" in warning
    assert "Shutting down" not in warning and "Unsaved work" not in warning
    assert fake_run.ssh_commands() == []  # only probed; nothing sent to the PC


def test_short_shutdown_timer_has_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fire(_app: object, _timer: timers.Timer) -> None:
        pass

    monkeypatch.setattr(timers, "fire_timer", fire)
    app, _ = fake_app()

    async def main() -> None:
        timers.start_timer(app, timers.Timer("shutdown", time.time() - 1, AUTHORIZED_ID))
        await timers._timer_tasks[(FIRST, "shutdown")]  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())
    app.bot.send_message.assert_not_awaited()


def test_new_timer_replaces_old_task() -> None:
    app, _ = fake_app()

    async def main() -> None:
        timers.start_timer(app, timers.Timer("wake", time.time() + 3600, AUTHORIZED_ID))
        old = timers._timer_tasks[(FIRST, "wake")]  # pyright: ignore[reportPrivateUsage]
        timers.start_timer(app, timers.Timer("wake", time.time() + 7200, AUTHORIZED_ID))
        await asyncio.sleep(0)
        assert old.cancelled()

    asyncio.run(main())


def test_restore_rearms_future_and_reports_missed() -> None:
    now = time.time()
    _ = timers.TIMERS_FILE.write_text(
        json.dumps(
            {
                "wake": {"due": now + 3600, "chat_id": AUTHORIZED_ID},
                "shutdown": {
                    "due": now - timers.MISSED_GRACE_SECONDS - 60,
                    "chat_id": AUTHORIZED_ID,
                },
            }
        )
    )
    app, _ = fake_app()

    async def main() -> list[timers.Timer]:
        missed, dropped = timers.restore_timers(app)
        assert dropped == []
        return missed

    [missed] = asyncio.run(main())
    assert missed.action == "shutdown"
    assert list(timers._timers) == [(FIRST, "wake")]  # pyright: ignore[reportPrivateUsage]
    assert list(saved_timers()) == ["wake"]


def test_restore_runs_slightly_overdue_timer() -> None:
    _ = timers.TIMERS_FILE.write_text(
        json.dumps({"wake": {"due": time.time() - 30, "chat_id": AUTHORIZED_ID}})
    )
    app, _ = fake_app()

    async def main() -> None:
        assert timers.restore_timers(app) == ([], [])
        assert (FIRST, "wake") in timers._timers  # pyright: ignore[reportPrivateUsage]  # armed, about to fire
        _ = timers._timer_tasks[(FIRST, "wake")].cancel()  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"wake": 5}',
        '{"reboot": {"due": 1, "chat_id": 1}}',
        '{"wake": {"due": "soon", "chat_id": 1}}',
    ],
)
def test_bad_timers_file_is_ignored(content: str) -> None:
    _ = timers.TIMERS_FILE.write_text(content)
    assert timers.load_timers() == []


def test_missing_timers_file_is_empty() -> None:
    assert timers.load_timers() == []


def test_stop_timers_cancels_but_keeps_file() -> None:
    app, _ = fake_app()

    async def main() -> None:
        timers.start_timer(app, timers.Timer("wake", time.time() + 3600, AUTHORIZED_ID))
        _ = timers.save_timers()
        task = timers._timer_tasks[(FIRST, "wake")]  # pyright: ignore[reportPrivateUsage]
        await timers.stop_timers(app)
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(main())
    assert list(saved_timers()) == ["wake"]


# --- /schedule_* without a time asks "when?" ---------------------------------


def typed(text: str) -> MagicMock:
    update = make_update()
    update.effective_message.text = text
    return update


@pytest.mark.parametrize(("name", "presets"), list(timers.WHEN_PRESETS.items()))
def test_schedule_without_time_asks_when(fake_run: FakeRun, name: str, presets: tuple[str]) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[f"schedule_{name}"], update)
    assert "when?" in replies(update)[0]
    assert button_data(update) == [*(f"when:{name}:{p}" for p in presets), "cancel:sched"]
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]
    assert fake_run.calls == []


def test_preset_turns_into_confirmation_on_same_message() -> None:
    call_with_args(timers.schedule_shutdown, make_update())
    update = make_button_press("when:shutdown:30m", message_id=9)
    call(handlers.on_button, update)
    [text] = edits(update)
    assert "Schedule a shutdown?" in text
    assert "in 30m" in text
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    confirm = markup.inline_keyboard[0][0].callback_data
    assert confirm.startswith("sched:shutdown:")
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]  # not until confirmed

    # Confirming on that same message still works (the preset didn't use it up).
    press = make_button_press(confirm, message_id=9)
    press.callback_query.message.chat.id = AUTHORIZED_ID
    call(handlers.on_button, press)
    assert (FIRST, "shutdown") in timers._timers  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("data", ["when:reboot:30m", "when:shutdown:soon", "when:"])
def test_bad_preset_sets_nothing(data: str) -> None:
    update = make_button_press(data)
    call(handlers.on_button, update)
    assert "No timer set" in edits(update)[0]


def test_typed_answer_gets_confirmation() -> None:
    call_with_args(timers.schedule_wake, make_update())
    update = typed("06:50")
    call(timers.on_text, update)
    assert "Schedule a wake?" in replies(update)[0]
    assert button_data(update)[0].startswith("sched:wake:")
    # Answered: later text is ordinary chat again.
    later = typed("45m")
    call(timers.on_text, later)
    assert replies(later) == []


def test_bad_typed_answer_explains_and_keeps_listening() -> None:
    call_with_args(timers.schedule_shutdown, make_update())
    bad = typed("later please")
    call(timers.on_text, bad)
    assert "Couldn't read" in replies(bad)[0]
    good = typed("1h")
    call(timers.on_text, good)
    assert "Schedule a shutdown?" in replies(good)[0]


def test_text_is_ignored_when_not_asked() -> None:
    update = typed("30m")
    call(timers.on_text, update)
    assert replies(update) == []


def test_typed_answer_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    call_with_args(timers.schedule_shutdown, make_update())
    clock[0] += timers.ASK_WHEN_TTL_SECONDS + 1
    update = typed("30m")
    call(timers.on_text, update)
    assert replies(update) == []


def test_cancel_stops_listening() -> None:
    call_with_args(timers.schedule_shutdown, make_update())
    call(handlers.on_button, make_button_press("cancel:sched"))
    update = typed("30m")
    call(timers.on_text, update)
    assert replies(update) == []


@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_strangers_cannot_answer_when(user_id: int | None) -> None:
    call_with_args(timers.schedule_shutdown, make_update())
    update = make_update(user_id)
    update.effective_message.text = "1m"
    call(timers.on_text, update)
    assert replies(update) == []


def test_build_app_registers_text_handler() -> None:
    app = application.build_app()
    assert any(getattr(h, "callback", None) is handlers.on_text for h in app.handlers[0])


def test_typed_answer_reaches_when_through_the_text_handler() -> None:
    call_with_args(timers.schedule_wake, make_update())
    update = typed("06:50")
    call(handlers.on_text, update)
    assert "Schedule a wake?" in replies(update)[0]


@pytest.mark.usefixtures("fake_run")
def test_keyboard_tap_stops_listening_for_when() -> None:
    call_with_args(timers.schedule_shutdown, make_update())
    call(handlers.on_text, typed("ℹ️ About"))  # moved on to something else
    update = typed("30m")
    call(handlers.on_text, update)
    assert replies(update) == []


# --- /timers with nothing set ---------------------------------------------------


def test_empty_timers_offers_schedule_buttons(fake_run: FakeRun) -> None:
    update = make_update()
    call(timers.timers, update)
    assert "No timers" in replies(update)[0]
    data = button_data(update)
    assert data == ["menu:schedule_shutdown", "menu:schedule_wake"]
    assert {d.removeprefix("menu:") for d in data} <= handlers.MENU_COMMANDS
    assert fake_run.calls == []


@pytest.mark.parametrize("action", ["shutdown", "wake"])
def test_schedule_button_asks_when(fake_run: FakeRun, action: str) -> None:
    update = make_button_press(f"menu:schedule_{action}")
    call(handlers.on_button, update)
    assert "when?" in replies(update)[0]
    assert fake_run.calls == []  # only asks; nothing scheduled or run
    assert timers.pending_timers() == []
