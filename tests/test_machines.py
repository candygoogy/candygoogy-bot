# pyright: reportAny=false
# MagicMock attributes are Any by design.
"""Several machines: the machines file, per-OS commands and parsers,
per-machine SSH, probes, Wake-on-LAN and power lanes, with sample command outputs."""

import asyncio
import dataclasses
import logging
import os
import re
import socket
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from candygoogy import config, handlers, history, oscommands, power, probes, system, wol
from candygoogy.oscommands import Details, Session
from candygoogy.settings import ConfigError, Machine, load_settings
from tests.helpers import (
    FakeRun,
    FakeSocket,
    busy,
    call,
    confirm_data,
    edits,
    make_button_press,
)

WINDOWS = config.FIRST_MACHINE
LINUX = dataclasses.replace(
    WINDOWS,
    name="nas",
    os="linux",
    mac="00:00:5e:00:53:02",
    ip="192.0.2.20",
    ssh_user="nasbot",
    ssh_port=2222,
)
MACOS = dataclasses.replace(
    WINDOWS,
    name="mini",
    os="macos",
    mac="00:00:5e:00:53:03",
    ip="192.0.2.30",
    ssh_user="botuser",
    wol_broadcast="192.0.2.255",
)


@pytest.fixture
def three_machines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fakes in conftest accept only configured machines."""
    monkeypatch.setattr(config, "MACHINES", (WINDOWS, LINUX, MACOS))


# --- The machines file ----------------------------------------------------------------

BASE = {"BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000", "AUTHORIZED_USER_IDS": "1111"}
# The same three machines as machines.example.toml.
EXAMPLE = """
# The machines the bot controls. The table name is a short ID shown on buttons:
# 1-16 of a-z 0-9 and "-" (not at the start or end).

[machines.desk]
os = "windows"
mac = "00:00:5e:00:53:01"
ip = "192.0.2.10"
ssh_user = "botuser"
# ssh_key_path defaults to SSH_KEY_PATH, then ~/.ssh/id_ed25519.

[machines.nas]                      # experimental: linux
os = "linux"
mac = "00:00:5e:00:53:02"
ip = "192.0.2.20"
ssh_user = "nasbot"
ssh_key_path = "/etc/candygoogy/ssh/nas_ed25519"
ssh_port = 2222

[machines.mini]                     # experimental: macos (wakes from sleep only)
os = "macos"
mac = "00:00:5e:00:53:03"
ip = "192.0.2.30"
ssh_user = "botuser"
wol_broadcast = "192.0.2.255"
"""


def machines_file(tmp_path: Path, text: str | bytes) -> str:
    path = tmp_path / "machines.toml"
    if isinstance(text, bytes):
        _ = path.write_bytes(text)
    else:
        _ = path.write_text(text, encoding="utf-8")
    return str(path)


def load_file(tmp_path: Path, text: str | bytes, **env: str) -> tuple[Machine, ...]:
    return load_settings({**BASE, "MACHINES_FILE": machines_file(tmp_path, text), **env}).machines


def file_problems(tmp_path: Path, text: str | bytes, **env: str) -> list[str]:
    with pytest.raises(ConfigError) as error:
        _ = load_file(tmp_path, text, **env)
    return error.value.problems


def one(**fields: object) -> str:
    """A [machines.pc] table: a valid Linux machine, with `fields` changed (None removes)."""
    values: dict[str, object] = {
        "os": "linux",
        "mac": "00:00:5e:00:53:02",
        "ip": "192.0.2.20",
        "ssh_user": "botuser",
        **fields,
    }
    lines = ["[machines.pc]"]
    for key, value in values.items():
        if value is None:
            continue
        literal = f'"{value}"' if isinstance(value, str) else str(value).lower()
        lines.append(f"{key} = {literal}")
    return "\n".join(lines) + "\n"


def test_the_example_file(tmp_path: Path) -> None:
    desk, nas, mini = load_file(tmp_path, EXAMPLE)
    default_key = os.path.expanduser("~/.ssh/id_ed25519")
    assert desk == Machine(
        "desk", "windows", "00:00:5e:00:53:01", "192.0.2.10", "botuser", default_key
    )
    assert nas == Machine(
        "nas",
        "linux",
        "00:00:5e:00:53:02",
        "192.0.2.20",
        "nasbot",
        "/etc/candygoogy/ssh/nas_ed25519",
        2222,
    )
    assert mini.os == "macos" and mini.wol_broadcast == "192.0.2.255" and mini.ssh_port == 22


def test_ssh_key_path_env_is_the_files_default(tmp_path: Path) -> None:
    desk, nas, _ = load_file(tmp_path, EXAMPLE, SSH_KEY_PATH="/keys/bot")
    assert desk.ssh_key_path == "/keys/bot"
    assert nas.ssh_key_path == "/etc/candygoogy/ssh/nas_ed25519"  # its own wins


def test_env_only_is_one_windows_machine_named_workstation() -> None:
    assert config.MACHINES == (config.FIRST_MACHINE,)
    assert (WINDOWS.name, WINDOWS.os, WINDOWS.ssh_port, WINDOWS.wol_broadcast) == (
        "workstation",
        "windows",
        22,
        None,
    )


def test_file_and_env_machine_together_is_an_error(tmp_path: Path) -> None:
    found = file_problems(tmp_path, EXAMPLE, WORKSTATION_MAC="00:00:5e:00:53:09")
    assert found == [
        "Use either MACHINES_FILE or WORKSTATION_MAC, WORKSTATION_IP and SSH_USER, not both."
    ]


SECRET = "SECRET-Zq9"


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (b"\xff\xfe not utf-8", "MACHINES_FILE isn't UTF-8 text."),
        (b"x" * (64 * 1024 + 1), "MACHINES_FILE is larger than 64 KiB."),
        (
            f'[machines.pc]\nos = "{SECRET}\n'.encode(),
            "MACHINES_FILE is not valid TOML (line 2, column",
        ),
        (
            f'[machines.pc]\nmac = "{SECRET}"\nmac = "x"\n'.encode(),
            "MACHINES_FILE is not valid TOML (line 3",
        ),
        (b"", "MACHINES_FILE has no [machines.<name>] tables."),
        (b"[other]\nx = 1\n", "MACHINES_FILE may only contain [machines.<name>] tables."),
    ],
    # Named: a 64 KiB parameter would make pytest's own test id too long.
    ids=["not-utf8", "too-large", "bad-toml", "duplicate-key", "empty", "other-table"],
)
def test_unreadable_files(tmp_path: Path, content: bytes, problem: str) -> None:
    found = file_problems(tmp_path, content)
    assert any(p.startswith(problem) for p in found), found
    assert not any(SECRET in p or str(tmp_path) in p for p in found)


def test_bad_toml_reports_the_position_and_nothing_else(tmp_path: Path) -> None:
    """tomllib's message may quote the file; only "(line N, column M)" is kept."""
    found = file_problems(tmp_path, f'[machines.pc]\nos = "{SECRET}\n')
    assert len(found) == 1
    assert re.fullmatch(r"MACHINES_FILE is not valid TOML \(line 2, column [0-9]+\)\.", found[0])


