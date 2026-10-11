"""The files people copy: .env.example, machines.example.toml, the systemd unit,
deploy.sh and the target-side power rules. Checked as text on every OS (bash
itself runs check-env.sh in test_check_env.py, on Linux)."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from candygoogy import oscommands, settings
from candygoogy.settings import load_settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
MACHINES_EXAMPLE = ROOT / "machines.example.toml"
UNIT = ROOT / "deploy" / "candygoogy-bot.service"
DEPLOY = ROOT / "deploy" / "deploy.sh"
REBOOT_RULE = ROOT / "deploy" / "50-candygoogy-reboot.rules"
LINUX_RULE = ROOT / "deploy" / "targets" / "linux-power.rules"
MACOS_SUDOERS = ROOT / "deploy" / "targets" / "macos-sudoers"
DOCS = ROOT / "docs"

# In .env.example but read by Python itself, not by the settings loader.
NOT_THE_LOADERS = {"TZ"}
REQUIRED = {"BOT_TOKEN", "AUTHORIZED_USER_IDS"}
# check-env.sh's names(): NAME=... or export NAME=..., at the start of a line.
ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", re.MULTILINE)
DOCUMENTED = re.compile(r"^#? ?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def found(pattern: re.Pattern[str], text: str) -> set[str]:
    return set(cast(list[str], pattern.findall(text)))


def strings(pattern: str, text: str) -> list[str]:
    """re.findall for a pattern with at most one group."""
    return cast(list[str], re.findall(pattern, text))


def values(text: str) -> dict[str, str]:
    """NAME=value from every line, commented or not (the last one wins)."""
    pairs = cast(
        list[tuple[str, str]], re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=(.*)$", text, re.MULTILINE)
    )
    return dict(pairs)


# --- .env.example -------------------------------------------------------------------


def test_env_example_documents_every_variable_the_loader_reads() -> None:
    documented = found(DOCUMENTED, read(ENV_EXAMPLE))
    assert set(settings.KNOWN_VARIABLES) <= documented
    assert documented - set(settings.KNOWN_VARIABLES) == NOT_THE_LOADERS


def test_env_example_requires_only_the_token_and_the_users() -> None:
    """What check-env.sh enforces: the uncommented names, so a MACHINES_FILE-only
    setup isn't asked for WORKSTATION_*. The
    loader enforces "a machine or MACHINES_FILE"."""
    assert found(ASSIGNMENT, read(ENV_EXAMPLE)) == REQUIRED


def test_env_example_placeholders_fail_until_replaced() -> None:
    """Copied unchanged, the required values are rejected, by name only."""
    env = {name: value for name, value in values(read(ENV_EXAMPLE)).items() if name in REQUIRED}
    with pytest.raises(settings.ConfigError) as error:
        _ = load_settings(env)
    problems = error.value.problems
    assert any(p.startswith("BOT_TOKEN doesn't look like a bot token") for p in problems)
    assert any(p.startswith("AUTHORIZED_USER_IDS: entry 1 is not a whole number") for p in problems)


def test_env_example_optional_values_are_valid() -> None:
    """Every commented example passes the loader, so uncommenting one is safe."""
    examples = values(read(ENV_EXAMPLE))
    base = {
        "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
        "AUTHORIZED_USER_IDS": "1111",
    }
    one_pc = {k: examples[k] for k in ("WORKSTATION_MAC", "WORKSTATION_IP", "SSH_USER")}
    optional = {
        k: v
        for k, v in examples.items()
        if k in settings.KNOWN_VARIABLES
        and k not in REQUIRED
        and k not in one_pc
        and k != "MACHINES_FILE"
    }
    loaded = load_settings({**base, **one_pc, **optional})
    assert loaded.machines[0].ip == "192.0.2.10"
    assert loaded.homelab_disks == ("/", "/srv/data")
    assert loaded.commit is not None and loaded.in_container


def test_env_example_has_no_real_looking_values() -> None:
    text = read(ENV_EXAMPLE)
    assert "/home/" not in text
    # Documentation MACs and IPs only (RFC 7042, RFC 5737), and the public resolvers.
    for mac in strings(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", text):
        assert mac.startswith("00:00:5e:00:53:"), mac
    for ip in strings(r"(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(?![0-9.])", text):
        assert ip.startswith("192.0.2.") or ip in ("1.1.1.1", "9.9.9.9"), ip


# --- machines.example.toml --------------------------------------------------------------


def test_machines_example_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/nonexistent")  # ~ in the default key path, on any OS
    env = {
        "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
        "AUTHORIZED_USER_IDS": "1111",
        "MACHINES_FILE": str(MACHINES_EXAMPLE),
        "SSH_KEY_PATH": "/etc/candygoogy/ssh/id_ed25519",
    }
    desk, nas, mini = load_settings(env).machines
    assert (desk.name, desk.os, desk.ssh_key_path) == (
        "desk",
        "windows",
        "/etc/candygoogy/ssh/id_ed25519",
    )
    assert (nas.os, nas.ssh_key_path, nas.ssh_port) == (
        "linux",
        "/etc/candygoogy/ssh/nas_ed25519",
        2222,
    )
    assert (mini.os, mini.wol_broadcast) == ("macos", "192.0.2.255")


def test_machines_example_mentions_every_key() -> None:
    text = read(MACHINES_EXAMPLE)
    for key in settings.MACHINE_KEYS:
        assert f"#   {key} " in text, key


# --- The systemd unit and deploy.sh ----------------------------------------------------


def unit_settings() -> list[str]:
    return [line for line in read(UNIT).splitlines() if line and not line.startswith(("#", "["))]


def test_unit_layout() -> None:
    lines = unit_settings()
    for expected in (
        "User=candygoogy",
        "WorkingDirectory=/opt/candygoogy-bot",
        "EnvironmentFile=/etc/candygoogy/bot.env",
        "ExecStart=/opt/candygoogy-bot/venv/bin/python bot.py",
        "Environment=SSH_KEY_PATH=/etc/candygoogy/ssh/id_ed25519",
        "StateDirectory=candygoogy-bot",
        # ssh's known_hosts lives in the state directory, so no writable ~/.ssh.
        "Environment=SSH_KNOWN_HOSTS=%S/candygoogy-bot/known_hosts",
        "ProtectHome=yes",
        "ProtectSystem=strict",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",
    ):
        assert expected in lines, expected
    assert not any(line.startswith("ReadWritePaths=") for line in lines)
    assert not any(
        line.startswith(("SupplementaryGroups=", "AmbientCapabilities=C")) for line in lines
    )


def test_unit_does_not_restart_on_config_errors() -> None:
    """bot.py exits 2 for a configuration problem; restarting can't fix that."""
    import bot

    assert bot.CONFIG_ERROR_EXIT == 2
    assert "RestartPreventExitStatus=2" in unit_settings()
    assert "Restart=on-failure" in unit_settings()


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(p, id=p.name)
        for p in (UNIT, DEPLOY, REBOOT_RULE, ENV_EXAMPLE, MACHINES_EXAMPLE, *DOCS.glob("*.md"))
    ],
)
def test_no_personal_paths(path: Path) -> None:
    text = read(path)
    for personal in ("/home/", "workstation_key", "candygoogy@"):
        assert personal not in text, personal


