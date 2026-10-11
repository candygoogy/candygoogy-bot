# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Only allowed users in a private chat get through; every handler is guarded."""

from unittest.mock import MagicMock

import pytest

from candygoogy import app as application
from candygoogy import (
    auth,
    handlers,
)
from tests.helpers import (
    STRANGER_ID,
    FakeRun,
    call,
    confirm_data,
    edits,
    make_button_press,
    make_update,
    replies,
)

# --- authorization ---------------------------------------------------------


@pytest.mark.parametrize("name", list(handlers.COMMANDS))
@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_unauthorized_users_are_ignored(fake_run: FakeRun, name: str, user_id: int | None) -> None:
    update = make_update(user_id)
    call(handlers.COMMANDS[name], update)
    assert replies(update) == []
    assert fake_run.calls == []


@pytest.mark.parametrize("user_id", [STRANGER_ID, None])
def test_unauthorized_button_presses_are_ignored(
    workstation_on: FakeRun, user_id: int | None
) -> None:
    update = make_button_press(confirm_data("shutdown"), user_id=user_id)
    call(handlers.on_button, update)
    assert edits(update) == []
    assert workstation_on.calls == []


def test_every_handler_is_guarded() -> None:
    guarded = auth._AUTHORIZED_HANDLERS  # pyright: ignore[reportPrivateUsage]
    assert all(h in guarded for h in handlers.COMMANDS.values())
    assert handlers.on_button in guarded


def test_build_app_rejects_unguarded_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    async def naked(_update: object, _context: object) -> None:
        pass

    monkeypatch.setitem(handlers.COMMANDS, "naked", naked)
    with pytest.raises(RuntimeError, match="naked"):
        _ = application.build_app()


def test_build_app_registers_all_commands_and_buttons() -> None:
    app = application.build_app()
    registered = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
    assert registered == set(handlers.COMMANDS)
    assert any(h.callback is handlers.on_button for h in app.handlers[0])


def test_main_keeps_retrying_telegram_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a homelab reboot the network may lag; exiting would exhaust systemd's restarts."""
    app = MagicMock()
    monkeypatch.setattr(application, "build_app", lambda: app)
    monkeypatch.setattr(application, "configure_logging", MagicMock())
    application.main()
    app.run_polling.assert_called_once_with(bootstrap_retries=-1, timeout=50)
