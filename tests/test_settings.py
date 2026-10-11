"""The settings loader: every rule, every problem reported at once, no values in messages."""

import dataclasses
import inspect
import logging
import os
import re
from pathlib import Path

import pytest

from candygoogy import config, settings
from candygoogy.settings import ConfigError, MachineLabels, load_settings

# The required variables, all valid. The token has a real token's shape but isn't one.
VALID = {
    "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
    "AUTHORIZED_USER_IDS": "1111",
    "WORKSTATION_MAC": "00:00:5e:00:53:01",
    "WORKSTATION_IP": "192.0.2.10",
    "SSH_USER": "tester",
}
REQUIRED = list(VALID)


def problems(env: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    return error.value.problems


def problem_for(name: str, value: str) -> str:
    """The one problem reported when only `name` is set to `value`."""
    [problem] = problems({**VALID, name: value})
    assert problem.startswith(name)
    return problem


# --- A valid configuration -------------------------------------------------------------


def test_full_valid_env() -> None:
    loaded = load_settings(
        {
            **VALID,
            "AUTHORIZED_USER_IDS": "1111, 3333",
            "WORKSTATION_MAC": "00-00-5E-00-53-01",
            "WORKSTATION_IP": "2001:DB8::1",
            "SSH_KEY_PATH": "/keys/bot_ed25519",
            "BACKUP_MAX_AGE_HOURS": "36.5",
            "DIGEST_TIME": "7:15",
            "WEEKLY_REPORT": "fri 18:30",
        }
    )
    assert loaded.bot_token == VALID["BOT_TOKEN"]
    assert loaded.authorized_user_ids == (1111, 3333)
    assert loaded.machines == (
        settings.Machine(
            "workstation",
            "windows",
            "00:00:5e:00:53:01",
            "2001:db8::1",
            "tester",
            "/keys/bot_ed25519",
        ),
    )
    assert loaded.backup_max_age_hours == 36.5
    assert loaded.digest_at == (7, 15)
    assert loaded.weekly_report_at == (4, 18, 30)


def test_defaults_for_optional_settings() -> None:
    loaded = load_settings(VALID)
    assert loaded.machines[0].ssh_key_path == os.path.expanduser("~/.ssh/id_ed25519")
    assert loaded.backup_max_age_hours == 26
    assert loaded.digest_at == (8, 0)
    assert loaded.weekly_report_at == (6, 20, 0)


def test_summaries_can_be_turned_off() -> None:
    loaded = load_settings({**VALID, "DIGEST_TIME": "off", "WEEKLY_REPORT": "OFF"})
    assert loaded.digest_at is None and loaded.weekly_report_at is None


def test_values_are_stripped() -> None:
    loaded = load_settings({k: f"  {v}\n" for k, v in VALID.items()})
    assert loaded.bot_token == VALID["BOT_TOKEN"]
    assert loaded.machines[0].ssh_user == "tester"


def test_printing_settings_never_shows_the_token() -> None:
    loaded = load_settings(VALID)
    assert "TEST-TOKEN" not in repr(loaded)
    assert "TEST-TOKEN" not in str(loaded)


def test_the_running_config_comes_from_the_loader() -> None:
    assert config.SETTINGS.authorized_user_ids == config.AUTHORIZED_USER_IDS == (1111,)
    assert config.MACHINES == config.SETTINGS.machines
    assert config.FIRST_MACHINE is config.MACHINES[0]


def test_the_loader_never_reads_the_environment_itself() -> None:
    """bot.py and the tests decide what it reads, so a test can't pick up a real .env."""
    source = inspect.getsource(settings)
    assert "os.environ" not in source
    assert "dotenv" not in source


def test_no_unicode_digit_shortcuts_in_the_loader() -> None:
    """\\d and isdigit() accept other scripts' digits; the loader spells out [0-9]."""
    source = inspect.getsource(settings)
    assert "isdigit" not in source
    assert "\\d" not in source


# --- Missing and empty -------------------------------------------------------------------


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_variable_missing(name: str) -> None:
    env = {k: v for k, v in VALID.items() if k != name}
    assert problems(env) == [f"{name} is missing."]


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_each_required_variable_empty(name: str, value: str) -> None:
    assert problems({**VALID, name: value}) == [f"{name} is empty."]


NO_MACHINE = (
    "No machine is configured: set WORKSTATION_MAC, WORKSTATION_IP and SSH_USER "
    + "(one Windows PC), or MACHINES_FILE."
)
MACHINE_VARS = ("WORKSTATION_MAC", "WORKSTATION_IP", "SSH_USER")
WITHOUT_MACHINE = {k: v for k, v in VALID.items() if k not in MACHINE_VARS}


def test_nothing_set_reports_every_required_variable() -> None:
    """The two required ones by name, then one line for the machine that names both ways."""
    assert problems({}) == ["BOT_TOKEN is missing.", "AUTHORIZED_USER_IDS is missing.", NO_MACHINE]


@pytest.mark.parametrize("empty", ["", "   "])
def test_no_machine_alone_is_one_problem(empty: str) -> None:
    """Missing or empty: neither WORKSTATION_* nor MACHINES_FILE says which machine."""
    assert problems(WITHOUT_MACHINE) == [NO_MACHINE]
    env = {**WITHOUT_MACHINE, **dict.fromkeys(MACHINE_VARS, empty), "MACHINES_FILE": empty}
    assert problems(env) == [NO_MACHINE]


def test_no_machine_with_other_problems_is_one_error() -> None:
    env = {**WITHOUT_MACHINE, "BOT_TOKEN": "SECRET-Zq9", "SSH_KEY_PATH": "relative/SECRET-Zq9"}
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    assert error.value.problems == [
        "BOT_TOKEN doesn't look like a bot token "
        + "(digits, a colon, then 30 or more of A-Z a-z 0-9 _ -).",
        NO_MACHINE,
        "SSH_KEY_PATH must be an absolute path (~ is allowed).",  # still checked when set
    ]
    assert "Zq9" not in str(error.value)


@pytest.mark.parametrize("given", MACHINE_VARS)
def test_partial_workstation_settings_keep_their_own_messages(given: str) -> None:
    """One of the three is set, so the others are reported by name, as before."""
    env = {**WITHOUT_MACHINE, given: VALID[given]}
    assert problems(env) == [f"{name} is missing." for name in MACHINE_VARS if name != given]


def test_several_problems_are_reported_together() -> None:
    found = problems(
        {
            **VALID,
            "BOT_TOKEN": "nope",
            "WORKSTATION_IP": "nope",
            "DIGEST_TIME": "nope",
            "BACKUP_MAX_AGE_HOURS": "nope",
        }
    )
    assert [p.split()[0] for p in found] == [
        "BOT_TOKEN",
        "WORKSTATION_IP",
        "BACKUP_MAX_AGE_HOURS",
        "DIGEST_TIME",
    ]


def test_error_message_lists_problems_and_points_to_the_example() -> None:
    with pytest.raises(ConfigError) as error:
        _ = load_settings({**VALID, "SSH_USER": "-x"})
    message = str(error.value)
    assert message.splitlines()[0] == "The configuration has problems:"
    assert "- SSH_USER " in message
    assert ".env.example" in message


# Recognisable values that must never be echoed back: a token pasted into the
# wrong variable would otherwise end up in the journal.
SECRET = "123456:SECRET-Zq9-not-a-real-token-77777"


def test_no_value_ever_appears_in_a_message() -> None:
    env = {
        "BOT_TOKEN": "SECRET-Zq9",
        "AUTHORIZED_USER_IDS": SECRET,
        "WORKSTATION_MAC": "SECRET-Zq9",
        "WORKSTATION_IP": "SECRET-Zq9",
        "SSH_USER": "-SECRET-Zq9",
        "SSH_KEY_PATH": "-SECRET-Zq9",
        "BACKUP_MAX_AGE_HOURS": "SECRET-Zq9",
        "DIGEST_TIME": "SECRET-Zq9",
        "WEEKLY_REPORT": "SECRET-Zq9",
    }
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    assert len(error.value.problems) == len(env)  # every variable reported
    for text in (str(error.value), repr(error.value), *error.value.problems):
        assert "Zq9" not in text
        assert "SECRET" not in text


# --- BOT_TOKEN ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "123456",
        "123456:",
        "123456:short-token",
        ":ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
        "bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
        "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdef",
        "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef\nmore",
        "１23456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
        "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdeé",
    ],
)
def test_bad_bot_token(value: str) -> None:
    assert "doesn't look like a bot token" in problem_for("BOT_TOKEN", value)


