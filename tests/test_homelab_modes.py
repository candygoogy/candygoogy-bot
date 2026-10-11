# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""What isn't there: backups not monitored, a container (no host readings), a
program that isn't installed, Docker not reachable. None of it is an error or
an alert. Also the host's / under HOMELAB_HOST_ROOT, the configurable disks and
internet check targets."""

import asyncio
import logging
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram import ReplyKeyboardMarkup

from candygoogy import config, handlers, homelab, internet, system, weekly
from tests.helpers import (
    FakeHomelab,
    button_data,
    call,
    edits,
    fake_app,
    fake_process,
    make_button_press,
    make_update,
    replies,
    sent_texts,
)

REAL_REACHABLE = internet.reachable  # conftest replaces internet.reachable for each test
REAL_DISK_SPACE = homelab.disk_space  # FakeHomelab replaces these two
REAL_IS_MOUNT = homelab.is_mount
REBOOT = ("systemctl", "reboot", "--no-ask-password")


def homelab_card() -> str:
    update = make_update()
    call(handlers.homelab, update)
    [text] = replies(update)
    return text


def run_check(app: MagicMock) -> None:
    asyncio.run(homelab.check_and_alert(app))


def open_alert(key: str, title: str) -> None:
    homelab._open_alerts[key] = title  # pyright: ignore[reportPrivateUsage]


def open_alerts() -> dict[str, str]:
    return homelab._open_alerts  # pyright: ignore[reportPrivateUsage]


# --- The config reaches the modules ----------------------------------------------------


def test_disks_and_targets_come_from_the_config() -> None:
    # The very objects the loader made, not equal copies: the test env uses the
    # default targets, so a hard-coded tuple would otherwise look the same.
    assert homelab.DISKS is config.HOMELAB_DISKS
    assert internet.TARGETS is config.INTERNET_CHECK_TARGETS
    assert config.HOMELAB_DISKS == ("/", "/srv/data")
    assert config.INTERNET_CHECK_TARGETS == (("1.1.1.1", 443), ("9.9.9.9", 443))
    assert config.IN_CONTAINER is False


@pytest.mark.usefixtures("lab")
def test_only_configured_disks_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(homelab, "DISKS", ("/",))
    text = homelab_card()
    assert "<code>/</code> 150 GB free" in text
    assert "/srv/data" not in text


# --- Backups off (no BACKUP_DIR) ----------------------------------------------------------


@pytest.fixture
def no_backups(lab: FakeHomelab, monkeypatch: pytest.MonkeyPatch) -> FakeHomelab:
    """Backup monitoring off, with nothing completed in the folder either: if the
    folder were still looked at, that would be a "No completed backup" problem."""
    monkeypatch.setattr(homelab, "BACKUP_DIR", None)
    shutil.rmtree(lab.backups)
    return lab


@pytest.mark.usefixtures("no_backups")
def test_homelab_card_has_no_backup_line() -> None:
    text = homelab_card()
    assert "ackup" not in text and "💾" not in text
    assert "⚠️" not in text


@pytest.mark.usefixtures("no_backups")
def test_no_backup_alerts_and_an_open_one_is_left_alone() -> None:
    open_alert("backup", "Backups are overdue")  # from before monitoring was turned off
    app, _ = fake_app()
    run_check(app)
    run_check(app)
    assert sent_texts(app) == []  # no new alert, no "back to normal"
    assert open_alerts() == {"backup": "Backups are overdue"}


@pytest.mark.usefixtures("no_backups", "fake_run")
def test_daily_summary_has_no_backup_line() -> None:
    update = make_update()
    call(handlers.summary, update)
    [text] = replies(update)
    assert "ackup" not in text and "💾" not in text
    assert "🖥 Homelab: all good." in text


@pytest.mark.usefixtures("no_backups")
def test_weekly_report_has_no_backup_section() -> None:
    text = weekly.week_message(time.time())
    assert "ackup" not in text and "💾" not in text
    assert "🌐 Internet" in text and "🖥 Homelab" in text  # the rest is still there


def test_snapshot_helpers_when_backups_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(homelab, "BACKUP_DIR", None)
    assert not homelab.backups_monitored()
    assert homelab.backup_snapshots() == []
    with pytest.raises(OSError):
        _ = homelab.snapshot_size("20261005-021500")


# --- In a container -----------------------------------------------------------------------


@pytest.fixture
def container(lab: FakeHomelab, monkeypatch: pytest.MonkeyPatch) -> FakeHomelab:
    """CANDYGOOGY_IN_CONTAINER=1, with a failed unit and a pending reboot on the
    host that the bot must not report from inside the container."""
    monkeypatch.setattr(homelab, "IN_CONTAINER", True)
    lab.failed("example.service")
    lab.reboot_required("linux-image-amd64")
    lab.updates("example-pkg")
    return lab


def test_container_card_says_what_isnt_checked(container: FakeHomelab) -> None:
    text = homelab_card()
    assert homelab.IN_CONTAINER_LINE in text
    assert "Couldn't check" not in text
    assert "failed" not in text and "Reboot" not in text and "📦" not in text
    assert "🐳 All 3 containers running." in text  # Docker is still read
    assert container.run.calls == []  # no systemctl, no apt
    assert len(container.docker_requests) == 1


@pytest.mark.usefixtures("container")
def test_container_raises_no_host_alerts() -> None:
    app, _ = fake_app()
    run_check(app)
    assert sent_texts(app) == []


@pytest.mark.usefixtures("container")
def test_container_leaves_open_host_alerts_alone() -> None:
    open_alert("unit:example.service", "<code>example.service</code> failed")
    open_alert("reboot", "Reboot required")
    app, _ = fake_app()
    run_check(app)
    assert sent_texts(app) == []  # neither repeated nor "back to normal"
    assert set(open_alerts()) == {"unit:example.service", "reboot"}


@pytest.mark.usefixtures("container")
def test_container_offers_no_reboot_button() -> None:
    update = make_update()
    call(handlers.homelab, update)
    assert button_data(update) == ["homelab"]


def test_container_refuses_the_reboot_buttons(container: FakeHomelab) -> None:
    ask = make_button_press("ask:homelab_reboot")
    call(handlers.on_button, ask)
    ask.callback_query.answer.assert_awaited_once_with(homelab.REBOOT_UNAVAILABLE)
    assert replies(ask) == []

    confirm = make_button_press(f"confirm:homelab_reboot:{int(time.time())}")
    call(handlers.on_button, confirm)
    assert edits(confirm) == [f"✖️ {homelab.REBOOT_UNAVAILABLE}"]
    assert REBOOT not in container.run.calls


def test_container_reboot_request_is_refused(container: FakeHomelab) -> None:
    refusal = asyncio.run(homelab.request_reboot())
    assert refusal is not None and homelab.REBOOT_UNAVAILABLE in refusal
    assert REBOOT not in container.run.calls


# --- The host's / under HOMELAB_HOST_ROOT -------------------------------------------------


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        (None, "/srv/data", "/srv/data"),
        ("/", "/srv/data", "/srv/data"),
        ("/host", "/", "/host"),
        ("/host", "/srv/data", "/host/srv/data"),
        ("/host", "/run", "/host/run"),
    ],
)
def test_on_host(
    monkeypatch: pytest.MonkeyPatch, root: str | None, path: str, expected: str
) -> None:
    monkeypatch.setattr(homelab, "HOST_ROOT", root)
    assert homelab.on_host(path) == expected


@pytest.fixture
def host_root(lab: FakeHomelab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOMELAB_HOST_ROOT is a tmp dir standing in for the host's /, with its
    own run/; the container's own RUN has a reboot-required that must be ignored."""
    root = tmp_path / "host"
    (root / "run").mkdir(parents=True)
    (root / "srv" / "data").mkdir(parents=True)
    monkeypatch.setattr(homelab, "HOST_ROOT", str(root))
    monkeypatch.setattr(homelab, "RUN", "/run")
    lab.reboot_required("not-the-hosts")  # in lab.run_dir, which is no longer read
    return root