def test_iowait_counts_as_idle() -> None:
    """100 more ticks of iowait in the second sample: 8% busy, not 19%."""
    second = "cpu2=cpu  1093301 2210 301470 47111560 20811 0 9013 0 0 0"
    details = oscommands.parse_linux_details(
        LINUX_DETAILS.replace("cpu2=cpu  1093301 2210 301470 47111560 20711 0 9013 0 0 0", second)
    )
    assert details is not None and details.cpu == pytest.approx(8.0)


def test_a_missing_file_says_so_without_the_path(tmp_path: Path) -> None:
    missing = str(tmp_path / f"{SECRET}.toml")
    with pytest.raises(ConfigError) as error:
        _ = load_settings({**BASE, "MACHINES_FILE": missing})
    assert error.value.problems == ["MACHINES_FILE can't be read."]
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "name",
    ["Desk", "-desk", "desk-", "a" * 17, "all", '"dësk"', "my_pc", '"a.b"', '"a:b"', '"x y"'],
)
def test_bad_machine_names_are_labelled_by_position(tmp_path: Path, name: str) -> None:
    text = EXAMPLE + f'\n[machines.{name}]\nos = "linux"\n'
    found = file_problems(tmp_path, text)
    assert found == [
        "machines entry 4 needs another name: 1-16 of a-z 0-9 and -, not starting or "
        + 'ending with -, and not "all".'
    ]


@pytest.mark.parametrize("name", ["a", "pc", "desk-2", "a" * 16, "0", "x1-y2"])
def test_good_machine_names(tmp_path: Path, name: str) -> None:
    [machine] = load_file(tmp_path, one().replace("[machines.pc]", f"[machines.{name}]"))
    assert machine.name == name


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"os": None}, "machines.pc.os is missing."),
        ({"os": "ios"}, "machines.pc.os must be windows, linux or macos."),
        ({"os": "Linux"}, "machines.pc.os must be windows, linux or macos."),
        ({"mac": None}, "machines.pc.mac is missing."),
        ({"mac": "00:00:5e:00:53"}, "machines.pc.mac must be 6 hex pairs"),
        (
            {"mac": "00:00:00:00:00:00"},
            "machines.pc.mac can't be all zeros, broadcast or multicast.",
        ),
        (
            {"mac": "ff:ff:ff:ff:ff:ff"},
            "machines.pc.mac can't be all zeros, broadcast or multicast.",
        ),
        (
            {"mac": "01:00:5e:00:00:01"},
            "machines.pc.mac can't be all zeros, broadcast or multicast.",
        ),
        ({"ip": None}, "machines.pc.ip is missing."),
        ({"ip": "192.0.2"}, "machines.pc.ip must be an IPv4 or IPv6 address"),
        ({"ip": "fe80::1%eth0"}, "machines.pc.ip can't have a %scope"),
        ({"ip": "0.0.0.0"}, "machines.pc.ip can't be unspecified, multicast or broadcast."),
        ({"ip": "::"}, "machines.pc.ip can't be unspecified, multicast or broadcast."),
        ({"ip": "224.0.0.1"}, "machines.pc.ip can't be unspecified, multicast or broadcast."),
        ({"ip": "255.255.255.255"}, "machines.pc.ip can't be unspecified, multicast or broadcast."),
        ({"ssh_user": None}, "machines.pc.ssh_user is missing."),
        ({"ssh_user": "-oProxyCommand=x"}, "machines.pc.ssh_user must be 1-64"),
        ({"ssh_key_path": ""}, "machines.pc.ssh_key_path is empty"),
        ({"ssh_key_path": "-i"}, "machines.pc.ssh_key_path can't start with -."),
        ({"ssh_key_path": "/keys/%h"}, "machines.pc.ssh_key_path can't contain %, $ or control"),
        (
            {"ssh_key_path": "/keys/${HOME}"},
            "machines.pc.ssh_key_path can't contain %, $ or control",
        ),
        ({"ssh_key_path": "keys/bot"}, "machines.pc.ssh_key_path must be an absolute path"),
        ({"ssh_port": 0}, "machines.pc.ssh_port must be from 1 to 65535."),
        ({"ssh_port": 65536}, "machines.pc.ssh_port must be from 1 to 65535."),
        ({"ssh_port": True}, "machines.pc.ssh_port must be a whole number, without quotes."),
        ({"ssh_port": "22"}, "machines.pc.ssh_port must be a whole number, without quotes."),
        ({"wol_broadcast": "192.0.2"}, "machines.pc.wol_broadcast must be an IPv4 address"),
        ({"wol_broadcast": "ff02::1"}, "machines.pc.wol_broadcast must be an IPv4 address"),
        ({"wol_broadcast": "0.0.0.0"}, "machines.pc.wol_broadcast can't be 0.0.0.0."),
        ({"ip": 10}, "machines.pc.ip must be text in quotes."),
        ({"os": 1}, "machines.pc.os must be text in quotes."),
        ({"ssh_usr": "botuser"}, "machines.pc has an unknown key 'ssh_usr'."),
        ({"X-Secret": "x"}, "machines.pc has an unknown key."),
    ],
)
def test_each_machine_rule(tmp_path: Path, fields: dict[str, object], problem: str) -> None:
    found = file_problems(tmp_path, one(**fields))
    assert len(found) == 1 and found[0].startswith(problem), found


