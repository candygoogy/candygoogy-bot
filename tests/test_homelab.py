# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""/homelab and its alerts."""

import asyncio
import json
import logging
import shutil
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram.error import TelegramError

from candygoogy import app as application
from candygoogy import (
    handlers,
    homelab,
    keyboard,
    system,
)
from tests.helpers import (
    AUTHORIZED_ID,
    GIB,
    STRANGER_ID,
    FakeHomelab,
    button_data,
    call,
    container,
    edits,
    fake_app,
    make_button_press,
    make_update,
    replies,
    sent_texts,
)

# --- /homelab ------------------------------------------------------------------


def homelab_card() -> str:
    update = make_update()
    call(handlers.homelab, update)
    [text] = replies(update)
    return text


def test_homelab_when_all_is_well(lab: FakeHomelab) -> None:
    text = homelab_card()
    assert "Homelab" in text
    assert "Up 6d 4h · load 0.19 0.76 1.39" in text
    assert "RAM 8.0 GB / 32 GB" in text
    assert "<code>/</code> 150 GB free · <code>/srv/data</code> 300 GB free" in text
    assert "✅ No failed services." in text
    assert "💾 Last backup 7h ago (10 MB)." in text
    assert "🐳 All 3 containers running." in text
    assert "📦 No updates waiting." in text
    assert "⚠️" not in text
    assert "Checked" in text
    # Read-only: systemctl and apt only list things, and Docker gets one GET.
    assert sorted(lab.run.calls) == [
        ("apt", "list", "--upgradable"),
        ("systemctl", "list-units", "--state=failed", "--no-pager", "--output=json"),
    ]
    assert [(r.method, str(r.url)) for r in lab.docker_requests] == [
        ("GET", "http://docker/containers/json?all=1")
    ]


@pytest.mark.usefixtures("lab")
def test_homelab_has_refresh_button() -> None:
    update = make_update()
    call(handlers.homelab, update)
    assert button_data(update) == ["homelab"]


@pytest.mark.usefixtures("lab")
def test_homelab_refresh_edits_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "REFRESH_COOLDOWN_SECONDS", 0)
    for _ in range(2):
        update = make_button_press("homelab", message_id=8)
        call(handlers.on_button, update)
        [text] = edits(update)
        assert "Homelab" in text
        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        assert markup is handlers.HOMELAB_REFRESH


@pytest.mark.usefixtures("lab")
def test_homelab_menu_button() -> None:
    assert "homelab" in handlers.MENU_COMMANDS
    update = make_button_press("menu:homelab")
    call(handlers.on_button, update)
    assert "Homelab" in replies(update)[0]


def test_homelab_warns_when_a_disk_is_almost_full(lab: FakeHomelab) -> None:
    lab.disks["/srv/data"] = (12 * GIB, 480 * GIB)
    text = homelab_card()
    assert "⚠️ <code>/srv/data</code> is almost full. 12 GB free of 480 GB." in text
    assert "<code>/srv/data</code> 12 GB free" not in text  # said once, as the warning
    assert "<code>/</code> 150 GB free" in text


def test_homelab_warns_when_data_disk_is_not_mounted(lab: FakeHomelab) -> None:
    lab.unmounted.add("/srv/data")
    text = homelab_card()
    assert "⚠️ <code>/srv/data</code> is not mounted." in text
    assert "<code>/srv/data</code> 300 GB free" not in text  # that would be the root disk


def test_homelab_lists_failed_services(lab: FakeHomelab) -> None:
    lab.failed("example.service", "<odd>.service")
    text = homelab_card()
    assert "⚠️ <code>example.service</code> failed." in text
    assert "<code>&lt;odd&gt;.service</code>" in text
    assert "No failed services" not in text


@pytest.mark.parametrize(
    "result",
    [
        system.RunResult(1, "Failed to connect to bus"),
        system.RunResult(0, out="not json"),
        system.RunResult(0, out='{"unit": "x"}'),
    ],
)
def test_homelab_says_when_services_are_unknown(lab: FakeHomelab, result: system.RunResult) -> None:
    lab.run.results["systemctl"] = result
    text = homelab_card()
    assert "Couldn't check services." in text
    assert "No failed services" not in text


