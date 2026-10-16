# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Repeating schedules in chat: /schedule_* with day words, the typed "when?"
answer, the confirmation, /timers' Repeating block and its buttons, and the
buttons under a repeating shutdown's warning."""

import dataclasses
import time
from datetime import date
from unittest.mock import MagicMock

import pytest

from candygoogy import config, handlers, repeating, schedules, settings, timers
from candygoogy.schedules import DAYS_ERROR, DISABLED, TIME_ERROR, TOO_LONG, Schedule
from tests.helpers import (
    AUTHORIZED_ID,
    button_data,
    call,
    call_with_args,
    edits,
    make_button_press,
    make_update,
    replies,
)

FIRST = config.FIRST_MACHINE.name
MON = date(2026, 7, 6)  # a Monday; July has no clock change anywhere
TUE = date(2026, 7, 7)


def at(day: date, hour: int, minute: int = 0) -> float:
    return time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))


NOW = at(MON, 12)


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Monday 6 July 2026, 12:00 local, and arm() records instead of starting tasks."""
    clock = [NOW]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def no_task(_app: object, _s: Schedule, _first: object = None) -> None:
        pass

    monkeypatch.setattr(schedules, "arm", no_task)
    return clock


def typed(text: str) -> MagicMock:
    update = make_update()
    update.effective_message.text = text
    return update


def confirm_data(update: MagicMock) -> str:
    return button_data(update)[0]


def press(data: str, message_id: int = 1) -> MagicMock:
    update = make_button_press(data, message_id=message_id)
    call(handlers.on_button, update)
    return update


def set_one(
    text: str = "weekdays 23:00", action: str = "shutdown", message_id: int = 1
) -> MagicMock:
    """/schedule_<action> <text>, then the Repeat button."""
    update = make_update()
    call_with_args(handlers.COMMANDS[f"schedule_{action}"], update, *text.split())
    return press(confirm_data(update), message_id)


def only_schedule() -> Schedule:
    [(s, _)] = schedules.pending_schedules()
    return s


def timers_card() -> tuple[str, list[str]]:
    update = make_update()
    call(handlers.COMMANDS["timers"], update)
    [text] = replies(update)
    return text, button_data(update)


# --- Asking ----------------------------------------------------------------------------


def test_day_words_ask_to_repeat() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "weekdays", "23:00")
    [text] = replies(update)
    assert "🔁 <b>Repeat a shutdown?</b>" in text
    assert "Every weekday at 23:00. First: 23:00." in text
    [repeat, cancel] = button_data(update)
    assert repeat == f"rsched:shutdown:31:2300:{int(NOW)}"
    assert cancel == "cancel:sched"
    assert schedules.pending_schedules() == []  # nothing until confirmed


@pytest.mark.parametrize(
    ("words", "every"),
    [
        (["daily", "7:30"], "Every day at 07:30"),
        (["weekends", "10:00"], "Every Saturday and Sunday at 10:00"),
        (["mon,", "wed,fri", "07:30"], "Every Monday, Wednesday and Friday at 07:30"),
        (["sun", "9:00"], "Every Sunday at 09:00"),
    ],
)
def test_the_question_spells_the_days_out(words: list[str], every: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_wake"], update, *words)
    assert every in replies(update)[0]


def test_a_plain_time_is_still_a_one_shot_timer() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "23:00")
    assert "Schedule a shutdown?" in replies(update)[0]
    assert confirm_data(update).startswith("sched:shutdown:")


@pytest.mark.parametrize(
    ("words", "error"),
    [
        (["mon-fri", "23:00"], DAYS_ERROR),
        (["everyday", "23:00"], DAYS_ERROR),
        (["weekdays", "11pm"], TIME_ERROR),
        (["weekdays"], TIME_ERROR),
        (["mon,"] * 10 + ["23:00"], TOO_LONG),
    ],
)
def test_rule_errors_explain_with_an_example(words: list[str], error: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, *words)
    [text] = replies(update)
    assert error in text
    assert "e.g. /schedule_shutdown weekdays 23:00" in text


def test_the_when_prompt_mentions_repeating() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update)
    assert "To repeat: weekdays 23:00." in replies(update)[0]


def test_a_typed_schedule_answers_when() -> None:
    call_with_args(handlers.COMMANDS["schedule_wake"], make_update())
    bad = typed("weekdays 25:00")
    call(handlers.on_text, bad)
    assert TIME_ERROR in replies(bad)[0]  # still listening: they can retry
    good = typed("mon,wed 7:30")
    call(handlers.on_text, good)
    assert "Repeat a wake?" in replies(good)[0]
    assert confirm_data(good) == f"rsched:wake:5:0730:{int(NOW)}"
    later = typed("weekdays 8:00")
    call(handlers.on_text, later)
    assert replies(later) == []  # answered: no longer listening


def test_a_typed_one_shot_time_still_answers_when() -> None:
    call_with_args(handlers.COMMANDS["schedule_wake"], make_update())
    update = typed("45m")
    call(handlers.on_text, update)
    assert "Schedule a wake?" in replies(update)[0]


def test_day_words_without_a_when_prompt_are_ignored() -> None:
    update = typed("weekdays 23:00")
    call(handlers.on_text, update)
    assert replies(update) == []


def test_no_new_schedules_while_they_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedules, "_disabled", True)
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "weekdays", "23:00")
    assert DISABLED in replies(update)[0]
    assert "rsched" not in str(update.effective_message.reply_text.call_args)


# --- Confirming --------------------------------------------------------------------------


def test_confirming_sets_the_schedule() -> None:
    update = set_one()
    [text] = edits(update)
    assert "🔁 <b>Repeating shutdown set</b>" in text
    assert "🔁🔌 Shutdown · weekdays 23:00 · next 23:00." in text
    assert "I'll warn you 5m before each one." in text
    s = only_schedule()
    assert (s.machine, s.action, s.days, s.at, s.chat_id) == (
        FIRST,
        "shutdown",
        (0, 1, 2, 3, 4),
        (23, 0),
        AUTHORIZED_ID,
    )


def test_a_confirmation_works_once() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "weekdays", "23:00")
    data = confirm_data(update)
    _ = press(data, message_id=5)
    again = press(data, message_id=5)
    assert edits(again) == []
    assert len(schedules.pending_schedules()) == 1


def test_an_old_confirmation_expires(frozen: list[float]) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "weekdays", "23:00")
    frozen[0] += config.CONFIRM_TTL_SECONDS + 1
    pressed = press(confirm_data(update))
    assert "Expired" in edits(pressed)[0]
    assert schedules.pending_schedules() == []


@pytest.mark.parametrize(
    "data",
    [
        f"rsched:reboot:31:2300:{int(NOW)}",
        f"rsched:shutdown:0:2300:{int(NOW)}",
        f"rsched:shutdown:128:2300:{int(NOW)}",
        f"rsched:shutdown:0031:2300:{int(NOW)}",
        f"rsched:shutdown:abc:2300:{int(NOW)}",
        f"rsched:shutdown:31:2460:{int(NOW)}",
        f"rsched:shutdown:31:230:{int(NOW)}",
        f"rsched:shutdown:31:2300:{int(NOW) + 3600}",  # "asked" in the future
        "rsched:shutdown:31:2300:soon",
        f"rsched:shutdown:31:2300:{int(NOW)}:nosuchmachine",
        "rsched:",
    ],
)
def test_forged_or_garbled_confirmations_set_nothing(data: str) -> None:
    pressed = press(data)
    [text] = edits(pressed)
    assert "Cancelled" in text or "Expired" in text
    assert schedules.pending_schedules() == []


def test_the_same_schedule_twice_is_refused_at_confirm_time() -> None:
    _ = set_one(message_id=1)
    second = set_one(message_id=2)
    assert "You already have that one." in edits(second)[0]
    assert len(schedules.pending_schedules()) == 1


def test_the_limit_is_checked_at_confirm_time() -> None:
    for n, hour in enumerate(range(4)):
        _ = set_one(f"daily {hour}:00", message_id=n + 1)
    fifth = set_one("daily 5:00", message_id=9)
    assert "There are already 4 repeating shutdowns" in edits(fifth)[0]


def test_the_longest_confirmation_fits_telegrams_64_bytes() -> None:
    longest = "a" + "b" * 14 + "c"
    assert settings.MACHINE_NAME.fullmatch(longest)
    assert not settings.MACHINE_NAME.fullmatch(longest + "d")  # 16 is the most
    data = f"rsched:shutdown:127:2359:{int(NOW) * 10}:{longest}"  # an 11-digit time, too
    assert len(data.encode()) <= 64


# --- /timers -----------------------------------------------------------------------------


def test_timers_with_nothing_set_explains_repeating() -> None:
    text, buttons = timers_card()
    assert "No timers" in text and "To repeat: /schedule_shutdown weekdays 23:00." in text
    assert buttons == ["menu:schedule_shutdown", "menu:schedule_wake"]


def test_timers_lists_one_shots_then_repeating() -> None:
    timers._timers[(FIRST, "wake")] = timers.Timer(  # pyright: ignore[reportPrivateUsage]
        "wake", at(MON, 20), AUTHORIZED_ID
    )
    _ = set_one()
    s = only_schedule()
    text, buttons = timers_card()
    lines = text.splitlines()
    assert lines.index("Repeating:") > next(i for i, x in enumerate(lines) if "Wake at 20:00" in x)
    assert "🔁🔌 Shutdown · weekdays 23:00 · next 23:00" in text
    assert buttons[-4:] == [
        f"rep:skip:{s.id}:20260706",
        f"rep:delete:{s.id}",
        "menu:schedule_shutdown",  # still a way to set another one
        "menu:schedule_wake",
    ]


def test_skip_next_and_dont_skip() -> None:
    _ = set_one()
    s = only_schedule()
    pressed = press(f"rep:skip:{s.id}:20260706")
    [text] = edits(pressed)
    assert "skipping 23:00, next tomorrow 23:00" in text
    unskip = f"rep:unskip:{s.id}"
    assert unskip in [
        b.callback_data
        for row in pressed.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for b in row
    ]
    assert schedules.get_schedule(s.id) == dataclasses.replace(s, skip=MON)
    again = press(unskip)
    assert "· next 23:00" in edits(again)[0]
    assert schedules.get_schedule(s.id) == s


def test_delete() -> None:
    _ = set_one()
    s = only_schedule()
    pressed = press(f"rep:delete:{s.id}")
    assert "No timers" in edits(pressed)[0]
    assert schedules.pending_schedules() == []
    stale = press(f"rep:delete:{s.id}")
    stale.callback_query.answer.assert_awaited_with(repeating.NO_SCHEDULE)


def test_a_stale_skip_button_does_nothing(frozen: list[float]) -> None:
    _ = set_one()
    s = only_schedule()
    frozen[0] = at(TUE, 12)
    schedules._schedules[s.id] = dataclasses.replace(s, last=MON)  # pyright: ignore[reportPrivateUsage]
    pressed = press(f"rep:skip:{s.id}:20260706")
    pressed.callback_query.answer.assert_awaited_with(repeating.ALREADY_HAPPENED)
    skip = schedules.get_schedule(s.id)
    assert skip is not None and skip.skip is None


def test_timers_says_when_schedules_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedules, "_disabled", True)
    text, _ = timers_card()
    assert DISABLED in text


def test_a_stale_one_shot_button_shows_the_whole_list() -> None:
    _ = set_one()
    pressed = press("timer:cancel:shutdown:123")
    assert "Repeating:" in edits(pressed)[0]


# --- The warning's buttons -------------------------------------------------------------------


def test_skip_this_one_from_the_warning() -> None:
    _ = set_one()
    s = only_schedule()
    [skip, later] = schedules.warning_buttons(s, MON)
    pressed = press(str(skip.callback_data))
    [text] = edits(pressed)
    assert "⏭ <b>Skipped</b>" in text and "Next: tomorrow 23:00." in text
    after = schedules.get_schedule(s.id)
    assert after is not None and after.skip == MON
    assert timers.pending_timers() == []
    assert later.text == "⏰ +30m"


def test_plus_30m_from_the_warning_sets_a_one_shot_timer() -> None:
    _ = set_one()
    s = only_schedule()
    later = schedules.warning_buttons(s, MON)[1]
    pressed = press(str(later.callback_data))
    [text] = edits(pressed)
    assert "⏰ <b>Postponed</b>" in text
    assert "Shutdown at 23:30" in text and "this time only" in text
    [timer] = timers.pending_timers()
    assert (timer.action, timer.due, timer.machine) == ("shutdown", at(MON, 23, 30), FIRST)
    after = schedules.get_schedule(s.id)
    assert after is not None and after.skip == MON  # tonight's 23:00 won't run too


@pytest.mark.parametrize("op", ["skipone", "later"])
def test_warning_buttons_only_work_for_a_shutdown(op: str) -> None:
    _ = set_one("weekdays 7:30", "wake")
    s = only_schedule()
    pressed = press(f"rep:{op}:{s.id}:20260707")  # forged: a wake has no warning
    assert edits(pressed) == []
    assert timers.pending_timers() == []  # +30m never makes a shutdown out of a wake
    assert schedules.get_schedule(s.id) == s  # and nothing is skipped


def test_a_warning_button_after_the_run_does_nothing(frozen: list[float]) -> None:
    _ = set_one()
    s = only_schedule()
    [skip, _] = schedules.warning_buttons(s, MON)
    schedules._schedules[s.id] = dataclasses.replace(s, last=MON)  # pyright: ignore[reportPrivateUsage]
    frozen[0] = at(MON, 23, 5)
    pressed = press(str(skip.callback_data))
    pressed.callback_query.answer.assert_awaited_with(repeating.ALREADY_HAPPENED)
    assert edits(pressed) == []
    assert timers.pending_timers() == []


# --- Several machines -----------------------------------------------------------------------


@pytest.fixture
def two_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    nas = dataclasses.replace(config.FIRST_MACHINE, name="nas", ip="192.0.2.20")
    monkeypatch.setattr(config, "MACHINES", (config.FIRST_MACHINE, nas))


@pytest.mark.usefixtures("two_machines")
def test_several_machines_name_the_machine() -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS["schedule_shutdown"], update, "nas", "weekends", "1:00")
    assert "Repeat a shutdown of nas?" in replies(update)[0]
    data = confirm_data(update)
    assert data.endswith(":nas")
    pressed = press(data)
    assert "Shutdown of nas · weekends 01:00" in edits(pressed)[0]
    assert only_schedule().machine == "nas"
    text, buttons = timers_card()
    assert "⏭ Skip next · nas weekends 01:00" in str(buttons) or "Shutdown of nas" in text


@pytest.mark.usefixtures("two_machines")
def test_the_picker_routes_day_words_too() -> None:
    update = make_button_press("pick:schedule_wake:nas")
    call(handlers.on_button, update)  # asks "when?" for nas
    answer = typed("daily 6:45")
    call(handlers.on_text, answer)
    assert "Repeat a wake of nas?" in replies(answer)[0]