def test_host_root_disks_are_read_under_it(
    lab: FakeHomelab, host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str]] = []

    def space(path: str) -> tuple[float, float]:
        asked.append(("space", path))
        return (100 * 1024**3, 200 * 1024**3)

    def mounted(path: str) -> bool:
        asked.append(("mount", path))
        return path != f"{host_root}/srv/data"

    monkeypatch.setattr(homelab, "disk_space", space)
    monkeypatch.setattr(homelab, "is_mount", mounted)
    health = asyncio.run(homelab.read_health())
    assert sorted(asked) == [
        ("mount", f"{host_root}/srv/data"),
        ("space", str(host_root)),
        ("space", f"{host_root}/srv/data"),
    ]
    # Shown and keyed by the host's path.
    assert [d.path for d in health.disks] == ["/", "/srv/data"]
    found, _ = homelab.problems(health, time.time())
    assert "mount:/srv/data" in found
    assert lab.run.calls  # outside a container, the rest is read as usual


@pytest.mark.usefixtures("host_root")
def test_host_root_reads_real_disks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real readings, on the tmp dir: space is there, /srv/data isn't a mount."""
    monkeypatch.setattr(homelab, "disk_space", REAL_DISK_SPACE)
    monkeypatch.setattr(homelab, "is_mount", REAL_IS_MOUNT)
    health = asyncio.run(homelab.read_health())
    root, data = health.disks
    assert root.mounted and root.space is not None and root.space[1] > 0
    assert not data.mounted