def test_deploy_checks_the_configuration_before_restarting() -> None:
    text = read(DEPLOY)
    assert "ENV_FILE=${CANDYGOOGY_ENV_FILE:-/etc/candygoogy/bot.env}" in text
    assert "--require-hashes" in text
    steps = [
        "check-env.sh .env.example",
        'bot.start(["--check-config"])',
        'sudo systemctl restart "$SERVICE"',
    ]
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions)


# The unit's Environment= as `systemctl show -p Environment --value` prints it.
UNIT_ENV = "SSH_KEY_PATH=/etc/candygoogy/ssh/id_ed25519 SSH_KNOWN_HOSTS=/var/lib/candygoogy-bot/known_hosts"
BOT_ENV = (
    "BOT_TOKEN=123456:TEST-TOKEN-not-a-real-token-0000\nAUTHORIZED_USER_IDS=1111\n"
    + "WORKSTATION_MAC=00:00:5e:00:53:01\nWORKSTATION_IP=192.0.2.10\nSSH_USER=botuser\n"
)


def run_deploy_check(
    tmp_path: Path, bot_env: str, unit_env: str
) -> subprocess.CompletedProcess[str]:
    """deploy.sh's configuration check, run as deploy.sh runs it: its Python
    snippet, the env file and the unit's Environment= value as arguments."""
    script = read(DEPLOY)
    snippet = script.split("./venv/bin/python -c '", 1)[1].split('\' "$ENV_FILE" "$unit_env"', 1)[0]
    env_file = tmp_path / "bot.env"
    _ = env_file.write_text(bot_env, encoding="utf-8")
    # Only what Python itself needs: none of the tests' fake settings.
    keep = ("PATH", "SYSTEMROOT", "HOME", "USERPROFILE")
    return subprocess.run(
        [sys.executable, "-c", snippet, str(env_file), unit_env],
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if k in keep},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def no_repo_dotenv() -> None:
    """bot.py also reads a .env next to it, which would add settings of its own."""
    if (ROOT / ".env").exists():
        pytest.skip("a developer's .env is present next to bot.py")


def test_deploy_reads_the_installed_units_environment() -> None:
    text = read(DEPLOY)
    assert (
        'unit_env=$(systemctl show -p Environment --value "$SERVICE" 2>/dev/null || true)' in text
    )
    assert '\' "$ENV_FILE" "$unit_env" || rc=$?' in text


@pytest.mark.usefixtures("no_repo_dotenv")
def test_deploy_check_uses_the_units_defaults(tmp_path: Path) -> None:
    """The env file has no SSH_KEY_PATH, so the unit's default is what's checked."""
    result = run_deploy_check(tmp_path, BOT_ENV, UNIT_ENV)
    assert (result.returncode, result.stdout) == (0, "OK: the configuration is valid.\n")
    bad_default = UNIT_ENV.replace("/etc/candygoogy/ssh/id_ed25519", "relative/key")
    result = run_deploy_check(tmp_path, BOT_ENV, bad_default)
    assert result.returncode == 2
    assert "- SSH_KEY_PATH must be an absolute path" in result.stderr