# --- AUTHORIZED_USER_IDS -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1111", (1111,)),
        ("1111,3333", (1111, 3333)),
        ("1111, 3333", (1111, 3333)),
        ("1111 3333", (1111, 3333)),
        (" 1111 ,\t3333\n", (1111, 3333)),
        ("3333,1111,3333", (3333, 1111)),  # duplicates dropped, order kept
        ("0042", (42,)),
        ("9223372036854775807", (2**63 - 1,)),
    ],
)
def test_allowed_user_ids(value: str, expected: tuple[int, ...]) -> None:
    assert load_settings({**VALID, "AUTHORIZED_USER_IDS": value}).authorized_user_ids == expected


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("0", "out of range"),
        ("1111,0", "entry 2 is out of range"),
        ("9223372036854775808", "out of range"),
        ("1" * 20, "not a whole number"),
        ("9" * 5000, "not a whole number"),  # int() itself would refuse it
        ("-1", "not a whole number"),
        ("+1", "not a whole number"),
        ("1.5", "not a whole number"),
        ("1e3", "not a whole number"),
        ("0x10", "not a whole number"),
        ("abc", "not a whole number"),
        ("1111;3333", "not a whole number"),
        ("１１１１", "not a whole number"),  # fullwidth
        ("²", "not a whole number"),  # superscript: isdigit() says yes
        ("١١١١", "not a whole number"),  # Arabic-Indic
        ("1111 3333", "not a whole number"),  # no-break space isn't a separator
        ("1111,,3333", "entry 2 is empty"),
        ("1111,", "entry 2 is empty"),
        (",1111", "entry 1 is empty"),
        ("1111, ,3333", "entry 2 is empty"),
    ],
)
def test_bad_allowed_user_ids(value: str, why: str) -> None:
    assert why in problem_for("AUTHORIZED_USER_IDS", value)


