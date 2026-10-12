# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Repeating schedules: the rule, when it runs (DST too), saving, the engine,
missed runs at startup, skips, the overlap with one-shot timers, and limits."""

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from typing import cast

import pytest
from telegram import InlineKeyboardButton

from candygoogy import app as application
from candygoogy import config, schedules, timers
from candygoogy.schedules import (
    DAYS_ERROR,
    DISABLED,
    TIME_ERROR,
    TOO_LONG,
    Schedule,
    ScheduleError,
)
from tests.helpers import AUTHORIZED_ID, STRANGER_ID, fake_app, sent_texts

FIRST = config.FIRST_MACHINE.name
WEEKDAYS = (0, 1, 2, 3, 4)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)


def at(day: date, hour: int, minute: int = 0) -> float:
    """A local wall-clock time. July has no clock change anywhere."""
    return time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))


MON = date(2026, 7, 6)  # a Monday
TUE, WED, THU, FRI = (date(2026, 7, d) for d in (7, 8, 9, 10))
SUN = date(2026, 7, 12)


def schedule(
    days: tuple[int, ...] = WEEKDAYS,
    hour_minute: tuple[int, int] = (23, 0),
    action: str = "shutdown",
    last: date | None = None,
    skip: date | None = None,
    sid: str = "a1b2c3",
    machine: str = FIRST,
    chat_id: int = AUTHORIZED_ID,
) -> Schedule:
    return Schedule(sid, machine, action, days, hour_minute, chat_id, last, skip)


def saved() -> dict[str, object]:
    return json.loads(schedules.SCHEDULES_FILE.read_text())


def write_file(data: object) -> None:
    _ = schedules.SCHEDULES_FILE.write_text(json.dumps(data))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """time.time() reads clock[0]."""
    now = [at(MON, 12)]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[date, float] | None]]:
    """arm() records instead of starting tasks."""
    calls: list[tuple[str, tuple[date, float] | None]] = []

    def record(_app: object, s: Schedule, first: tuple[date, float] | None = None) -> None:
        calls.append((s.id, first))

    monkeypatch.setattr(schedules, "arm", record)
    return calls


# --- The rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "days", "hour_minute"),
    [
        ("daily 23:00", EVERY_DAY, (23, 0)),
        ("weekdays 7:30", WEEKDAYS, (7, 30)),
        ("Weekends 10.00", (5, 6), (10, 0)),
        ("mon,wed,fri 07:30", (0, 2, 4), (7, 30)),
        ("Mon, Fri 23:15", (0, 4), (23, 15)),
        ("monday,FRIDAY  6:05", (0, 4), (6, 5)),
        ("sun,sun,mon 00:00", (0, 6), (0, 0)),
    ],
)
def test_parse_rule(text: str, days: tuple[int, ...], hour_minute: tuple[int, int]) -> None:
    assert schedules.parse_rule(text) == (days, hour_minute)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("mon-fri 23:00", DAYS_ERROR),
        ("everyday 23:00", DAYS_ERROR),
        ("mon,,fri 23:00", DAYS_ERROR),
        ("mon fri 23:00", DAYS_ERROR),  # days need commas
        ("23:00", DAYS_ERROR),
        ("", DAYS_ERROR),
        ("weekdays", TIME_ERROR),
        ("weekdays 11pm", TIME_ERROR),
        ("weekdays 24:00", TIME_ERROR),
        ("weekdays 23:00:00", TIME_ERROR),
        ("weekdays ２３:００", TIME_ERROR),  # fullwidth digits
        ("weekdays ٢٣:00", TIME_ERROR),  # Arabic-Indic digits
        ("mon," * 10 + " 23:00", TOO_LONG),
    ],
)
def test_parse_rule_rejects(text: str, error: str) -> None:
    with pytest.raises(ScheduleError) as e:
        _ = schedules.parse_rule(text)
    assert str(e.value) == error


@pytest.mark.parametrize(
    ("days", "text"),
    [
        (EVERY_DAY, "daily 23:00"),
        (WEEKDAYS, "weekdays 23:00"),
        ((5, 6), "weekends 23:00"),
        ((0, 2, 4), "mon,wed,fri 23:00"),
        ((6,), "sun 23:00"),
    ],
)
def test_rule_text_is_canonical_and_round_trips(days: tuple[int, ...], text: str) -> None:
    assert schedules.rule_text(days, (23, 0)) == text
    assert schedules.parse_rule(text) == (days, (23, 0))
    assert schedules.rule_text(days, (7, 5)).endswith(" 07:05")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("weekdays 23:00", True),
        ("Mon, fri 7:30", True),
        ("sunday 9:00", True),
        ("23:00", False),
        ("45m", False),
        ("monster 23:00", False),
        ("mon-fri 23:00", True),  # refused by parse_rule, with the days' error
        ("everyday 23:00", True),
        ("", False),
    ],
)
def test_starts_with_days(text: str, expected: bool) -> None:
    assert schedules.starts_with_days(text) is expected


# --- When it runs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (at(MON, 12), at(MON, 23)),
        (at(MON, 23, 30), at(TUE, 23)),
        (at(FRI, 23, 30), at(date(2026, 7, 13), 23)),  # past the weekend
    ],
)
def test_next_occurrence_on_weekdays(after: float, expected: float) -> None:
    assert schedules.next_occurrence(schedule(), after) == expected


def test_next_occurrence_across_the_month_end() -> None:
    s = schedule(EVERY_DAY, (8, 0))
    assert schedules.next_occurrence(s, at(date(2026, 7, 31), 9)) == at(date(2026, 8, 1), 8)


def test_next_occurrence_wraps_the_week() -> None:
    s = schedule((6,), (9, 0))
    assert schedules.next_occurrence(s, at(SUN, 10)) == at(date(2026, 7, 19), 9)


def test_last_blocks_a_second_run_that_day() -> None:
    s = schedule(last=MON)
    assert schedules.next_run(s, at(MON, 12)) == (TUE, at(TUE, 23))


def test_a_last_from_weeks_ago_still_finds_the_next_run() -> None:
    s = schedule(last=date(2026, 6, 22))  # 14 days before MON
    assert schedules.next_run(s, at(MON, 12)) == (MON, at(MON, 23))


# --- DST (needs time.tzset, which Windows lacks; CI runs these on Linux) ----------------------

needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is Unix-only")


@pytest.fixture
def zone() -> Iterator[None]:
    """Lets a test switch TZ (with zone_is()); the original comes back after."""
    old = os.environ.get("TZ")
    yield
    if old is None:
        _ = os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def zone_is(name: str) -> None:
    os.environ["TZ"] = name
    time.tzset()


@needs_tzset
@pytest.mark.usefixtures("zone")
@pytest.mark.parametrize(
    ("name", "day", "hour_minute"),
    [
        ("Europe/Berlin", date(2026, 3, 29), (2, 30)),
        ("America/New_York", date(2026, 3, 8), (2, 15)),
    ],
)
def test_spring_forward_runs_once_at_the_gaps_end(
    name: str, day: date, hour_minute: tuple[int, int]
) -> None:
    zone_is(name)
    s = schedule(EVERY_DAY, hour_minute)
    found_day, due = schedules.next_run(s, at(date(day.year, day.month, day.day - 1), 12))
    assert found_day == day
    assert schedules.local_minute(due) == (day, 3, 0)  # 02:xx doesn't exist that night
    s = schedules.handled(s, found_day)
    following = date(day.year, day.month, day.day + 1)
    assert schedules.next_run(s, due + 1) == (following, at(following, *hour_minute))


@needs_tzset
@pytest.mark.usefixtures("zone")
def test_fall_back_runs_once() -> None:
    zone_is("Europe/Berlin")
    day = date(2026, 10, 25)  # 03:00 CEST becomes 02:00 CET: 02:30 happens twice
    s = schedule(EVERY_DAY, (2, 30))
    found_day, due = schedules.next_run(s, at(date(2026, 10, 24), 12))
    assert found_day == day and schedules.local_minute(due) == (day, 2, 30)
    s = schedules.handled(s, found_day)
    for after in (due + 1, due + 3600 + 1):  # after the first 02:30, and the second
        assert schedules.next_run(s, after)[0] == date(2026, 10, 26)


@needs_tzset
@pytest.mark.usefixtures("zone", "armed")
def test_fall_back_restart_between_the_two_runs_doesnt_run_again(clock: list[float]) -> None:
    zone_is("Europe/Berlin")
    day = date(2026, 10, 25)
    s = schedule(EVERY_DAY, (2, 30), last=day)  # ran at the first 02:30, then restarted
    first = schedules.run_time(day, (2, 30))
    clock[0] = max(first, time.mktime((2026, 10, 25, 2, 30, 0, 0, 0, 0))) + 10
    write_file({"version": 1, "schedules": [schedules.to_json(s)]})
    assert schedules.restore_schedules(fake_app()[0]) == []
    assert schedules.next_run(schedules.get_schedule(s.id) or s, clock[0])[0] == date(2026, 10, 26)


@needs_tzset
@pytest.mark.usefixtures("zone")
def test_a_time_zone_change_between_restarts_follows_the_rule() -> None:
    s = schedule(EVERY_DAY, (23, 0))
    zone_is("Europe/Berlin")
    after = time.mktime((2026, 7, 6, 12, 0, 0, 0, 0, -1))
    berlin = schedules.next_occurrence(s, after)
    zone_is("America/New_York")
    new_york = schedules.next_occurrence(s, after)
    assert new_york - berlin == 6 * 3600  # 23:00 there, not Berlin's 23:00
    assert schedules.local_minute(new_york)[1:] == (23, 0)


# --- Saving and loading ----------------------------------------------------------------------


@pytest.mark.usefixtures("armed")
def test_no_file_means_no_schedules() -> None:
    assert schedules.restore_schedules(fake_app()[0]) == []
    assert schedules.pending_schedules() == []
    assert not schedules.SCHEDULES_FILE.exists()
    assert schedules.schedules_disabled_reason() is None


@pytest.mark.usefixtures("armed", "clock")
def test_a_v1_1_timers_file_is_untouched() -> None:
    old = {FIRST: {"wake": {"due": at(MON, 20), "chat_id": AUTHORIZED_ID}}}
    text = json.dumps(old)
    _ = timers.TIMERS_FILE.write_text(text)
    write_file({"version": 1, "schedules": [schedules.to_json(schedule(last=MON))]})
    _ = schedules.restore_schedules(fake_app()[0])
    assert timers.TIMERS_FILE.read_text() == text
    [timer] = timers.load_timers()
    assert (timer.action, timer.due) == ("wake", at(MON, 20))
    assert [s.id for s, _ in schedules.pending_schedules()] == ["a1b2c3"]


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
@pytest.mark.usefixtures("armed", "clock")
def test_an_unknown_version_is_read_only(version: object) -> None:
    data = {"version": version, "schedules": [schedules.to_json(schedule())]}
    if version is None:
        del data["version"]
    write_file(data)
    text = schedules.SCHEDULES_FILE.read_text()
    assert schedules.restore_schedules(fake_app()[0]) == [DISABLED]
    assert schedules.schedules_disabled_reason() == DISABLED
    assert schedules.pending_schedules() == []
    with pytest.raises(ScheduleError) as e:
        _ = schedules.add_schedule(fake_app()[0], FIRST, "wake", WEEKDAYS, (7, 0), AUTHORIZED_ID)
    assert str(e.value) == DISABLED
    assert schedules.save_schedules() is False
    assert schedules.SCHEDULES_FILE.read_text() == text  # never written


def test_the_startup_report_says_schedules_are_off() -> None:
    report = application.startup_report([], [], schedule_lines=[DISABLED])
    assert report is not None and DISABLED in report


@pytest.mark.usefixtures("armed", "clock")
def test_malformed_entries_and_duplicate_ids_are_dropped_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = schedules.to_json(schedule(last=MON))
    bad: list[object] = [
        {**good, "id": "XYZ"},
        {**good, "action": "reboot"},
        {**good, "days": []},
        {**good, "days": [7]},
        {**good, "days": [True]},
        {**good, "at": "25:00"},
        {**good, "chat_id": "1111"},
        {**good, "last": "yesterday"},
        {**good, "skip": 5},
        "not an object",
    ]
    twin = {**good, "action": "wake"}  # the same ID again
    write_file({"version": 1, "schedules": [good, *bad, twin]})
    assert schedules.restore_schedules(fake_app()[0]) == []
    assert [s.id for s, _ in schedules.pending_schedules()] == ["a1b2c3"]
    assert schedules.get_schedule("a1b2c3") == schedule(last=MON)
    assert caplog.text.count("Ignoring a malformed schedule") == len(bad)
    assert "Ignoring a second schedule with the ID a1b2c3" in caplog.text
    assert saved() == {"version": 1, "schedules": [good]}  # saved once, cleaned


def test_saved_shape() -> None:
    s = schedule((0, 4), (7, 5), "wake", last=MON, skip=FRI)
    assert schedules.to_json(s) == {
        "id": "a1b2c3",
        "machine": FIRST,
        "action": "wake",
        "days": [0, 4],
        "at": "07:05",
        "chat_id": AUTHORIZED_ID,
        "last": "2026-07-06",
        "skip": "2026-07-10",
    }
    assert schedules.from_json(schedules.to_json(s)) == s


# --- The engine ------------------------------------------------------------------------------


class Engine:
    """Runs a schedule's task with instant sleeps that move the clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
        self.clock: list[float] = clock
        self.slept: list[float] = []
        self.warned: list[tuple[timers.Timer, list[str]]] = []
        self.ran: list[tuple[timers.Timer, str, dict[str, object]]] = []
        self.jump: float = 0  # how far past each wake-up the clock lands
        self.app: object = fake_app()[0]
        self.sleeps: int = 0

        async def sleep_until(when: float) -> None:
            if len(self.slept) >= self.sleeps:
                raise asyncio.CancelledError  # ends the test's run
            self.slept.append(when)
            self.clock[0] = max(self.clock[0], when + self.jump)

        async def warn(
            _app: object, timer: timers.Timer, buttons: list[InlineKeyboardButton] | None = None
        ) -> None:
            assert buttons is not None  # never the one-shot's Cancel/+30m
            self.warned.append((timer, [cast(str, b.callback_data) for b in buttons]))

        async def run(_app: object, timer: timers.Timer, intro: str) -> None:
            self.ran.append((timer, intro, saved()))  # the file as the action starts

        monkeypatch.setattr(timers, "sleep_until", sleep_until)
        monkeypatch.setattr(timers, "warn_before_shutdown", warn)
        monkeypatch.setattr(timers, "run_timed_action", run)

    def run(self, s: Schedule, sleeps: int, first: tuple[date, float] | None = None) -> None:
        schedules._schedules[s.id] = s  # pyright: ignore[reportPrivateUsage]
        self.sleeps = sleeps

        async def main() -> None:
            try:
                await schedules.run_schedule(self.app, s.id, first)  # pyright: ignore[reportArgumentType]
            except asyncio.CancelledError:
                pass

        asyncio.run(main())


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> Engine:
    return Engine(monkeypatch, clock)