def test_homelab_warns_about_overdue_backups(lab: FakeHomelab) -> None:
    shutil.rmtree(lab.backups / "20261005-021500")
    _ = lab.snapshot("20261002-021500", age_hours=74)
    text = homelab_card()
    assert "⚠️ Backups are overdue. Last completed backup 3d 2h ago (20261002-021500)." in text
    assert "💾" not in text


def test_homelab_without_any_completed_backup(lab: FakeHomelab) -> None:
    shutil.rmtree(lab.backups / "20261005-021500")
    _ = lab.snapshot("manual-20261004")  # made by hand, not by the backup job
    _ = lab.snapshot("20261006-021500", complete=False)
    text = homelab_card()
    assert "⚠️ No completed backup." in text
    assert "20261006-021500 didn't finish" in text


def test_homelab_flags_a_newer_unfinished_backup(lab: FakeHomelab) -> None:
    _ = lab.snapshot("20261006-021500", complete=False)
    text = homelab_card()
    assert "💾 Last backup 7h ago (10 MB)." in text
    assert "<i>20261006-021500 didn't finish (failed, or still running).</i>" in text


def test_homelab_unreadable_backup_folder(
    lab: FakeHomelab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(homelab, "BACKUP_DIR", lab.backups / "missing")
    text = homelab_card()
    assert "⚠️ Can't read the backups folder." in text


def test_homelab_survives_unreadable_proc(lab: FakeHomelab) -> None:
    for name in ("uptime", "loadavg", "meminfo"):
        (lab.proc / name).unlink()
    lab.disks["/"] = None
    text = homelab_card()
    assert "Up " not in text and "RAM" not in text
    assert "<code>/</code>" not in text
    assert "<code>/srv/data</code> 300 GB free" in text


@pytest.mark.usefixtures("fake_run")
def test_homelab_is_listed_in_help() -> None:
    update = make_update()
    call(handlers.start, update)
    assert "/homelab" in replies(update)[0]


# --- homelab alerts --------------------------------------------------------------


def run_check(app: MagicMock) -> None:
    asyncio.run(homelab.check_and_alert(app))


@pytest.mark.usefixtures("lab")
def test_healthy_homelab_sends_nothing() -> None:
    app, _ = fake_app()
    run_check(app)
    assert sent_texts(app) == []


def test_alert_once_then_resolved(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.failed("example.service")
    run_check(app)
    run_check(app)  # still failed: no repeat
    [alert] = sent_texts(app)
    assert alert.startswith("⚠️ <b>Homelab: <code>example.service</code> failed</b>")
    assert app.bot.send_message.call_args.args[0] == AUTHORIZED_ID
    # Alerts carry the keyboard, which keeps it under the message box.
    assert app.bot.send_message.call_args.kwargs["reply_markup"] is keyboard.KEYBOARD

    lab.failed()
    run_check(app)
    run_check(app)
    assert sent_texts(app)[1:] == [
        "✅ <b>Homelab: back to normal</b>\nWas: <code>example.service</code> failed."
    ]


def test_each_problem_alerts_separately(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.disks["/"] = (5 * GIB, 250 * GIB)
    lab.failed("a.service")
    run_check(app)
    assert len(sent_texts(app)) == 2
    lab.failed("a.service", "b.service")
    run_check(app)
    assert len(sent_texts(app)) == 3
    assert "b.service" in sent_texts(app)[2]


def test_open_alerts_survive_a_restart(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.failed("example.service")
    run_check(app)
    homelab._open_alerts.clear()  # pyright: ignore[reportPrivateUsage]
    homelab.load_alerts()
    run_check(app)
    assert len(sent_texts(app)) == 1  # not repeated after the restart
    lab.failed()
    homelab._open_alerts.clear()  # pyright: ignore[reportPrivateUsage]
    homelab.load_alerts()
    run_check(app)
    assert "back to normal" in sent_texts(app)[-1]  # fixed while the bot was down


def test_alerts_are_kept_for_the_weekly_report(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.failed("example.service")
    run_check(app)
    run_check(app)
    [incident] = homelab.incidents_since(0)
    assert (incident.key, incident.title, incident.end) == (
        "unit:example.service",
        "<code>example.service</code> failed",
        None,
    )
    lab.failed()
    run_check(app)
    homelab._incidents.clear()  # pyright: ignore[reportPrivateUsage]
    homelab.load_incidents()  # as after a restart
    [incident] = homelab.incidents_since(0)
    assert incident.end is not None and incident.end >= incident.start
    assert homelab.incidents_since(incident.end + 1) == []


def test_old_incidents_are_forgotten(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    old = time.time() - homelab.INCIDENTS_KEPT_SECONDS - 60
    homelab._incidents.append(homelab.Incident("x", "x", old - 60, old))  # pyright: ignore[reportPrivateUsage]
    lab.failed("example.service")
    run_check(app)
    assert [i.key for i in homelab.incidents_since(0)] == ["unit:example.service"]


@pytest.mark.parametrize(
    "content", ["not json", "{}", '[["k", "t", 1]]', '[["k", "t", "1", null]]']
)
def test_bad_incidents_file_is_ignored(caplog: pytest.LogCaptureFixture, content: str) -> None:
    _ = homelab.INCIDENTS_FILE.write_text(content)
    homelab.load_incidents()
    assert homelab.incidents_since(0) == []
    assert "Ignoring" in caplog.text


def test_unknown_state_does_not_resolve(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.failed("example.service")
    lab.disks["/"] = (5 * GIB, 250 * GIB)
    run_check(app)
    lab.run.results["systemctl"] = system.RunResult(1, "Failed to connect to bus")
    lab.disks["/"] = None
    run_check(app)
    assert len(sent_texts(app)) == 2  # the two alerts, no "back to normal"


def test_failed_alert_is_retried(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.failed("example.service")
    app.bot.send_message.side_effect = TelegramError("network down")
    run_check(app)
    assert homelab._open_alerts == {}  # pyright: ignore[reportPrivateUsage]
    app.bot.send_message.side_effect = None
    run_check(app)
    assert app.bot.send_message.await_count == 2
    assert "unit:example.service" in homelab._open_alerts  # pyright: ignore[reportPrivateUsage]


def test_overdue_backup_alert(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    shutil.rmtree(lab.backups / "20261005-021500")
    _ = lab.snapshot("20261004-021500", age_hours=30)
    run_check(app)
    [alert] = sent_texts(app)
    assert "Backups are overdue" in alert
    _ = lab.snapshot("20261005-021500", age_hours=1)
    run_check(app)
    assert sent_texts(app)[-1].endswith("Was: Backups are overdue.")


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"a": 3}'])
def test_bad_alerts_file_is_ignored(caplog: pytest.LogCaptureFixture, content: str) -> None:
    _ = homelab.ALERTS_FILE.write_text(content)
    homelab.load_alerts()
    assert homelab._open_alerts == {}  # pyright: ignore[reportPrivateUsage]
    assert "Ignoring" in caplog.text


def test_watch_keeps_going_after_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    checks: list[int] = []

    async def flaky(_app: object) -> None:
        checks.append(1)
        if len(checks) == 1:
            raise RuntimeError("boom")
        if len(checks) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(homelab, "check_and_alert", flaky)
    monkeypatch.setattr(homelab, "FIRST_CHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(homelab, "CHECK_INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(homelab.watch(MagicMock()))
    assert len(checks) == 3


@pytest.mark.usefixtures("fake_run")
def test_startup_starts_and_stop_cancels_the_watch() -> None:
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(return_value=[])

    async def main() -> None:
        await application.on_startup(app)
        tasks = list(application._background)  # pyright: ignore[reportPrivateUsage]
        assert len(tasks) == len(application.BACKGROUND)
        assert not any(t.done() for t in tasks)
        await application.on_stop(app)
        await asyncio.sleep(0)
        assert all(t.cancelled() for t in tasks)

    asyncio.run(main())


# --- /homelab: containers, updates, reboot ------------------------------------------


def test_homelab_warns_about_a_stopped_container(lab: FakeHomelab) -> None:
    lab.containers(
        container("web"),
        container("app", "exited", status="Exited (1) 5 minutes ago"),
        container("<odd>", "created", status="Created"),
    )
    text = homelab_card()
    assert "🐳 1 of 3 containers running." in text
    assert (
        "⚠️ <code>app</code> is stopped. Exited (1) 5 minutes ago. "
        + "See <code>docker logs app</code>."
    ) in text
    assert "⚠️ <code>&lt;odd&gt;</code> is not started." in text


@pytest.mark.parametrize(
    ("c", "expected"),
    [
        (container("svc", health="unhealthy", status="Up 2h (unhealthy)"), "is unhealthy"),
        # Older Engines have no Health field; Status says it.
        (container("svc", status="Up 2h (unhealthy)"), "is unhealthy"),
        (container("svc", "restarting", status="Restarting (1) 4s ago"), "keeps restarting"),
        (container("svc", "paused", status="Up 2h (Paused)"), "is paused"),
    ],
)
def test_homelab_container_problems(lab: FakeHomelab, c: dict[str, object], expected: str) -> None:
    lab.containers(container("web"), c)
    assert f"⚠️ <code>svc</code> {expected}." in homelab_card()


def test_homelab_healthy_and_starting_containers_are_fine(lab: FakeHomelab) -> None:
    lab.containers(
        container("a", health="healthy"),
        container("b", health="starting"),
        container("c", status="Up 1m (health: starting)"),
        container("d", status="Up 1h (healthy)"),
    )
    text = homelab_card()
    assert "🐳 All 4 containers running." in text
    assert "⚠️" not in text


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, text="Forbidden"),
        httpx.Response(500, json={"message": "server error"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"message": "not a list"}),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json=[{"Names": "/x", "State": "running"}]),
        httpx.Response(200, json=[{"Names": [], "State": "running"}]),
        httpx.Response(200, json=[{"Names": ["/x"]}]),
        httpx.ReadTimeout("slow"),
    ],
    ids=lambda a: type(a).__name__ if isinstance(a, Exception) else str(a.status_code),
)
def test_homelab_says_when_containers_are_unknown(
    lab: FakeHomelab, answer: httpx.Response | Exception
) -> None:
    lab.docker_answer = answer
    assert "<i>Couldn't check containers.</i>" in homelab_card()


def test_homelab_says_when_docker_isnt_reachable(lab: FakeHomelab) -> None:
    """No socket, no permission or refused: httpx raises ConnectError for each."""
    lab.docker_answer = httpx.ConnectError("[Errno 2] No such file or directory")
    text = homelab_card()
    assert "<i>Docker isn't reachable, so containers aren't checked.</i>" in text
    assert "Couldn't check containers" not in text
    assert "⚠️" not in text


def test_homelab_without_containers(lab: FakeHomelab) -> None:
    lab.containers()
    assert "🐳 No containers." in homelab_card()


@pytest.mark.parametrize(
    ("packages", "expected"),
    [
        (["example-pkg"], "📦 1 update available: example-pkg."),
        (["a", "b<c>", "d", "e", "f"], "📦 5 updates available: a, b&lt;c&gt;, d and 2 more."),
    ],
)
def test_homelab_lists_updates(lab: FakeHomelab, packages: list[str], expected: str) -> None:
    lab.updates(*packages)
    text = homelab_card()
    assert expected in text
    assert "⚠️" not in text  # updates alone aren't a problem


def test_homelab_says_when_updates_are_unknown(lab: FakeHomelab) -> None:
    lab.run.results["apt"] = system.RunResult(100, "E: something")
    assert "<i>Couldn't check for updates.</i>" in homelab_card()


def test_homelab_warns_when_a_reboot_is_required(lab: FakeHomelab) -> None:
    lab.reboot_required("linux-image-amd64", "libc6")
    text = homelab_card()
    assert "⚠️ Reboot required. Needed by libc6, linux-image-amd64." in text


def test_homelab_reboot_required_without_package_list(lab: FakeHomelab) -> None:
    lab.reboot_required(listed=False)
    text = homelab_card()
    assert "⚠️ Reboot required. An update installed since the last boot needs it." in text


def test_container_alert_and_recovery(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.containers(container("web"), container("app", "exited", status="Exited (1) 1m ago"))
    run_check(app)
    [alert] = sent_texts(app)
    assert alert.startswith("⚠️ <b>Homelab: <code>app</code> is stopped</b>")
    lab.docker_answer = httpx.ConnectError("Connection refused")
    run_check(app)  # unknown: no "back to normal"
    assert len(sent_texts(app)) == 1
    lab.containers(container("web"), container("app"))
    run_check(app)
    assert sent_texts(app)[-1].endswith("Was: <code>app</code> is stopped.")


def test_reboot_alert_once_until_rebooted(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.reboot_required("linux-image-amd64")
    run_check(app)
    run_check(app)
    [alert] = sent_texts(app)
    assert "Homelab: Reboot required" in alert
    assert "Needed by linux-image-amd64." in alert
    (lab.run_dir / "reboot-required").unlink()
    run_check(app)
    assert sent_texts(app)[-1].endswith("Was: Reboot required.")


# --- Reboot homelab ------------------------------------------------------------------

REBOOT = ("systemctl", "reboot", "--no-ask-password")


def confirm_reboot(issued: float | None = None, message_id: int = 1) -> MagicMock:
    stamp = int(time.time() if issued is None else issued)
    update = make_button_press(f"confirm:homelab_reboot:{stamp}", message_id=message_id)
    call(handlers.on_button, update)
    return update


def test_homelab_offers_a_reboot_only_when_needed(lab: FakeHomelab) -> None:
    update = make_update()
    call(handlers.homelab, update)
    assert button_data(update) == ["homelab"]
    lab.reboot_required("linux-image-amd64")
    update = make_update()
    call(handlers.homelab, update)
    assert button_data(update) == ["homelab", "ask:homelab_reboot"]


def test_homelab_refresh_keeps_the_reboot_button(
    lab: FakeHomelab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "REFRESH_COOLDOWN_SECONDS", 0)
    lab.reboot_required()
    update = make_button_press("homelab")
    call(handlers.on_button, update)
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "homelab",
        "ask:homelab_reboot",
    ]


def test_reboot_alert_carries_the_reboot_button(lab: FakeHomelab) -> None:
    app, _ = fake_app()
    lab.reboot_required("linux-image-amd64")
    run_check(app)
    assert app.bot.send_message.call_args.kwargs["reply_markup"] is homelab.REBOOT_BUTTONS


def test_reboot_button_asks_first(lab: FakeHomelab) -> None:
    lab.reboot_required("linux-image-amd64")
    update = make_button_press("ask:homelab_reboot")
    call(handlers.on_button, update)
    [prompt] = replies(update)
    assert prompt.startswith("🔄 <b>Reboot the homelab?</b>")
    assert "No update is waiting" not in prompt
    confirm, cancel = button_data(update)
    assert confirm.startswith("confirm:homelab_reboot:")
    assert cancel == "cancel:homelab_reboot"
    assert REBOOT not in lab.run.calls


def test_reboot_prompt_says_when_no_update_needs_it(lab: FakeHomelab) -> None:
    update = make_button_press("ask:homelab_reboot")
    call(handlers.on_button, update)
    assert "<i>No update is waiting for a reboot.</i>" in replies(update)[0]
    assert REBOOT not in lab.run.calls


@pytest.mark.usefixtures("lab")
def test_too_old_reboot_button_says_so() -> None:
    update = make_button_press("ask:homelab_reboot")
    update.effective_message = None
    call(handlers.on_button, update)
    update.callback_query.answer.assert_awaited_once_with(
        "This message is too old. Send /homelab again."
    )


def test_confirmed_reboot_asks_logind(lab: FakeHomelab, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    lab.run.by_last_arg["--no-ask-password"] = system.RunResult(0)
    update = confirm_reboot()
    assert edits(update) == ["⏳ <b>Rebooting the homelab…</b>\nI'll say when I'm back."]
    assert lab.run.calls == [REBOOT]
    assert "at" in json.loads(homelab.REBOOT_FILE.read_text())
    assert "action=homelab_reboot event=requested" in caplog.text


def test_reboot_without_the_polkit_rule_explains(lab: FakeHomelab) -> None:
    lab.run.by_last_arg["--no-ask-password"] = system.RunResult(
        1, "Call to RebootWithFlags failed: Access denied"
    )
    error = edits(confirm_reboot())[-1]
    assert "Not allowed to reboot the homelab" in error
    assert "50-candygoogy-reboot.rules /etc/polkit-1/rules.d/" in error
    assert not homelab.REBOOT_FILE.exists()


def test_other_reboot_failures_are_shown(lab: FakeHomelab) -> None:
    lab.run.by_last_arg["--no-ask-password"] = system.RunResult(
        1, 'Operation inhibited by "backup" (reason: "running")'
    )
    error = edits(confirm_reboot())[-1]
    assert "Homelab reboot failed" in error
    assert "inhibited" in error


def test_expired_reboot_does_nothing(lab: FakeHomelab) -> None:
    update = confirm_reboot(issued=time.time() - 120)
    assert "Tap Reboot homelab under /homelab again." in edits(update)[0]
    assert REBOOT not in lab.run.calls


def test_double_tap_reboots_once(lab: FakeHomelab) -> None:
    for _ in range(2):
        _ = confirm_reboot(message_id=7)
    assert lab.run.calls.count(REBOOT) == 1


def test_cancelled_reboot_does_nothing(lab: FakeHomelab) -> None:
    update = make_button_press("cancel:homelab_reboot")
    call(handlers.on_button, update)
    assert edits(update) == ["✖️ Cancelled. Nothing was sent."]
    assert REBOOT not in lab.run.calls


@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_strangers_cannot_reboot_the_homelab(lab: FakeHomelab, user_id: int | None) -> None:
    for data in ("ask:homelab_reboot", f"confirm:homelab_reboot:{int(time.time())}"):
        update = make_button_press(data, user_id=user_id)
        call(handlers.on_button, update)
        assert replies(update) == [] and edits(update) == []
    assert REBOOT not in lab.run.calls


def test_back_online_says_the_homelab_rebooted(lab: FakeHomelab) -> None:
    _ = (lab.proc / "uptime").write_text("60.00 50.00\n")  # booted a minute ago
    _ = homelab.REBOOT_FILE.write_text(json.dumps({"at": time.time() - 150}))
    line = homelab.reboot_outcome()
    assert line == "✅ Homelab rebooted: back 2m 30s after you confirmed."
    assert not homelab.REBOOT_FILE.exists()  # said once
    assert homelab.reboot_outcome() is None
    report = application.startup_report([], [], line)
    assert report is not None and line in report


@pytest.mark.usefixtures("lab")
def test_back_online_says_when_the_reboot_did_not_happen() -> None:
    _ = homelab.REBOOT_FILE.write_text(json.dumps({"at": time.time() - 300}))  # booted days ago
    assert homelab.reboot_outcome() == "⚠️ The homelab reboot asked for 5m ago didn't happen."


@pytest.mark.parametrize("content", ["not json", "[]", '{"at": "x"}'])
@pytest.mark.usefixtures("lab")
def test_bad_reboot_file_is_ignored(content: str) -> None:
    _ = homelab.REBOOT_FILE.write_text(content)
    assert homelab.reboot_outcome() is None
    assert not homelab.REBOOT_FILE.exists()


@pytest.mark.usefixtures("fake_run")
def test_startup_reports_the_reboot(lab: FakeHomelab) -> None:
    _ = (lab.proc / "uptime").write_text("60.00 50.00\n")
    _ = homelab.REBOOT_FILE.write_text(json.dumps({"at": time.time() - 90}))
    app, _ = fake_app()
    app.bot.set_my_commands = AsyncMock()
    app.bot.get_updates = AsyncMock(return_value=[])
    asyncio.run(application.on_startup(app))
    [report] = sent_texts(app)
    assert "Back online" in report and "Homelab rebooted" in report