def test_host_root_reboot_required(lab: FakeHomelab, host_root: Path) -> None:
    assert asyncio.run(homelab.read_health()).reboot is None  # the container's own is ignored
    _ = (host_root / "run" / "reboot-required").write_text("")
    _ = (host_root / "run" / "reboot-required.pkgs").write_text("linux-image-amd64\n")
    assert asyncio.run(homelab.read_health()).reboot == ["linux-image-amd64"]
    assert lab.run_dir != host_root / "run"


@pytest.fixture
def container_with_host_root(container: FakeHomelab, host_root: Path) -> FakeHomelab:
    """In a container, with a pending reboot on the host."""
    _ = (host_root / "run" / "reboot-required").write_text("")
    return container


@pytest.mark.usefixtures("container_with_host_root")
def test_container_with_host_root_reports_the_hosts_reboot() -> None:
    text = homelab_card()
    assert homelab.IN_CONTAINER_HOST_ROOT_LINE in text
    assert homelab.IN_CONTAINER_LINE not in text
    assert "⚠️ Reboot required." in text
    assert "failed" not in text and "📦" not in text  # units and updates still aren't read


@pytest.mark.usefixtures("container_with_host_root")
def test_container_with_host_root_alerts_without_a_reboot_button() -> None:
    app, _ = fake_app()
    run_check(app)
    [alert] = sent_texts(app)
    assert "Reboot required" in alert
    # The usual reply keyboard, no inline reboot button.
    assert isinstance(app.bot.send_message.call_args.kwargs["reply_markup"], ReplyKeyboardMarkup)
    assert "reboot" in open_alerts()


@pytest.mark.usefixtures("container_with_host_root")
def test_container_with_host_root_keeps_reboot_alerts_at_startup() -> None:
    open_alert("unit:example.service", "<code>example.service</code> failed")
    open_alert("reboot", "Reboot required")
    homelab.drop_switched_off_alerts(time.time())
    assert set(open_alerts()) == {"reboot"}


# --- A program that isn't installed -------------------------------------------------------

NOT_FOUND = system.RunResult(-1, "[Errno 2] No such file or directory", missing=True)


def make_missing(lab: FakeHomelab, program: str) -> None:
    """The program isn't installed; for Docker, nothing answers at DOCKER_HOST."""
    if program == "docker":
        lab.docker_answer = httpx.ConnectError("[Errno 2] No such file or directory")
    else:
        lab.run.results[program] = NOT_FOUND


# An alert each program's reading could have opened before it went missing.
OPEN_ALERT = {
    "systemctl": ("unit:example.service", "<code>example.service</code> failed"),
    "docker": ("container:app", "<code>app</code> is stopped"),
    "apt": None,  # updates are never an alert
}


@pytest.mark.parametrize("program", ["systemctl", "docker", "apt"])
def test_missing_program_reads_as_not_installed(lab: FakeHomelab, program: str) -> None:
    make_missing(lab, program)
    text = homelab_card()
    assert homelab.NOT_INSTALLED[program] in text
    assert "Couldn't check" not in text
    assert "⚠️" not in text