def test_a_shutdown_warns_then_runs_with_last_saved_first(engine: Engine) -> None:
    s = schedule()
    engine.run(s, sleeps=2)
    assert engine.slept == [at(MON, 23) - timers.TIMER_WARNING_SECONDS, at(MON, 23)]
    [(warned, buttons)] = engine.warned
    assert (warned.action, warned.due, warned.machine) == ("shutdown", at(MON, 23), FIRST)
    assert buttons == ["rep:skipone:a1b2c3:20260706", "rep:later:a1b2c3:20260706"]
    [(timer, intro, file)] = engine.ran
    assert (timer.action, timer.due, timer.chat_id) == ("shutdown", at(MON, 23), AUTHORIZED_ID)
    assert "Repeating: weekdays 23:00." in intro
    assert file["schedules"] == [schedules.to_json(replace(s, last=MON))]  # saved before


def test_runs_continue_the_next_day(engine: Engine) -> None:
    engine.run(schedule(), sleeps=4)
    assert [t.due for t, _, _ in engine.ran] == [at(MON, 23), at(TUE, 23)]


def test_a_wake_has_no_warning(engine: Engine) -> None:
    engine.run(schedule(action="wake", hour_minute=(7, 30)), sleeps=1)
    assert engine.slept == [at(TUE, 7, 30)]
    assert engine.warned == []
    assert [t.action for t, _, _ in engine.ran] == ["wake"]