def test_old_single_id_variable_says_it_was_renamed() -> None:
    env = {k: v for k, v in VALID.items() if k != "AUTHORIZED_USER_IDS"}
    [problem] = problems({**env, "AUTHORIZED_USER_ID": "1111"})
    assert "AUTHORIZED_USER_ID was renamed to AUTHORIZED_USER_IDS" in problem


def test_old_variable_is_ignored_when_the_new_one_is_set() -> None:
    loaded = load_settings({**VALID, "AUTHORIZED_USER_ID": "9999"})
    assert loaded.authorized_user_ids == (1111,)


# --- The machine: MAC, IP, SSH user, key path ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "00:00:5e:00:53",
        "00:00:5e:00:53:01:02",
        "00:00-5e:00:53:01",  # mixed separators
        "0000.5e00.5301",
        "00005e005301",
        "gg:00:5e:00:53:01",
        "00:00:5e:00:53:1",
        "００:00:5e:00:53:01",
    ],
)
def test_bad_mac(value: str) -> None:
    assert "6 hex pairs" in problem_for("WORKSTATION_MAC", value)


@pytest.mark.parametrize("value", ["00:00:5E:00:53:01", "00-00-5e-00-53-01"])
def test_mac_is_normalised(value: str) -> None:
    loaded = load_settings({**VALID, "WORKSTATION_MAC": value})
    assert loaded.machines[0].mac == "00:00:5e:00:53:01"


@pytest.mark.parametrize(
    "value",
    ["192.0.2.256", "192.0.2", "192.0.2.010", "example.com", "１９２.0.2.10", "2001:db8::g"],
)
def test_bad_ip(value: str) -> None:
    assert "IPv4 or IPv6" in problem_for("WORKSTATION_IP", value)


def test_ip_with_a_scope_is_refused() -> None:
    assert "%scope" in problem_for("WORKSTATION_IP", "fe80::1%eth0")