@pytest.mark.usefixtures("no_repo_dotenv")
def test_deploy_check_lets_the_env_file_win(tmp_path: Path) -> None:
    bad_default = UNIT_ENV.replace("/etc/candygoogy/ssh/id_ed25519", "relative/key")
    bot_env = BOT_ENV + "SSH_KEY_PATH=/etc/candygoogy/ssh/desk_ed25519\n"
    result = run_deploy_check(tmp_path, bot_env, bad_default)
    assert (result.returncode, result.stderr) == (0, "")


@pytest.mark.usefixtures("no_repo_dotenv")
@pytest.mark.parametrize(
    ("unit_env", "code", "problem"),
    [
        ("", 0, ""),  # no unit installed: the env file alone
        ('SSH_KNOWN_HOSTS="/var/lib/a b/known_hosts"', 2, "- SSH_KNOWN_HOSTS can't contain spaces"),
        ("NOT_AN_ASSIGNMENT SSH_USER=ignored", 0, ""),  # the env file's SSH_USER wins
    ],
)
def test_deploy_check_reads_systemds_quoting(
    tmp_path: Path, unit_env: str, code: int, problem: str
) -> None:
    result = run_deploy_check(tmp_path, BOT_ENV, unit_env)
    assert result.returncode == code
    assert problem in result.stderr


# --- Power rules -------------------------------------------------------------------------


def test_reboot_rule_is_for_the_service_user() -> None:
    [user] = strings(r'subject\.user == "([^"]+)"', read(REBOOT_RULE))
    assert f"User={user}" in unit_settings()


def test_linux_rule_allows_exactly_the_bots_power_actions() -> None:
    text = read(LINUX_RULE)
    allowed = set(strings(r'action\.id == "org\.freedesktop\.login1\.([a-z-]+)"', text))
    verbs = {"poweroff": "power-off", "reboot": "reboot", "suspend": "suspend"}
    commands = (oscommands.LINUX_SHUTDOWN, oscommands.LINUX_REBOOT, oscommands.LINUX_SLEEP)
    actions = {verbs[c.split()[1]] for c in commands}
    assert actions == {"power-off", "reboot", "suspend"}
    assert allowed == actions | {f"{a}-multiple-sessions" for a in actions}
    assert "ignore-inhibit" not in text
    assert strings(r'subject\.user == "([^"]+)"', text) == ["botuser"]


def test_macos_sudoers_allows_exactly_the_bots_commands() -> None:
    rules = [line for line in read(MACOS_SUDOERS).splitlines() if line and line[0] != "#"]
    assert len(rules) == 1
    user, spec = rules[0].split(" ALL=(root) NOPASSWD: ")
    assert user == "botuser"
    commands = (oscommands.MACOS_SHUTDOWN, oscommands.MACOS_REBOOT, oscommands.MACOS_SLEEP)
    assert spec.split(", ") == [c.removeprefix("sudo -n ") for c in commands]


# --- Docs ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["windows-target.md", "linux-target.md", "macos-target.md"])
def test_target_docs_exist_and_restrict_the_key(name: str) -> None:
    text = read(DOCS / name)
    # The whole authorized_keys line, options first (Windows' OpenSSH: no `restrict`).
    options = "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty"
    line = re.compile(
        rf'^from="<bot host IP>",(?:restrict|{options}) ssh-ed25519 AAAA\.\.\. candygoogy-bot$',
        re.MULTILINE,
    )
    assert line.search(text)
    assert "Rollback" in text


@pytest.mark.parametrize("name", ["linux-target.md", "macos-target.md"])
def test_linux_and_macos_docs_say_experimental(name: str) -> None:
    assert read(DOCS / name).startswith(f"# {'A Linux machine' if 'linux' in name else 'A Mac'}")
    assert "> **Experimental.**" in read(DOCS / name).split("\n\n")[1]


def test_denial_hints_point_at_existing_docs() -> None:
    for commands in (oscommands.LINUX, oscommands.MACOS):
        [doc] = strings(r"docs/[a-z-]+\.md", commands.denial_hint)
        assert (ROOT / doc).is_file()


def test_relative_links_in_docs_resolve() -> None:
    for doc in [ROOT / "README.md", ROOT / "SECURITY.md", *DOCS.glob("*.md")]:
        text = read(doc)
        # Markdown links, and the README's HTML header (href=, src=); not URLs.
        targets = strings(r"\]\(([^)#:]+)(?:#[^)]*)?\)", text)
        targets += strings(r'(?:href|src)="([^"#:]+)"', text)
        for target in targets:
            assert (doc.parent / target).exists(), f"{doc.name}: {target}"
    assert "docs/demo.gif" in strings(r'src="([^"]+)"', read(ROOT / "README.md"))
