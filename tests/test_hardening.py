# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Hostile input: other scripts' digits, huge numbers, forged buttons; the token
and the private config in log output; strangers flooding the log."""

import asyncio
import io
import logging
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from candygoogy import app as application
from candygoogy import auth, config, handlers, logs, probes, system, timers, wol
from tests.helpers import (
    STRANGER_ID,
    FakeRun,
    FakeSocket,
    call,
    call_with_args,
    confirm_data,
    edits,
    fake_process,
    make_button_press,
    make_update,
    replies,
)

# --- Numbers typed or tapped -----------------------------------------------------------

# Each passes str.isdigit() or a digit-class regex, or is too long, and would
# have reached int(): bare, or as a duration with a unit.
BAD_NUMBERS = ["²", "١٢", "１２", "9" * 5000, "١٢m", "２h", "1h٣٠m"]


def friendly(text: str) -> bool:
    return "Couldn't read" in text or "too long to be a time" in text


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize("text", BAD_NUMBERS)
@pytest.mark.parametrize("command", ["schedule_shutdown", "schedule_wake"])
def test_bad_numbers_as_a_schedule_argument(command: str, text: str) -> None:
    update = make_update()
    call_with_args(handlers.COMMANDS[command], update, text)  # would raise if unhandled
    [reply] = replies(update)
    assert friendly(reply)
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("fake_run")
@pytest.mark.parametrize("text", BAD_NUMBERS)
def test_bad_numbers_as_a_typed_answer(text: str) -> None:
    call_with_args(handlers.COMMANDS["schedule_shutdown"], make_update())  # asks "when?"
    update = make_update()
    update.effective_message.text = text
    call(handlers.on_text, update)
    [reply] = replies(update)
    assert friendly(reply)
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


def test_bare_unicode_digits_are_not_minutes() -> None:
    """ "١٢" used to pass isdigit() and int() to a 12-minute timer."""
    with pytest.raises(ValueError, match="Couldn't read"):
        _ = timers.parse_when("١٢", 0)
    assert timers.parse_when("12", 0) == 12 * 60


def test_typed_text_is_capped_before_parsing() -> None:
    assert timers.parse_when("1h" * 16, 0) == 16 * 3600  # 32 characters: still read
    with pytest.raises(ValueError, match="too long to be a time"):
        _ = timers.parse_when("1h" * 16 + "1", 0)


def test_rejected_text_is_escaped_in_the_reply() -> None:
    with pytest.raises(ValueError) as error:
        _ = timers.parse_when("<b>²</b>", 0)
    assert "<code>&lt;b&gt;²&lt;/b&gt;</code>" in str(error.value)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("confirm:shutdown:１２３", "Expired"),
        ("confirm:shutdown:²", "Expired"),
        ("confirm:reboot:١٢٣٤٥٦٧٨٩٠", "Expired"),
        ("sched:wake:²:１", "No timer set"),
        ("sched:wake:１２３:1", "No timer set"),
        ("sched:shutdown:2000000000:１", "Expired"),
    ],
)
def test_forged_buttons_with_unicode_digits(fake_run: FakeRun, data: str, expected: str) -> None:
    press = make_button_press(data)
    call(handlers.on_button, press)  # would raise if unhandled
    [text] = edits(press)
    assert expected in text
    assert fake_run.calls == []
    assert timers._timers == {}  # pyright: ignore[reportPrivateUsage]


# --- The bot token in log output ----------------------------------------------------------

TOKEN = config.BOT_TOKEN
BOT_ID, _, SECRET = TOKEN.partition(":")
TOKEN_FORMS = [TOKEN, f"{BOT_ID}%3A{SECRET}", f"{BOT_ID}%3a{SECRET}", SECRET]


def leaked(text: str) -> list[str]:
    return [form for form in TOKEN_FORMS if form in text]


def log_the_token_every_way(logger: logging.Logger) -> None:
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/getMe: {BOT_ID}%3A{SECRET}")
    except RuntimeError:
        logger.exception("Request with %s failed", TOKEN)  # as an arg, and in the traceback
    logger.error("Encoded %s, secret alone %s", f"{BOT_ID}%3a{SECRET}", SECRET)


def test_redact_every_form() -> None:
    for form in TOKEN_FORMS:
        assert logs.redact(f"x {form} y") == "x <BOT_TOKEN> y"
    assert logs.redact("nothing secret") == "nothing secret"


def test_stderr_log_never_shows_the_token() -> None:
    """Through the handler main() installs, including the traceback."""
    stream = io.StringIO()
    root = logging.getLogger()
    level = root.level
    handler = application.configure_logging(stream)
    try:
        log_the_token_every_way(logging.getLogger("candygoogy.test"))
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    out = stream.getvalue()
    assert "Traceback" in out and "RuntimeError" in out  # the traceback is there...
    assert leaked(out) == []  # ...without the token
    assert out.count("<BOT_TOKEN>") == 5  # the arg, two in the traceback, two in the last line


def test_logs_buffer_and_file_never_show_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logs.BUFFER.entries.clear()
    logs.install()
    log_the_token_every_way(logging.getLogger("candygoogy.test"))
    chat, _ = logs.logs_message("all")
    content = logs.logs_file().input_file_content
    assert isinstance(content, bytes)
    file_text = content.decode()
    assert "Traceback" in file_text
    assert leaked(chat) == [] and leaked(file_text) == []
    assert "<BOT_TOKEN>" in file_text and "&lt;BOT_TOKEN&gt;" in chat


# --- The private config in log output ----------------------------------------------------

PRIVATE = [
    config.FIRST_MACHINE.ssh_user,
    config.FIRST_MACHINE.ip,
    config.FIRST_MACHINE.mac,
    config.FIRST_MACHINE.mac.upper().replace(":", "-"),
    config.FIRST_MACHINE.ssh_key_path,
]
# What a real ssh says, which names exactly these.
SSH_ERROR = (
    f"ssh: connect to host {config.FIRST_MACHINE.ip} port 22: Connection refused\n"
    + f"{config.FIRST_MACHINE.ssh_user}@{config.FIRST_MACHINE.ip}: Permission denied "
    + f"(publickey). Identity file {config.FIRST_MACHINE.ssh_key_path} not accessible"
).encode()


def all_log_output(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join([caplog.text, *(r.exc_text or "" for r in caplog.records)])


def assert_no_private_config(caplog: pytest.LogCaptureFixture) -> None:
    out = all_log_output(caplog)
    assert out  # something was logged, so the check means something
    for value in PRIVATE:
        assert value not in out, value


def processes(monkeypatch: pytest.MonkeyPatch, **by_program: list[MagicMock]) -> None:
    """Real system.run(), with scripted processes per program (the last one repeats)."""

    async def exec_(*args: str, **_kwargs: object) -> MagicMock:
        queue = by_program[args[0]]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)


def test_a_failing_ssh_command_logs_no_private_config(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    awake_sequence: Callable[..., None],
) -> None:
    caplog.set_level(logging.DEBUG)
    processes(
        monkeypatch,
        ping=[fake_process(0)],
        ssh=[fake_process(255, stderr=SSH_ERROR)],
    )
    monkeypatch.setattr(probes, "ssh_port_open", AsyncMock(return_value=True))
    awake_sequence(True)
    press = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, press)
    assert "Shutdown failed" in edits(press)[-1]  # the allowed user still sees ssh's words
    assert config.FIRST_MACHINE.ip in edits(press)[-1]
    assert_no_private_config(caplog)


def test_an_ssh_timeout_logs_no_private_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    processes(monkeypatch, ssh=[fake_process(hang=True)])
    result = asyncio.run(system.ssh(config.FIRST_MACHINE, "sessions", timeout=0.01))
    assert result.timed_out
    assert "ssh timed out" in caplog.text
    assert_no_private_config(caplog)


def test_a_failing_wake_on_lan_logs_no_private_config(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    awake_sequence: Callable[..., None],
) -> None:
    caplog.set_level(logging.DEBUG)
    # The first packet goes out; the later ones fail, which is what gets logged.
    sent: list[tuple[bytes, tuple[str, int]]] = []
    sockets = iter([FakeSocket(sent)])
    unreachable = OSError(101, "Network is unreachable")
    monkeypatch.setattr(wol, "open_socket", lambda: next(sockets, FakeSocket(sent, unreachable)))
    awake_sequence(False)
    update = make_update()
    call(handlers.wake, update)
    assert len(sent) == 1
    assert "Magic packet 2/3 failed (exit -1)" in caplog.text
    assert_no_private_config(caplog)


def test_status_while_unreachable_logs_no_private_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    neigh = f"{config.FIRST_MACHINE.ip} dev eth0 lladdr {config.FIRST_MACHINE.mac} STALE\n"
    processes(
        monkeypatch,
        ping=[fake_process(1, stderr=f"ping: {config.FIRST_MACHINE.ip} unreachable".encode())],
        ip=[fake_process(0, stdout=neigh.encode())],
    )
    monkeypatch.setattr(probes, "ssh_port_open", AsyncMock(return_value=False))
    logging.getLogger("candygoogy.test").info("status check")  # so the log isn't empty
    update = make_update()
    call(handlers.status, update)
    assert replies(update)
    assert_no_private_config(caplog)


# --- Strangers flooding the log ----------------------------------------------------------


def ignored_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "candygoogy.auth"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """auth's clock reads clock[0]."""
    now = [1000.0]
    monkeypatch.setattr(auth, "monotonic", lambda: now[0])
    return now