def test_a_skip_consumes_the_run_silently(engine: Engine) -> None:
    engine.run(schedule(skip=MON), sleeps=4)
    assert [t.due for t, _ in engine.warned] == [at(TUE, 23)]  # none before the skipped run
    assert [t.due for t, _, _ in engine.ran] == [at(TUE, 23)]
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.skip is None and s.last == TUE
    assert sent_texts(engine.app) == []  # pyright: ignore[reportArgumentType]


def test_a_stale_skip_does_nothing(engine: Engine) -> None:
    engine.run(schedule(last=date(2026, 7, 3), skip=date(2026, 7, 3)), sleeps=2)
    assert [t.due for t, _, _ in engine.ran] == [at(MON, 23)]
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.skip is None


@pytest.mark.parametrize(("gap", "replaced"), [(600, True), (-600, True), (601, False)])
def test_a_one_shot_timer_close_by_replaces_the_run(
    engine: Engine, caplog: pytest.LogCaptureFixture, gap: int, replaced: bool
) -> None:
    caplog.set_level(logging.INFO)
    _ = timers.Timer("shutdown", at(MON, 23) + gap, AUTHORIZED_ID)
    timers._timers[(FIRST, "shutdown")] = timers.Timer(  # pyright: ignore[reportPrivateUsage]
        "shutdown", at(MON, 23) + gap, AUTHORIZED_ID
    )
    engine.run(schedule(), sleeps=2)
    assert (engine.ran == []) is replaced
    assert (engine.warned == []) is replaced
    assert ("event=schedule_deferred" in caplog.text) is replaced
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.last == MON  # handled either way, never "missed"
    assert "Missed" not in "".join(sent_texts(engine.app))  # pyright: ignore[reportArgumentType]