@pytest.mark.parametrize("program", ["systemctl", "docker", "apt"])
def test_missing_program_raises_no_alert_and_leaves_open_ones(
    lab: FakeHomelab, program: str
) -> None:
    make_missing(lab, program)
    if alert := OPEN_ALERT[program]:
        open_alert(*alert)
    app, _ = fake_app()
    run_check(app)
    assert sent_texts(app) == []
    if alert:
        assert alert[0] in open_alerts()


def test_failing_program_still_says_couldnt_check(lab: FakeHomelab) -> None:
    """Installed but failing is different from not installed."""
    lab.run.results["systemctl"] = system.RunResult(1, "Failed to connect to bus")
    lab.docker_answer = httpx.Response(500, json={"message": "server error"})
    text = homelab_card()
    assert "<i>Couldn't check services.</i>" in text
    assert "<i>Couldn't check containers.</i>" in text


def test_unreachable_docker_is_logged_without_alerting(
    lab: FakeHomelab, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    make_missing(lab, "docker")
    _ = homelab_card()
    assert "Docker isn't reachable ([Errno 2] No such file or directory)" in caplog.text


def test_run_marks_a_missing_program(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("apt"))
    )
    result = asyncio.run(system.run("apt", "list", timeout=1))
    assert result.missing and not result.ok
    assert "apt isn't installed" in caplog.text

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=PermissionError("denied"))
    )
    result = asyncio.run(system.run("apt", "list", timeout=1))
    assert not result.missing and not result.ok  # there, but couldn't be started


def test_run_passes_the_environment_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env= argument: the child inherits e.g. TZ as is."""
    start = AsyncMock(return_value=fake_process(0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setenv("TZ", "UTC")
    _ = asyncio.run(system.run("apt", "list", timeout=1))
    assert "env" not in start.call_args.kwargs


# --- Internet check targets ---------------------------------------------------------------


def test_internet_check_uses_the_configured_targets_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(internet, "TARGETS", (("example.net", 853), ("2001:db8::1", 443)))
    connect = AsyncMock(side_effect=OSError("unreachable"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert not asyncio.run(REAL_REACHABLE())
    assert [c.args for c in connect.await_args_list] == [("example.net", 853), ("2001:db8::1", 443)]


@pytest.mark.parametrize(
    ("targets", "footer"),
    [
        ((("1.1.1.1", 443), ("9.9.9.9", 443)), "1.1.1.1 and 9.9.9.9 are tried every minute"),
        ((("1.1.1.1", 443),), "1.1.1.1 is tried every minute"),
        (
            (("a.example", 1), ("2001:db8::1", 443), ("9.9.9.9", 443)),
            "a.example, [2001:db8::1] and 9.9.9.9 are tried every minute",
        ),
    ],
)
def test_net_footer_names_the_targets(
    monkeypatch: pytest.MonkeyPatch, targets: tuple[tuple[str, int], ...], footer: str
) -> None:
    monkeypatch.setattr(internet, "TARGETS", targets)
    update = make_update()
    call(handlers.net, update)
    assert footer in replies(update)[0]


# --- Stale alerts for checks that are off ----------------------------------------------------


@pytest.mark.usefixtures("no_backups")
def test_startup_drops_a_backup_alert_when_backups_are_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    now = time.time()
    open_alert("backup", "Backups are overdue")
    open_alert("unit:example.service", "<code>example.service</code> failed")
    homelab.incident_opened("backup", "Backups are overdue", now - 3600)
    homelab.drop_switched_off_alerts(now)
    assert set(open_alerts()) == {"unit:example.service"}  # that check is still on
    [incident] = homelab.incidents_since(0)
    assert incident.end == now  # no longer "still open" in the weekly report
    assert "Dropped 1 open homelab alert(s)" in caplog.text


@pytest.mark.usefixtures("container")
def test_startup_drops_host_alerts_in_a_container() -> None:
    open_alert("unit:example.service", "<code>example.service</code> failed")
    open_alert("reboot", "Reboot required")
    open_alert("container:app", "<code>app</code> is stopped")
    homelab.drop_switched_off_alerts(time.time())
    assert set(open_alerts()) == {"container:app"}  # Docker is still checked


@pytest.mark.usefixtures("lab")
def test_startup_keeps_alerts_for_checks_that_are_on() -> None:
    open_alert("backup", "Backups are overdue")
    open_alert("reboot", "Reboot required")
    homelab.drop_switched_off_alerts(time.time())
    assert set(open_alerts()) == {"backup", "reboot"}