@pytest.mark.parametrize(
    "value",
    ["-oProxyCommand=x", "-", "a b", "user@host", "a;b", "$(id)", "ü", "a" * 65, "a\nb"],
)
def test_bad_ssh_user(value: str) -> None:
    assert "can't start with -" in problem_for("SSH_USER", value)


@pytest.mark.parametrize("value", ["a", "pc-user", "first.last", "_svc", "A1", "a" * 64])
def test_good_ssh_user(value: str) -> None:
    assert load_settings({**VALID, "SSH_USER": value}).machines[0].ssh_user == value


def test_ssh_key_path_is_expanded() -> None:
    loaded = load_settings({**VALID, "SSH_KEY_PATH": "~/keys/bot"})
    assert loaded.machines[0].ssh_key_path == os.path.expanduser("~/keys/bot")
    assert not loaded.machines[0].ssh_key_path.startswith("~")


@pytest.mark.parametrize(("value", "why"), [("", "is empty"), ("  ", "is empty"), ("-i", "start")])
def test_bad_ssh_key_path(value: str, why: str) -> None:
    assert why in problem_for("SSH_KEY_PATH", value)


def test_machine_check_uses_the_callers_labels() -> None:
    """Ready for several machines: the same rules, named the way the caller names them."""
    labels = MachineLabels(
        "machines.pc.mac", "machines.pc.ip", "machines.pc.user", "machines.pc.key"
    )
    machine, found = settings.check_machine("nope", "nope", "-x", "-k", labels)
    assert machine is None
    assert [p.split()[0] for p in found] == [
        "machines.pc.mac",
        "machines.pc.ip",
        "machines.pc.user",
        "machines.pc.key",
    ]
    assert not any("WORKSTATION" in p or "SSH_" in p for p in found)
    _, missing = settings.check_machine(None, None, None, None, labels)
    assert missing == [
        "machines.pc.mac is missing.",
        "machines.pc.ip is missing.",
        "machines.pc.user is missing.",
    ]


def test_machine_check_returns_the_cleaned_machine() -> None:
    labels = settings.WORKSTATION_LABELS
    machine, found = settings.check_machine(" 00-00-5E-00-53-01 ", "192.0.2.10", "pc", None, labels)
    assert found == []
    assert machine == settings.Machine(
        "workstation",
        "windows",
        "00:00:5e:00:53:01",
        "192.0.2.10",
        "pc",
        os.path.expanduser("~/.ssh/id_ed25519"),
    )


# --- Numbers and times -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["0", "0.0", "-1", "abc", "1e3", "inf", "nan", "9" * 400, "２６", "1,5", ".5", "5."]
)
def test_bad_backup_max_age(value: str) -> None:
    assert "number of hours above 0" in problem_for("BACKUP_MAX_AGE_HOURS", value)


@pytest.mark.parametrize("value", ["26", "1.5", "0.25", " 48 "])
def test_good_backup_max_age(value: str) -> None:
    loaded = load_settings({**VALID, "BACKUP_MAX_AGE_HOURS": value})
    assert loaded.backup_max_age_hours == float(value)


# --- The SSH key at startup ----------------------------------------------------------------