def test_no_value_is_ever_echoed(tmp_path: Path) -> None:
    text = one(
        mac=SECRET,
        ip=SECRET,
        ssh_user=f"-{SECRET}",
        ssh_key_path=f"%{SECRET}",
        wol_broadcast=SECRET,
    ).replace('os = "linux"', f'os = "{SECRET}"')
    found = file_problems(tmp_path, text)
    assert len(found) == 6
    assert not any(SECRET in p for p in found)


def test_machine_count_limits(tmp_path: Path) -> None:
    eleven = "".join(
        one(mac=f"00:00:5e:00:53:{n:02x}", ip=f"192.0.2.{n}").replace("pc]", f"pc{n}]")
        for n in range(1, 12)
    )
    assert "MACHINES_FILE lists more than 10 machines." in file_problems(tmp_path, eleven)
    ten = eleven.split("[machines.pc11]")[0]
    assert len(load_file(tmp_path, ten)) == 10
    assert file_problems(tmp_path, "[machines]\n") == [
        "MACHINES_FILE has no [machines.<name>] tables."
    ]


def test_ip_and_mac_must_be_unique(tmp_path: Path) -> None:
    text = (
        one()
        + one(ip="192.0.2.21").replace("pc]", "pc2]")
        + one(mac="00:00:5e:00:53:09").replace("pc]", "pc3]")
    )
    assert file_problems(tmp_path, text) == [
        "machines.pc3.ip is the same as machines.pc.ip.",
        "machines.pc2.mac is the same as machines.pc.mac.",
    ]


def test_a_machine_that_is_not_a_table(tmp_path: Path) -> None:
    assert file_problems(tmp_path, '[machines]\npc = "desk"\n') == [
        "machines.pc must be a table, [machines.pc]."
    ]


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"WORKSTATION_MAC": "01:00:5e:00:00:01"}, "WORKSTATION_MAC can't be all zeros"),
        ({"WORKSTATION_MAC": "00:00:00:00:00:00"}, "WORKSTATION_MAC can't be all zeros"),
        ({"WORKSTATION_IP": "224.0.0.1"}, "WORKSTATION_IP can't be unspecified"),
        ({"SSH_KEY_PATH": "/keys/%u"}, "SSH_KEY_PATH can't contain %, $"),
    ],
)
def test_extra_rules_apply_to_the_env_machine(env: dict[str, str], problem: str) -> None:
    valid = {
        **BASE,
        "WORKSTATION_MAC": "00:00:5e:00:53:01",
        "WORKSTATION_IP": "192.0.2.10",
        "SSH_USER": "tester",
    }
    with pytest.raises(ConfigError) as error:
        _ = load_settings({**valid, **env})
    [found] = error.value.problems
    assert found.startswith(problem)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("/srv/ssh/known_hosts", "/srv/ssh/known_hosts"), ("", None), (" ", None)],
)
def test_ssh_known_hosts(value: str, expected: str | None) -> None:
    env = {
        **BASE,
        "WORKSTATION_MAC": "00:00:5e:00:53:01",
        "WORKSTATION_IP": "192.0.2.10",
        "SSH_USER": "tester",
        "SSH_KNOWN_HOSTS": value,
    }
    assert load_settings(env).ssh_known_hosts == expected


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("known_hosts", "SSH_KNOWN_HOSTS must be an absolute path"),
        ("/srv/my hosts", "SSH_KNOWN_HOSTS can't contain spaces, % or $."),
        ("/srv/%h", "SSH_KNOWN_HOSTS can't contain spaces, % or $."),
        ("/srv/../hosts", "SSH_KNOWN_HOSTS can't contain .."),
    ],
)
def test_bad_ssh_known_hosts(value: str, problem: str) -> None:
    env = {
        **BASE,
        "WORKSTATION_MAC": "00:00:5e:00:53:01",
        "WORKSTATION_IP": "192.0.2.10",
        "SSH_USER": "tester",
        "SSH_KNOWN_HOSTS": value,
    }
    with pytest.raises(ConfigError) as error:
        _ = load_settings(env)
    [found] = error.value.problems
    assert found.startswith(problem)


# --- SSH per machine ------------------------------------------------------------------