def test_a_one_shot_for_the_other_action_never_interacts(engine: Engine) -> None:
    timers._timers[(FIRST, "wake")] = timers.Timer(  # pyright: ignore[reportPrivateUsage]
        "wake", at(MON, 23), AUTHORIZED_ID
    )
    engine.run(schedule(), sleeps=2)
    assert len(engine.ran) == 1


def test_a_clock_jump_past_the_grace_is_a_miss(engine: Engine) -> None:
    engine.jump = timers.MISSED_GRACE_SECONDS + 60
    engine.run(schedule(action="wake", hour_minute=(7, 30)), sleeps=1)
    assert engine.ran == []
    [text] = sent_texts(engine.app)  # pyright: ignore[reportArgumentType]
    assert "Missed the repeating wake (weekdays 07:30)" in text
    assert "The clock jumped past it." in text


def test_a_failure_doesnt_stop_the_schedule(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(schedules, "RETRY_SECONDS", 0)
    calls: list[float] = []

    async def flaky(_app: object, timer: timers.Timer, _intro: str) -> None:
        calls.append(timer.due)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(timers, "run_timed_action", flaky)
    engine.run(schedule(action="wake", hour_minute=(7, 30)), sleeps=2)
    assert calls == [at(TUE, 7, 30), at(WED, 7, 30)]  # Tuesday's failed; Wednesday still ran
    assert "Repeating schedule a1b2c3 failed; trying again" in caplog.text


def test_a_run_due_at_startup_runs_first(engine: Engine) -> None:
    engine.clock[0] = at(MON, 23, 1)
    engine.run(schedule(last=date(2026, 7, 3)), sleeps=0, first=(MON, at(MON, 23)))
    assert [t.due for t, _, _ in engine.ran] == [at(MON, 23)]


# --- Startup: missed runs, dropped schedules -------------------------------------------------


def restore(s: Schedule) -> list[str]:
    write_file({"version": 1, "schedules": [schedules.to_json(s)]})
    return schedules.restore_schedules(fake_app()[0])


@pytest.mark.usefixtures("armed")
def test_startup_runs_a_run_within_the_grace(
    clock: list[float], armed: list[tuple[str, tuple[date, float] | None]]
) -> None:
    clock[0] = at(MON, 23) + timers.MISSED_GRACE_SECONDS
    assert restore(schedule(last=date(2026, 7, 3))) == []
    assert armed == [("a1b2c3", (MON, at(MON, 23)))]
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.last == MON and saved()["schedules"] == [schedules.to_json(s)]


def test_startup_reports_missed_runs_once(
    clock: list[float],
    armed: list[tuple[str, tuple[date, float] | None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    clock[0] = at(THU, 23, 10)  # Tue, Wed and Thu at 23:00 were missed
    [line] = restore(schedule(last=MON))
    assert line.startswith("⚠️ Missed the repeating shutdown (weekdays 23:00) at 23:00")
    assert line.endswith(" (and 2 earlier). Nothing was done.")
    assert armed == [("a1b2c3", None)]  # nothing runs late
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.last == THU
    assert caplog.text.count("event=schedule_missed") == 3


@pytest.mark.usefixtures("armed")
def test_startup_counts_misses_only_7_days_back(clock: list[float]) -> None:
    clock[0] = at(THU, 12)
    [line] = restore(schedule(last=date(2026, 6, 15)))
    assert "(and earlier ones)" in line
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.last == WED
    assert schedules.next_run(s, clock[0]) == (THU, at(THU, 23))


@pytest.mark.usefixtures("armed")
def test_startup_never_reports_a_skipped_run(clock: list[float]) -> None:
    clock[0] = at(TUE, 12)
    assert restore(schedule(last=date(2026, 7, 3), skip=MON)) == []
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.skip is None and s.last == MON


@pytest.mark.usefixtures("armed")
def test_startup_without_last_reports_nothing(clock: list[float]) -> None:
    clock[0] = at(THU, 12)
    assert restore(schedule(last=None)) == []
    s = schedules.get_schedule("a1b2c3")
    assert s is not None and s.last == WED


@pytest.mark.usefixtures("armed", "clock")
def test_startup_drops_a_removed_machine_and_reports_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    [line] = restore(schedule(machine="gone", last=MON))
    assert line == (
        "⚠️ Dropped the repeating shutdown of gone (weekdays 23:00): "
        + "that machine isn't configured any more."
    )
    assert schedules.pending_schedules() == []
    assert saved()["schedules"] == []
    assert "reason=machine_not_configured" in caplog.text


@pytest.mark.usefixtures("armed", "clock")
def test_startup_drops_a_removed_setter_and_reports_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    [line] = restore(schedule(chat_id=STRANGER_ID, last=MON))
    assert line == (
        "⚠️ Dropped the repeating shutdown of workstation (weekdays 23:00): "
        + "whoever set it is no longer allowed."
    )
    assert schedules.pending_schedules() == []
    assert "reason=chat_not_allowed" in caplog.text
    assert str(STRANGER_ID) not in caplog.text


@pytest.mark.usefixtures("armed")
def test_the_startup_report_lists_the_schedules(clock: list[float]) -> None:
    clock[0] = at(THU, 23, 10)
    lines = restore(schedule(last=MON))
    report = application.startup_report([], [], schedule_lines=lines)
    assert report is not None
    assert "Missed the repeating shutdown" in report
    assert "🔁 Shutdown · weekdays 23:00 · next tomorrow 23:00" in report


# --- Setting, skipping, deleting -----------------------------------------------------------


def add(
    days: tuple[int, ...] = WEEKDAYS, hour_minute: tuple[int, int] = (23, 0), **kw: str
) -> Schedule:
    machine = kw.get("machine", FIRST)
    action = kw.get("action", "shutdown")
    s, ok = schedules.add_schedule(fake_app()[0], machine, action, days, hour_minute, AUTHORIZED_ID)
    assert ok
    return s


@pytest.mark.usefixtures("armed")
def test_a_new_schedule_starts_after_now(clock: list[float]) -> None:
    clock[0] = at(WED, 12)
    s = add()
    assert s.last == TUE  # Mon and Tue weren't "missed": it didn't exist yet
    assert schedules.next_run(s, clock[0]) == (WED, at(WED, 23))
    assert saved()["schedules"] == [schedules.to_json(s)]


@pytest.mark.usefixtures("armed", "clock")
def test_limits_per_machine_and_action() -> None:
    for hour in range(schedules.MAX_PER_MACHINE_ACTION):
        _ = add(hour_minute=(hour, 0))
    with pytest.raises(ScheduleError) as e:
        _ = add(hour_minute=(20, 0))
    assert "There are already 4 repeating shutdowns" in str(e.value)
    _ = add(hour_minute=(20, 0), action="wake")  # another action is fine


@pytest.mark.usefixtures("armed", "clock")
def test_limit_in_total() -> None:
    for n in range(schedules.MAX_SCHEDULES):
        _ = add(hour_minute=(n % 4, 0), machine=f"m{n // 4}")
    with pytest.raises(ScheduleError) as e:
        _ = add(machine="another")
    assert "There are already 20 repeating schedules" in str(e.value)


@pytest.mark.usefixtures("armed", "clock")
def test_the_same_schedule_twice_is_refused() -> None:
    _ = add()
    with pytest.raises(ScheduleError) as e:
        _ = add()
    assert str(e.value) == "You already have that one."


@pytest.mark.usefixtures("armed", "clock")
def test_an_id_collision_is_regenerated(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["aaaaaa", "aaaaaa", "bbbbbb"])

    def token_hex(_n: int) -> str:
        return next(ids)

    monkeypatch.setattr(secrets, "token_hex", token_hex)
    assert add().id == "aaaaaa"
    assert add(hour_minute=(22, 0)).id == "bbbbbb"


@pytest.mark.usefixtures("clock")
def test_skip_and_unskip(armed: list[tuple[str, tuple[date, float] | None]]) -> None:
    s = add()
    app = fake_app()[0]
    assert schedules.set_skip(app, s.id) == (MON, True)
    assert schedules.get_schedule(s.id) == replace(s, skip=MON)
    assert schedules.clear_skip(app, s.id) == (True, True)
    assert schedules.clear_skip(app, s.id) == (False, True)  # nothing to undo
    assert schedules.set_skip(app, "ffffff") == (None, True)
    assert len(armed) == 3  # set, skip and unskip each re-arm


@pytest.mark.usefixtures("armed", "clock")
def test_delete() -> None:
    s = add()
    assert schedules.delete_schedule(s.id) == (s, True)
    assert schedules.delete_schedule(s.id) == (None, True)
    assert saved()["schedules"] == []


@pytest.mark.usefixtures("armed", "clock")
def test_audit_lines_name_the_rule_but_never_the_chat(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    s = add()
    _ = schedules.set_skip(fake_app()[0], s.id)
    _ = schedules.clear_skip(fake_app()[0], s.id)
    _ = schedules.delete_schedule(s.id)
    for event in ("set", "skipped", "unskipped", "deleted"):
        assert f'event=schedule_{event} machine={FIRST} rule="weekdays 23:00"' in caplog.text
    assert str(AUTHORIZED_ID) not in caplog.text


def test_the_real_arm_starts_and_replaces_a_task(clock: list[float]) -> None:
    clock[0] = at(MON, 12)

    async def main() -> None:
        s, _ = schedules.add_schedule(fake_app()[0], FIRST, "wake", WEEKDAYS, (7, 0), AUTHORIZED_ID)
        first = schedules._tasks[s.id]  # pyright: ignore[reportPrivateUsage]
        _ = schedules.set_skip(fake_app()[0], s.id)
        await asyncio.sleep(0)
        assert first.cancelled()
        await schedules.stop_schedules(fake_app()[0])
        assert schedules._tasks == {}  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())