def with_key(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The one configured machine, with its key at `path`."""
    machine = dataclasses.replace(config.FIRST_MACHINE, ssh_key_path=path)
    monkeypatch.setattr(config, "MACHINES", (machine,))


def test_unreadable_ssh_key_warns_without_the_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with_key(monkeypatch, "/nonexistent/secret-key-path-Zq9")
    assert not config.check_ssh_key()
    assert "The SSH key for workstation isn't a readable file" in caplog.text
    assert "Zq9" not in caplog.text


def test_readable_ssh_key_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    key = tmp_path / "key"
    _ = key.write_text("not really a key")
    with_key(monkeypatch, str(key))
    assert config.check_ssh_key()
    assert caplog.text == ""


def test_a_folder_is_not_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with_key(monkeypatch, str(tmp_path))
    assert not config.check_ssh_key()
    assert str(tmp_path) not in caplog.text


# --- Homelab disks, backups, internet targets, container ---------------------------------


def test_homelab_defaults() -> None:
    loaded = load_settings(VALID)
    assert loaded.homelab_disks == ("/",)
    assert loaded.backup_dir is None  # backup monitoring is off unless set
    assert loaded.internet_targets == (("1.1.1.1", 443), ("9.9.9.9", 443))
    assert loaded.in_container is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/", ("/",)),
        ("/,/srv/data", ("/", "/srv/data")),
        (" / , /srv/data/ ", ("/", "/srv/data")),
        ("/srv/data,/srv/data,/", ("/srv/data", "/")),  # duplicates dropped, order kept
        ("/srv/./data", ("/srv/data",)),
        ("/srv/my disk", ("/srv/my disk",)),
        (",".join(f"/d{n}" for n in range(10)), tuple(f"/d{n}" for n in range(10))),
    ],
)
def test_homelab_disks(value: str, expected: tuple[str, ...]) -> None:
    assert load_settings({**VALID, "HOMELAB_DISKS": value}).homelab_disks == expected


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("srv/data", "entry 1 must be an absolute path"),
        ("/,data", "entry 2 must be an absolute path"),
        ("~/data", "must be an absolute path"),
        ("C:\\data", "must be an absolute path"),
        ("/srv/../etc", "can't contain .."),
        ("/..", "can't contain .."),
        ("/srv/da\tta", "control characters"),
        ("/srv/\x00", "control characters"),
        ("/srv/\x85x", "control characters"),  # C1 control
        ("/,,/srv", "entry 2 is empty"),
        ("/,", "entry 2 is empty"),
        (",".join(f"/d{n}" for n in range(11)), "more than 10 paths"),
    ],
)
def test_bad_homelab_disks(value: str, why: str) -> None:
    assert why in problem_for("HOMELAB_DISKS", value)


def test_empty_homelab_disks() -> None:
    assert problems({**VALID, "HOMELAB_DISKS": " "}) == [
        "HOMELAB_DISKS is empty; leave it out to check just /."
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/srv/backups", "/srv/backups"),
        (" /srv/backups/ ", "/srv/backups"),
        ("", None),
        ("  ", None),
    ],
)
def test_backup_dir(value: str, expected: str | None) -> None:
    assert load_settings({**VALID, "BACKUP_DIR": value}).backup_dir == expected


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("backups", "must be an absolute path"),
        ("/srv/../backups", "can't contain .."),
        ("/srv/back\x07ups", "control characters"),
    ],
)
def test_bad_backup_dir(value: str, why: str) -> None:
    assert why in problem_for("BACKUP_DIR", value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.1.1.1:443", (("1.1.1.1", 443),)),
        ("one.one.one.one:443", (("one.one.one.one", 443),)),
        ("[2606:4700:4700::1111]:443", (("2606:4700:4700::1111", 443),)),
        ("[2001:DB8::1]:53", (("2001:db8::1", 53),)),  # canonical form
        ("a:1,b-c.example:65535", (("a", 1), ("b-c.example", 65535))),
        (" example.com:80 , 9.9.9.9:443 ", (("example.com", 80), ("9.9.9.9", 443))),
        ("1.1.1.1:443,1.1.1.1:443", (("1.1.1.1", 443),)),  # duplicates dropped
        (",".join(f"h{n}:1" for n in range(5)), tuple((f"h{n}", 1) for n in range(5))),
    ],
)
def test_internet_check_targets(value: str, expected: tuple[tuple[str, int], ...]) -> None:
    loaded = load_settings({**VALID, "INTERNET_CHECK_TARGETS": value})
    assert loaded.internet_targets == expected


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("1.1.1.1", "must be host:port"),
        (":443", "must be host:port"),
        ("1.1.1.1:0", "port from 1 to 65535"),
        ("1.1.1.1:65536", "port from 1 to 65535"),
        ("1.1.1.1:-1", "port from 1 to 65535"),
        ("1.1.1.1:", "port from 1 to 65535"),
        ("1.1.1.1:443443", "port from 1 to 65535"),
        ("1.1.1.1:４４３", "port from 1 to 65535"),
        ("2606:4700::1111:443", "needs [ ] around an IPv6 address"),
        ("[2606:4700::1111]", "must be [IPv6 address]:port"),
        ("[2606:4700::1111]443", "must be [IPv6 address]:port"),
        ("[1.1.1.1]:443", "no valid IPv6 address"),
        ("[fe80::1%eth0]:443", "%scope"),
        ("999.1.1.1:443", "invalid IPv4 address"),
        ("1.2.3:443", "invalid IPv4 address"),
        ("１.1.1.1:443", "invalid host name"),
        ("-bad.example:443", "invalid host name"),
        ("bad_host:443", "invalid host name"),
        ("exa mple.com:443", "invalid host name"),
        ("exämple.com:443", "invalid host name"),
        ("a" * 64 + ".example:443", "invalid host name"),  # a label over 63
        ("example..com:443", "invalid host name"),
        ("1.1.1.1:443,", "entry 2 is empty"),
        ("a:1,,b:1", "entry 2 is empty"),
        (",".join(f"h{n}:1" for n in range(6)), "more than 5 targets"),
    ],
)
def test_bad_internet_check_targets(value: str, why: str) -> None:
    assert why in problem_for("INTERNET_CHECK_TARGETS", value)


def test_empty_internet_check_targets() -> None:
    assert problems({**VALID, "INTERNET_CHECK_TARGETS": ""}) == [
        "INTERNET_CHECK_TARGETS is empty; leave it out to use the defaults."
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), (" TRUE ", True), ("0", False), ("false", False), ("", False)],
)
def test_in_container(value: str, expected: bool) -> None:
    assert load_settings({**VALID, "CANDYGOOGY_IN_CONTAINER": value}).in_container is expected


@pytest.mark.parametrize("value", ["yes", "on", "2", "１", "truee", "-1"])
def test_bad_in_container(value: str) -> None:
    assert "must be 1, true, 0 or false" in problem_for("CANDYGOOGY_IN_CONTAINER", value)


def test_new_settings_never_show_their_values() -> None:
    env = {
        **VALID,
        "HOMELAB_DISKS": "SECRET-Zq9",
        "BACKUP_DIR": "SECRET-Zq9",
        "INTERNET_CHECK_TARGETS": "SECRET-Zq9.example:99999",
        "CANDYGOOGY_IN_CONTAINER": "SECRET-Zq9",
    }
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    assert [p.split()[0].rstrip(":") for p in error.value.problems] == [
        "HOMELAB_DISKS",
        "BACKUP_DIR",
        "INTERNET_CHECK_TARGETS",
        "CANDYGOOGY_IN_CONTAINER",
    ]
    assert "Zq9" not in str(error.value) and "SECRET" not in str(error.value)


# --- Docker, the host's root, the image's commit ------------------------------------------


def test_container_settings_defaults() -> None:
    loaded = load_settings(VALID)
    assert loaded.docker_host == "unix:///var/run/docker.sock"
    assert loaded.homelab_host_root is None
    assert loaded.commit is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("unix:///var/run/docker.sock", "unix:///var/run/docker.sock"),
        (" unix:///run/docker-proxy//docker.sock ", "unix:///run/docker-proxy/docker.sock"),
        ("tcp://192.0.2.5:2375", "tcp://192.0.2.5:2375"),
        ("tcp://docker-proxy.example:2375/", "tcp://docker-proxy.example:2375"),
        ("tcp://[2001:db8::1]:2375", "tcp://[2001:db8::1]:2375"),
        ("", "unix:///var/run/docker.sock"),
    ],
)
def test_docker_host(value: str, expected: str) -> None:
    assert load_settings({**VALID, "DOCKER_HOST": value}).docker_host == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("ssh://user@192.0.2.5", "must be unix:///<socket path> or tcp://<host>:<port>"),
        ("npipe:////./pipe/docker_engine", "must be unix:///<socket path> or tcp://<host>:<port>"),
        ("http://192.0.2.5:2375", "must be unix:///<socket path> or tcp://<host>:<port>"),
        ("/var/run/docker.sock", "must be unix:///<socket path> or tcp://<host>:<port>"),
        ("UNIX:///var/run/docker.sock", "must be unix:///<socket path> or tcp://<host>:<port>"),
        ("unix://var/run/docker.sock", "has a socket path that must be an absolute path"),
        ("unix:///run/../docker.sock", "has a socket path that can't contain .."),
        ("tcp://192.0.2.5", "must be host:port"),
        ("tcp://192.0.2.5:0", "needs a port from 1 to 65535"),
        ("tcp://bad_host:2375", "has an invalid host name"),
        ("tcp://2001:db8::1:2375", "needs [ ] around an IPv6 address"),
    ],
)
def test_bad_docker_host(value: str, message: str) -> None:
    assert message in problem_for("DOCKER_HOST", value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("/host", "/host"), ("/host/", "/host"), (" /mnt//host ", "/mnt/host"), ("", None)],
)
def test_homelab_host_root(value: str, expected: str | None) -> None:
    assert load_settings({**VALID, "HOMELAB_HOST_ROOT": value}).homelab_host_root == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("host", "must be an absolute path, starting with /"),
        ("/host/../etc", "can't contain .."),
        ("/host\x07", "can't contain control characters"),
    ],
)
def test_bad_homelab_host_root(value: str, message: str) -> None:
    assert message in problem_for("HOMELAB_HOST_ROOT", value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0123abc", "0123abc"),
        ("0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"),
        (" 0123abc ", "0123abc"),
        ("", None),
    ],
)
def test_commit(value: str, expected: str | None) -> None:
    assert load_settings({**VALID, "CANDYGOOGY_COMMIT": value}).commit == expected


@pytest.mark.parametrize(
    "value",
    [
        "unknown",
        "0123ab",  # 6
        "0123456789abcdef0123456789abcdef012345678",  # 41
        "0123ABC",
        "0123abg",
        "0123 abc",
        "０１２３abc",
        "smoke-0123abcd",
    ],
)
def test_bad_commit(value: str) -> None:
    assert "must be a commit hash: 7 to 40 of 0-9 a-f" in problem_for("CANDYGOOGY_COMMIT", value)


def test_container_settings_never_show_their_values() -> None:
    env = {
        **VALID,
        "HOMELAB_HOST_ROOT": "SECRET-Zq9",
        "DOCKER_HOST": "tcp://SECRET-Zq9",
        "CANDYGOOGY_COMMIT": "SECRET-Zq9",
    }
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    assert [p.split()[0] for p in error.value.problems] == [
        "HOMELAB_HOST_ROOT",
        "DOCKER_HOST",
        "CANDYGOOGY_COMMIT",
    ]
    assert "Zq9" not in str(error.value) and "SECRET" not in str(error.value)


def test_config_exposes_the_container_settings() -> None:
    assert config.DOCKER_HOST == config.SETTINGS.docker_host
    assert config.HOMELAB_HOST_ROOT == config.SETTINGS.homelab_host_root
    assert config.BUILD_COMMIT == config.SETTINGS.commit


def test_known_variables_are_exactly_what_the_loader_reads() -> None:
    """KNOWN_VARIABLES is what the Dockerfile and compose files are checked against."""
    source = inspect.getsource(settings.load_settings)
    read = set(re.findall(r'env\.get\(\s*"([A-Z_]+)"', source))
    read |= set(re.findall(r'"([A-Z_]+)" (?:not )?in env', source))
    assert read == set(settings.KNOWN_VARIABLES) | {"AUTHORIZED_USER_ID"}
    assert len(set(settings.KNOWN_VARIABLES)) == len(settings.KNOWN_VARIABLES)


def test_tests_never_see_a_developers_variables() -> None:
    """tests/__init__.py sets or removes every variable the loader reads."""
    unset = set(settings.KNOWN_VARIABLES) - {
        "BOT_TOKEN",
        "AUTHORIZED_USER_IDS",
        "WORKSTATION_MAC",
        "WORKSTATION_IP",
        "SSH_USER",
        "SSH_KEY_PATH",
        "HOMELAB_DISKS",
        "BACKUP_DIR",
        "BACKUP_MAX_AGE_HOURS",
        "DIGEST_TIME",
        "WEEKLY_REPORT",
        "INTERNET_CHECK_TARGETS",
    }
    assert {"DOCKER_HOST", "HOMELAB_HOST_ROOT", "CANDYGOOGY_COMMIT"} <= unset
    for name in unset:
        assert name not in os.environ, name