@pytest.mark.usefixtures("three_machines")
def test_ssh_argv_per_machine(fake_run: FakeRun) -> None:
    _ = asyncio.run(system.ssh(LINUX, "shutdown"))
    [args] = fake_run.calls
    assert args[:5] == ("ssh", "-i", LINUX.ssh_key_path, "-p", "2222")
    assert "HostKeyAlias=candygoogy-nas" in args
    assert args[-3:] == ("--", "nasbot@192.0.2.20", oscommands.LINUX_SHUTDOWN)


@pytest.mark.usefixtures("three_machines")
def test_ssh_known_hosts_option(fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    options = ("-o", "UserKnownHostsFile=/srv/ssh/known_hosts")
    monkeypatch.setattr(system, "KNOWN_HOSTS_OPTIONS", options)
    _ = asyncio.run(system.ssh(MACOS, "details"))
    [args] = fake_run.calls
    assert "UserKnownHostsFile=/srv/ssh/known_hosts" in args
    assert args[-1] == oscommands.MACOS_DETAILS


# --- Power commands and what their results mean, per OS ------------------------------

POWER: list[power.PowerName] = ["shutdown", "reboot", "sleep"]


@pytest.mark.usefixtures("three_machines")
@pytest.mark.parametrize("machine", [WINDOWS, LINUX, MACOS], ids=["windows", "linux", "macos"])
@pytest.mark.parametrize("name", POWER)
def test_power_commands_per_os(fake_run: FakeRun, machine: Machine, name: power.PowerName) -> None:
    _ = asyncio.run(power.perform(machine, name))
    assert fake_run.ssh_commands() == [oscommands.commands_for(machine.os).command(name)]


def result_of(
    fake_run: FakeRun, machine: Machine, name: power.PowerName, result: system.RunResult
) -> tuple[str, bool]:
    fake_run.results["ssh"] = result
    return asyncio.run(power.perform(machine, name))


CLOSED = system.RunResult(255, "Connection to 192.0.2.20 closed by remote host.")
TIMEOUT = system.RunResult(-1, "No response within 15s.", timed_out=True)


@pytest.mark.usefixtures("three_machines")
@pytest.mark.parametrize("machine", [WINDOWS, LINUX, MACOS], ids=["windows", "linux", "macos"])
def test_outcomes_shared_by_every_os(fake_run: FakeRun, machine: Machine) -> None:
    text, follow = result_of(fake_run, machine, "shutdown", system.RunResult(0))
    assert "Shutting down" in text and follow
    text, follow = result_of(fake_run, machine, "reboot", CLOSED)
    assert "Rebooting" in text and "closed before it confirmed" in text and follow
    text, follow = result_of(fake_run, machine, "shutdown", TIMEOUT)
    assert "No answer over SSH; checking" in text and follow
    text, follow = result_of(fake_run, machine, "sleep", TIMEOUT)
    assert "Going to sleep" in text and follow
    text, follow = result_of(
        fake_run, machine, "reboot", system.RunResult(255, "Host key verification failed.")
    )
    assert f"candygoogy-{machine.name}" in text and not follow
    text, follow = result_of(fake_run, machine, "reboot", system.RunResult(1, "Disk on fire"))
    assert "Reboot failed" in text and "Disk on fire" in text and not follow


@pytest.mark.usefixtures("three_machines")
def test_sleep_returning_is_early_only_on_windows(fake_run: FakeRun) -> None:
    assert "returned early" in result_of(fake_run, WINDOWS, "sleep", system.RunResult(0))[0]
    text, follow = result_of(fake_run, LINUX, "sleep", system.RunResult(0))
    assert "Going to sleep" in text and follow


@pytest.mark.usefixtures("three_machines")
@pytest.mark.parametrize(
    ("machine", "err", "hint"),
    [
        (LINUX, "Failed to power off system via logind: Access denied", "polkit rule"),
        (LINUX, "Interactive authentication required.", "inhibitor"),
        (MACOS, "sudo: a password is required", "sudoers rule"),
        (MACOS, "Sorry, user botuser may not run sudo on mini.", "sudoers rule"),
        (MACOS, "user botuser is not allowed to execute '/sbin/shutdown -h now'", "sudoers rule"),
    ],
)
def test_denials_get_a_setup_hint(fake_run: FakeRun, machine: Machine, err: str, hint: str) -> None:
    text, follow = result_of(fake_run, machine, "shutdown", system.RunResult(1, err))
    assert "Shutdown not allowed" in text and hint in text and not follow


@pytest.mark.usefixtures("three_machines")
def test_windows_denial_reads_as_before(fake_run: FakeRun) -> None:
    text, _ = result_of(fake_run, WINDOWS, "shutdown", system.RunResult(1, "Access is denied."))
    assert "Shutdown failed" in text and "Access is denied." in text


# --- Details ---------------------------------------------------------------------------

LINUX_DETAILS = """candygoogy-details-v1
uptime=273600.52
load=0.42 0.35 0.30
ncpu=8
memtotal_kb=32768000
memavail_kb=24117248
cpu1=cpu  1093245 2210 301455 47110832 20711 0 9012 0 0 0
cpu2=cpu  1093301 2210 301470 47111560 20711 0 9013 0 0 0
df=/dev/nvme0n1p2  479597248 143421184 312345600  32% /
"""

MACOS_DETAILS = """candygoogy-details-v1
now=1791705600
boottime={ sec = 1791570600, usec = 512345 } Fri Oct  9 18:30:00 2026
waketime={ sec = 1791704530, usec = 104882 } Sun Oct 11 07:42:10 2026
load={ 1.52 1.73 1.80 }
ncpu=8
memsize=17179869184
vm=Mach Virtual Memory Statistics: (page size of 16384 bytes)
vm=Pages free:                               52000.
vm=Pages active:                            380000.
vm=Pages inactive:                          360000.
vm=Pages speculative:                         9000.
vm=Pages throttled:                              0.
vm=Pages wired down:                        150000.
vm=Pages purgeable:                           6000.
vm="Translation faults":                 123456789.
vm=Pages copy-on-write:                    1234567.
vm=Pages zero filled:                     23456789.
vm=Pages reactivated:                        12345.
vm=Pages purged:                             23456.
vm=File-backed pages:                       330000.
vm=Anonymous pages:                         410000.
vm=Pages stored in compressor:              210000.
vm=Pages occupied by compressor:             70000.
vm=Decompressions:                          123456.
vm=Compressions:                            234567.
vm=Pageins:                                 345678.
vm=Pageouts:                                  1234.
vm=Swapins:                                      0.
vm=Swapouts:                                     0.
df=/dev/disk3s5   482797652 301234567 160123456    66% /System/Volumes/Data
cpuline=CPU usage: 7.45% user, 5.12% sys, 87.42% idle
"""


def test_linux_details_sample() -> None:
    assert oscommands.parse_linux_details(LINUX_DETAILS) == Details(
        boot=273600.52,
        cpu=9.0,
        mem_free=24117248 * 1024,
        mem_total=32768000 * 1024,
        disk_free=312345600 * 1024,
        disk_total=479597248 * 1024,
    )


def test_macos_details_sample() -> None:
    details = oscommands.parse_macos_details(MACOS_DETAILS)
    used = (410000 - 6000 + 150000 + 70000) * 16384
    assert details == Details(
        boot=135000,
        cpu=pytest.approx(12.58),  # pyright: ignore[reportArgumentType]
        mem_free=17179869184 - used,
        mem_total=17179869184,
        disk_free=160123456 * 1024,
        disk_total=482797652 * 1024,
        woke=1070,
    )


@pytest.mark.parametrize(
    ("change", "field", "expected"),
    [
        (("memavail_kb=24117248\n", ""), "mem_free", None),  # kernels before 3.14
        (("uptime=273600.52", "uptime=１２"), "boot", None),
        (("uptime=273600.52", "uptime=" + "9" * 400), "boot", None),
        (  # no time passed between the two samples
            (
                "cpu2=cpu  1093301 2210 301470 47111560 20711 0 9013",
                "cpu2=cpu  1093245 2210 301455 47110832 20711 0 9012",
            ),
            "cpu",
            None,
        ),
        (
            ("cpu1=cpu  1093245 2210 301455 47110832 20711 0 9012 0 0 0", "cpu1=cpu 1 2"),
            "cpu",
            None,
        ),
        (("df=/dev/nvme0n1p2  ", "df=//nas/my share/x  "), "disk_free", 312345600 * 1024),
        (("df=/dev/nvme0n1p2  479597248 143421184 312345600  32% /\n", ""), "disk_total", None),
    ],
)
def test_linux_details_edge_cases(
    change: tuple[str, str], field: str, expected: float | None
) -> None:
    old, new = change
    assert old in LINUX_DETAILS
    details = oscommands.parse_linux_details(LINUX_DETAILS.replace(old, new))
    assert details is not None and getattr(details, field) == expected


def test_startup_file_noise_before_the_marker_is_ignored() -> None:
    noisy = "Welcome!\nuptime=1\ncandygoogy-details-v1\nuptime=5\n" + LINUX_DETAILS
    details = oscommands.parse_linux_details(noisy)
    assert details is not None and details.boot == 273600.52  # after the last marker


@pytest.mark.parametrize("out", ["", "garbage", "uptime=5\n"])
def test_details_without_a_marker_are_unreadable(out: str) -> None:
    assert oscommands.parse_linux_details(out) is None
    assert oscommands.parse_macos_details(out) is None


@pytest.mark.parametrize(
    ("change", "field", "expected"),
    [
        (("page size of 16384", "page size of 4096"), "mem_free", 17179869184 - 624000 * 4096),
        (
            ("waketime={ sec = 1791704530, usec = 104882 } Sun Oct 11 07:42:10 2026", "waketime="),
            "woke",
            None,
        ),
        (("waketime={ sec = 1791704530", "waketime={ sec = 0"), "woke", None),
        (("cpuline=CPU usage: 7.45% user, 5.12% sys, 87.42% idle\n", ""), "cpu", None),
        (
            (
                "df=/dev/disk3s5   482797652 301234567 160123456    66% /System/Volumes/Data",
                "df=/dev/disk1s1 1000 500 400 55% /",
            ),
            "disk_free",
            400 * 1024,
        ),
        (("vm=Anonymous pages:                         410000.\n", ""), "mem_free", None),
    ],
)
def test_macos_details_edge_cases(
    change: tuple[str, str], field: str, expected: float | None
) -> None:
    old, new = change
    assert old in MACOS_DETAILS
    details = oscommands.parse_macos_details(MACOS_DETAILS.replace(old, new))
    assert details is not None and getattr(details, field) == expected


@pytest.mark.usefixtures("three_machines")
def test_status_reads_each_os_with_its_own_parser(fake_run: FakeRun) -> None:
    fake_run.results["ping"] = system.RunResult(0)
    fake_run.port22_open = True
    fake_run.by_last_arg[oscommands.LINUX_DETAILS] = system.RunResult(0, out=LINUX_DETAILS)
    fake_run.by_last_arg[oscommands.LINUX_SESSIONS] = system.RunResult(1, "no loginctl")
    text = asyncio.run(probes.machine_status(LINUX))
    assert "Last boot 3d 4h ago." in text
    # (32768000 - 24117248) KiB used = 8.25 GiB, of 31.25 GiB
    assert "CPU 9% · RAM 8.2 GB / 31 GB · / 298 GB free" in text


# --- Sessions --------------------------------------------------------------------------

LINUX_SESSIONS = """candygoogy-sessions-v1
self=14
Id=2
Name=alice
TTY=tty2
Remote=no
RemoteHost=
Type=wayland
Class=user
State=active
IdleHint=no

Id=14
Name=botuser
TTY=
Remote=yes
RemoteHost=192.0.2.5
Type=tty
Class=user
State=active
IdleHint=no

Id=15
Name=botuser
TTY=
Remote=yes
RemoteHost=192.0.2.5
Type=tty
Class=user
State=active
IdleHint=no

Id=9
Name=bob
TTY=pts/1
Remote=yes
RemoteHost=192.0.2.50
Type=tty
Class=user
State=online
IdleHint=yes

Id=c1
Name=gdm
TTY=tty1
Remote=no
RemoteHost=
Type=wayland
Class=greeter
State=online
IdleHint=no
"""


def linux_sessions(out: str, ok: bool = True) -> list[Session] | None:
    return oscommands.parse_linux_sessions(system.RunResult(0 if ok else 1, out=out), "botuser")


def test_linux_sessions_sample() -> None:
    sessions = linux_sessions(LINUX_SESSIONS)
    assert sessions == [Session("alice", "graphical, active"), Session("bob", "remote, idle")]
    assert "192.0.2" not in oscommands.describe_sessions(sessions or [])  # no addresses in chat


def test_linux_sessions_in_any_property_order_and_with_keys_missing() -> None:
    out = "candygoogy-sessions-v1\nself=\nState=active\nClass=user\nName=carol\nId=3\n\nId=4\nName=dave\n"
    assert linux_sessions(out) == [Session("carol", "console, active")]  # dave: no Class


def test_linux_closing_and_manager_sessions_are_left_out() -> None:
    out = (
        "candygoogy-sessions-v1\nself=1\n"
        "Id=5\nName=erin\nClass=user\nState=closing\n\n"
        "Id=6\nName=frank\nClass=manager\nState=active\n"
    )
    assert linux_sessions(out) == []


def test_linux_who_fallback() -> None:
    out = (
        "candygoogy-sessions-v1\nmode=who\n"
        "alice    tty2         2026-10-11 07:58 (tty2)\n"
        "bob      pts/1        2026-10-11 09:15 (192.0.2.50)\n"
    )
    assert linux_sessions(out) == [Session("alice", "terminal"), Session("bob", "remote")]


@pytest.mark.parametrize("out", ["", "garbage\n"])
def test_linux_sessions_unreadable(out: str) -> None:
    assert linux_sessions(out) is None
    assert linux_sessions(LINUX_SESSIONS, ok=False) is None


def test_linux_nobody_signed_in() -> None:
    assert linux_sessions("candygoogy-sessions-v1\nself=\n") == []


MACOS_SESSIONS = """candygoogy-sessions-v1
alice            console      Oct 11 07:58
bob              ttys001      Oct 11 09:15  (192.0.2.50)
hididle=    | |   "HIDIdleTime" = 4200000000
"""


def test_macos_sessions_sample() -> None:
    sessions = oscommands.parse_macos_sessions(system.RunResult(0, out=MACOS_SESSIONS))
    assert sessions == [Session("alice", "console, in use now"), Session("bob", "remote")]


def test_macos_sessions_idle_or_unknown() -> None:
    idle = MACOS_SESSIONS.replace("4200000000", "600000000000")
    sessions = oscommands.parse_macos_sessions(system.RunResult(0, out=idle))
    assert sessions is not None and sessions[0] == Session("alice", "console, idle 10m")
    unknown = MACOS_SESSIONS.split("hididle=")[0]
    sessions = oscommands.parse_macos_sessions(system.RunResult(0, out=unknown))
    assert sessions is not None and sessions[0] == Session("alice", "console")


# --- Booted or resumed --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("boot", "woke", "since_wake", "expected"),
    [
        (160, None, 100, "booted"),  # boot ≤ since + 60
        (161, None, 100, "resumed"),
        (159, None, 100, "booted"),
        (99999, 50, 100, "resumed"),  # macOS: the wake confirms it
        (99999, 161, 100, None),  # macOS: woken before the request, not by it
        (99999, 160, 100, "resumed"),
        (None, None, 100, None),
    ],
)
def test_booted_or_resumed(
    boot: float | None, woke: float | None, since_wake: float, expected: str | None
) -> None:
    assert power.woke_how(boot, woke, since_wake) == expected