def test_a_flooding_stranger_is_logged_once_a_minute(
    clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    for _ in range(100):
        call(handlers.start, make_update(STRANGER_ID))
    assert ignored_lines(caplog) == ["Ignored start from user 2222 in a private chat"]

    for _ in range(3):
        call(handlers.status, make_update(4444))  # a second stranger: their own line
    assert len(ignored_lines(caplog)) == 2

    clock[0] += 59
    call(handlers.start, make_update(STRANGER_ID))
    assert len(ignored_lines(caplog)) == 2  # still within the minute
    clock[0] += 2
    call(handlers.start, make_update(STRANGER_ID))
    assert ignored_lines(caplog)[-1] == (
        "Ignored start from user 2222 in a private chat (100 more from them since the last line)"
    )
    clock[0] += 61
    call(handlers.start, make_update(STRANGER_ID))
    assert ignored_lines(caplog)[-1] == "Ignored start from user 2222 in a private chat"


def test_memory_for_ignored_users_is_bounded(
    clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)  # 5,000 warnings aren't the point here
    for n in range(5000):
        clock[0] += 0.001
        auth.log_ignored(handlers.start, make_update(10_000 + n))
    remembered = auth._ignored  # pyright: ignore[reportPrivateUsage]
    assert len(remembered) == auth.IGNORED_LOG_MAX_USERS
    assert 10_000 + 4999 in remembered and 10_000 not in remembered  # the oldest went first