# --- /status per OS ------------------------------------------------------------------------


def neigh(state: str, mac: str) -> system.RunResult:
    return system.RunResult(0, out=f"192.0.2.20 dev eth0 lladdr {mac} {state}\n")


@pytest.mark.usefixtures("three_machines")
def test_linux_reachable_arp_is_not_asleep(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE", LINUX.mac)
    text = asyncio.run(probes.machine_status(LINUX))
    assert "Off or asleep" in text and "Probably asleep" not in text
    assert ("ip", "neigh", "show", "192.0.2.20") in fake_run.calls


@pytest.mark.usefixtures("three_machines")
def test_macos_other_mac_may_be_a_sleep_proxy(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE", "00:00:5e:00:53:99")
    text = asyncio.run(probes.machine_status(MACOS))
    assert "Bonjour Sleep Proxy" in text and "IP conflict" not in text


@pytest.mark.usefixtures("three_machines")
def test_windows_other_mac_is_still_an_ip_conflict(fake_run: FakeRun) -> None:
    fake_run.results["ip"] = neigh("REACHABLE", "00:00:5e:00:53:99")
    assert "Possible IP conflict" in asyncio.run(probes.machine_status(WINDOWS))


@pytest.mark.usefixtures("three_machines")
def test_port_probe_uses_the_machines_ssh_port(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = AsyncMock(side_effect=OSError("closed"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert not asyncio.run(probes.ssh_port_open(LINUX))
    connect.assert_awaited_once_with("192.0.2.20", 2222)


@pytest.mark.usefixtures("three_machines")
def test_ssh_closed_names_the_port(fake_run: FakeRun) -> None:
    fake_run.results["ping"] = system.RunResult(0)
    reason = asyncio.run(probes.ssh_unavailable_reason(LINUX))
    assert reason is not None and "port 2222 is closed" in reason


# --- Wake-on-LAN -------------------------------------------------------------------------


def test_magic_packet_bytes() -> None:
    packet = wol.magic_packet("00:00:5e:00:53:01")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes([0x00, 0x00, 0x5E, 0x00, 0x53, 0x01]) * 16


@pytest.mark.parametrize(
    ("machine", "destination"),
    [(WINDOWS, ("255.255.255.255", 9)), (MACOS, ("192.0.2.255", 9))],
    ids=["default", "wol_broadcast"],
)
def test_sender_broadcasts_to_the_machines_address(
    monkeypatch: pytest.MonkeyPatch, machine: Machine, destination: tuple[str, int]
) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []
    sockets: list[FakeSocket] = []

    def open_socket() -> FakeSocket:
        sockets.append(FakeSocket(sent))
        return sockets[-1]

    monkeypatch.setattr(wol, "open_socket", open_socket)
    result = asyncio.run(wol.send_magic_packet(machine))
    assert result.ok
    assert sent == [(wol.magic_packet(machine.mac), destination)]
    assert sockets[0].options == [(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)]


def test_sender_failure_is_plain_and_never_names_the_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(101, "Network is unreachable", WINDOWS.mac)
    monkeypatch.setattr(wol, "open_socket", lambda: FakeSocket([], error))
    result = asyncio.run(wol.send_magic_packet(WINDOWS))
    assert not result.ok and result.code == -1
    assert result.err == "Couldn't send the magic packet: Network is unreachable."
    assert WINDOWS.mac not in result.describe("Wake-on-LAN")


async def post_nowhere(_text: str) -> None:
    """A Post that sends nothing, so start_wake() doesn't hand off a follow-up."""


@pytest.mark.usefixtures("three_machines")
def test_wake_uses_the_machines_broadcast(
    fake_run: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    awake_sequence(False)
    asyncio.run(power.start_wake(MACOS, post_nowhere, lambda coro: coro.close()))
    assert fake_run.calls[-1] == ("wol", MACOS.mac, "192.0.2.255")


@pytest.mark.usefixtures("three_machines", "fake_run")
def test_mac_wake_after_shutdown_warns(awake_sequence: Callable[..., None]) -> None:
    history.remember(MACOS, "shutdown")
    awake_sequence(False)
    posted: list[str] = []

    async def post(text: str) -> None:
        posted.append(text)

    asyncio.run(power.start_wake(MACOS, post, lambda coro: coro.close()))
    assert "Macs usually wake only from sleep" in posted[0]
    posted.clear()
    power.release_all()
    asyncio.run(power.start_wake(LINUX, post, lambda coro: coro.close()))
    assert "Macs" not in posted[0]


# --- One lane per machine -----------------------------------------------------------------


@pytest.mark.usefixtures("three_machines", "fake_run")
def test_a_busy_machine_does_not_block_another(awake_sequence: Callable[..., None]) -> None:
    power.claim(LINUX, "shutdown")
    assert power.busy_message(LINUX) is not None
    assert power.busy_message(WINDOWS) is None and power.busy_message(MACOS) is None
    awake_sequence(False)
    posted: list[str] = []

    async def post(text: str) -> None:
        posted.append(text)

    asyncio.run(power.start_wake(MACOS, post, lambda coro: coro.close()))
    assert "Waking it up" in posted[0]  # not "Busy"
    asyncio.run(power.start_wake(LINUX, post, lambda coro: coro.close()))
    assert "Already shutting nas down" in posted[1]  # several machines: it names it
    power.release(LINUX)
    assert power.busy_message(LINUX) is None


# --- Callback data budget (Telegram: 1-64 bytes) ---------------------------------------

LONGEST_NAME = "a" * 16
TEN_DIGITS = "9999999999"
# Every callback pattern with the longest machine name: the machine is appended last, and only
# when there are several (test_several_machines.py checks the real builders too).
PATTERNS = [
    f"confirm:shutdown:{TEN_DIGITS}:{LONGEST_NAME}",
    f"confirm:reboot:{TEN_DIGITS}:{LONGEST_NAME}",
    f"confirm:sleep:{TEN_DIGITS}:{LONGEST_NAME}",
    "cancel",
    f"sched:shutdown:{TEN_DIGITS}:{TEN_DIGITS}:{LONGEST_NAME}",
    f"sched:wake:{TEN_DIGITS}:{TEN_DIGITS}:{LONGEST_NAME}",
    f"when:shutdown:12h:{LONGEST_NAME}",
    f"timer:cancel:shutdown:{TEN_DIGITS}:{LONGEST_NAME}",
    f"timer:snooze:shutdown:{TEN_DIGITS}:{LONGEST_NAME}",
    "menu:schedule_shutdown",
    f"status:{LONGEST_NAME}",
    f"pick:schedule_shutdown:{LONGEST_NAME}",
    "confirm:homelab_reboot:9999999999",
    "cancel:homelab_reboot",
    "ask:homelab_reboot",
    "logs:errors",
    "cancel:sched",
]


@pytest.mark.parametrize("data", PATTERNS)
def test_callback_data_fits(data: str) -> None:
    assert 1 <= len(data.encode()) <= 64


def test_the_longest_name_is_the_longest_allowed() -> None:
    assert len(LONGEST_NAME) == 16 and LONGEST_NAME.encode() == LONGEST_NAME.encode("ascii")


# --- Startup ------------------------------------------------------------------------------


def test_each_machines_key_is_checked_by_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.WARNING)
    key = tmp_path / "key"
    _ = key.write_text("not really a key")
    readable = dataclasses.replace(LINUX, ssh_key_path=str(key))
    monkeypatch.setattr(config, "MACHINES", (WINDOWS, readable))
    assert not config.check_ssh_key()
    assert "The SSH key for workstation isn't a readable file" in caplog.text
    assert "nas" not in caplog.text and str(key) not in caplog.text


# --- ssh's keepalive giving up ---------------------------------------------------------------

# What ssh prints when ServerAliveInterval/CountMax give up on a frozen session.
KEEPALIVE = system.RunResult(255, "Timeout, server candygoogy-workstation not responding.\r\n")


@pytest.mark.parametrize("name", POWER)
@pytest.mark.parametrize("os_name", ["windows", "linux", "macos"])
def test_keepalive_exit_reads_like_a_timeout(os_name: str, name: power.PowerName) -> None:
    commands = oscommands.commands_for(os_name)
    expected = "asleep" if name == "sleep" else "no_answer"
    assert oscommands.outcome(commands, name, TIMEOUT) == expected
    assert oscommands.outcome(commands, name, KEEPALIVE) == expected


@pytest.mark.parametrize(
    "err",
    [
        "Timeout, server 192.0.2.10 not responding.",  # right words, but not ssh's exit 255
        "ssh: connect to host 192.0.2.10 port 22: Connection timed out",
        "Connection timed out during banner exchange",
    ],
)
def test_other_255s_are_not_the_keepalive(err: str) -> None:
    result = system.RunResult(1 if "Timeout, server" in err else 255, err)
    assert oscommands.outcome(oscommands.WINDOWS, "sleep", result) == "failed"


def test_power_calls_outlast_the_keepalive(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot's own timeout is the fallback: ConnectTimeout 5 + 5s x 2 keepalives < 25."""
    timeouts: list[float] = []

    async def record(machine: Machine, action: str, timeout: float = 15) -> system.RunResult:
        timeouts.append(timeout)
        return await fake_run("ssh", machine.ip, action, timeout=timeout)

    monkeypatch.setattr(power, "ssh", record)
    for name in POWER:
        _ = asyncio.run(power.perform(WINDOWS, name))
    assert timeouts == [power.POWER_SSH_TIMEOUT_SECONDS] * 3
    keepalive_window = 5 + 5 * (2 + 1)  # ConnectTimeout, then ServerAliveInterval x (CountMax + 1)
    assert power.POWER_SSH_TIMEOUT_SECONDS > keepalive_window
    assert "ConnectTimeout=5" in system.SSH_OPTIONS
    assert "ServerAliveInterval=5" in system.SSH_OPTIONS
    assert "ServerAliveCountMax=2" in system.SSH_OPTIONS


def test_windows_sleep_end_to_end(
    workstation_on: FakeRun, awake_sequence: Callable[..., None]
) -> None:
    """A PC that fell asleep: "Going to sleep", then the follow-up says it's asleep."""
    workstation_on.results["ssh"] = KEEPALIVE
    awake_sequence(False)  # quiet once the follow-up looks
    press = make_button_press(confirm_data("sleep"))
    call(handlers.on_button, press)
    texts = edits(press)
    assert any(t.startswith("😴 <b>Going to sleep</b>") for t in texts)
    assert texts[-1].startswith("🌙 <b>Asleep</b>")  # the follow-up ran
    assert not busy()  # and the lane was released after it


def test_shutdown_end_to_end(workstation_on: FakeRun, awake_sequence: Callable[..., None]) -> None:
    workstation_on.results["ssh"] = KEEPALIVE
    awake_sequence(False)
    press = make_button_press(confirm_data("shutdown"))
    call(handlers.on_button, press)
    texts = edits(press)
    assert any("No answer over SSH; checking" in t for t in texts)
    assert texts[-1].startswith("⚫ <b>Off</b>")
    assert not busy()


@pytest.mark.usefixtures("three_machines")
@pytest.mark.parametrize("machine", [WINDOWS, LINUX, MACOS], ids=["windows", "linux", "macos"])
def test_read_only_calls_cut_by_the_keepalive_are_unreadable(
    fake_run: FakeRun, machine: Machine
) -> None:
    fake_run.results["ssh"] = KEEPALIVE
    assert asyncio.run(probes.machine_details(machine)) is None
    assert asyncio.run(probes.signed_in_sessions(machine)) is None
    fake_run.results["ping"] = system.RunResult(0)
    fake_run.port22_open = True
    assert "Couldn't read details over SSH." in asyncio.run(probes.machine_status(machine))
