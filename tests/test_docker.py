"""The Dockerfile, .dockerignore, compose files and CI workflows, checked as text.

There is no Docker (and no YAML parser) in the dev tools, so these read the
files line by line. CI's "Docker image" workflow builds and runs the real image.
"""

import re
from pathlib import Path
from typing import cast

import pytest

from candygoogy import dockerapi, settings
from candygoogy.settings import load_settings

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
BUILD_OVERLAY = ROOT / "docker-compose.build.yml"
PROXY_OVERLAY = ROOT / "docker-compose.docker-proxy.yml"
PUBLISHED = "ghcr.io/candygoogy/candygoogy-bot"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
EACH_WORKFLOW = [pytest.param(path, id=path.name) for path in WORKFLOWS]
CI = ROOT / ".github" / "workflows" / "ci.yml"

# Read by the bot but not by load_settings(), or by Python and pip themselves.
OTHER_VARIABLES = {
    "STATE_DIRECTORY",  # config.STATE_DIR
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_ROOT_USER_ACTION",
}
SHA256 = re.compile(r"@sha256:[0-9a-f]{64}")
BOT_UID = "10001"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def found(pattern: str, text: str) -> list[str]:
    """re.findall for a pattern with at most one group (multi-line ^ and $)."""
    return cast(list[str], re.findall(pattern, text, re.MULTILINE))


def instructions(name: str) -> list[str]:
    """The Dockerfile's `name` instructions, continuation lines joined."""
    text = re.sub(r"\\\n", " ", read(DOCKERFILE))
    return [line[len(name) + 1 :] for line in text.splitlines() if line.startswith(f"{name} ")]


def dockerfile_env() -> dict[str, str]:
    [env] = instructions("ENV")
    return dict(re.findall(r"([A-Z_][A-Z0-9_]*)=(\S*)", env))


def uncommented(path: Path) -> str:
    return "\n".join(line for line in read(path).splitlines() if not line.lstrip().startswith("#"))


# --- Dockerfile --------------------------------------------------------------------


def test_dockerfile_env_names_are_known() -> None:
    env = dockerfile_env()
    assert {"CANDYGOOGY_IN_CONTAINER", "STATE_DIRECTORY", "SSH_KNOWN_HOSTS"} <= set(env)
    assert set(env) <= set(settings.KNOWN_VARIABLES) | OTHER_VARIABLES


def test_dockerfile_env_values_load() -> None:
    """What the image sets passes the loader (the commit as CI builds it)."""
    env = dockerfile_env()
    assert env["CANDYGOOGY_IN_CONTAINER"] == "1"
    assert env["CANDYGOOGY_COMMIT"] == "${CANDYGOOGY_COMMIT}"  # the build arg
    assert env["SSH_KNOWN_HOSTS"].startswith(env["STATE_DIRECTORY"] + "/")
    image = {k: v for k, v in env.items() if k in settings.KNOWN_VARIABLES}
    image["CANDYGOOGY_COMMIT"] = "0123456789abcdef0123456789abcdef01234567"
    base = {
        "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
        "AUTHORIZED_USER_IDS": "1111",
        "WORKSTATION_MAC": "00:00:5e:00:53:01",
        "WORKSTATION_IP": "192.0.2.10",
        "SSH_USER": "tester",
    }
    loaded = load_settings({**base, **image})
    assert loaded.in_container is True
    assert loaded.ssh_known_hosts == env["SSH_KNOWN_HOSTS"]
    # Built without the arg, the variable is empty, which means "unknown".
    assert load_settings({**base, **image, "CANDYGOOGY_COMMIT": ""}).commit is None


def test_dockerfile_base_is_pinned_and_matches_ci() -> None:
    """The image's Python is the newest one CI tests."""
    [base] = instructions("FROM")
    match = re.fullmatch(r"python:([0-9]+\.[0-9]+)-slim-trixie@sha256:[0-9a-f]{64}", base)
    assert match, base
    matrix = re.search(r"python: \[([^\]]*)\]", read(CI))
    assert matrix
    versions = [v.strip().strip('"') for v in matrix[1].split(",")]
    newest = max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
    assert match[1] == newest


def test_dockerfile_installs_only_the_tools_it_runs() -> None:
    [apt] = [r for r in instructions("RUN") if "apt-get install" in r]
    packages = apt.split("--no-install-recommends")[1].split(";")[0].split()
    assert packages == ["iputils-ping", "iproute2", "openssh-client", "tini"]
    assert "wakeonlan" not in read(DOCKERFILE)  # the magic packet is sent from Python
    # The build fails if ping is setuid/setgid or has a file capability.
    assert "S_ISUID | stat.S_ISGID" in apt and "'security.capability' not in" in apt


def test_dockerfile_pip_is_locked_and_binary_only() -> None:
    [pip] = [r for r in instructions("RUN") if "pip install" in r]
    for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-cache-dir"):
        assert flag in pip
    assert "-r requirements.txt" in pip and "pip check" in pip


def test_dockerfile_runs_as_the_fixed_user_under_tini() -> None:
    assert instructions("USER") == ["${BOT_UID}:${BOT_GID}"]
    assert f"ARG BOT_UID={BOT_UID}" in read(DOCKERFILE)
    assert f"ARG BOT_GID={BOT_UID}" in read(DOCKERFILE)
    assert instructions("ENTRYPOINT") == ['["/usr/bin/tini", "--"]']
    assert instructions("CMD") == ['["python", "bot.py"]']
    assert instructions("HEALTHCHECK") == [] and instructions("VOLUME") == []


def test_dockerfile_copies_only_what_dockerignore_lets_in() -> None:
    allowed = {line[1:] for line in read(DOCKERIGNORE).splitlines() if line.startswith("!")}
    copied: list[str] = []
    for copy in instructions("COPY"):
        *sources, _destination = [w for w in copy.split() if not w.startswith("--")]
        copied += sources
    for source in copied:
        assert source in allowed, source
    assert "LICENSE" in copied  # the image carries its license


def test_dockerfile_normalises_the_apps_file_modes() -> None:
    """COPY keeps the build context's modes: a checkout made with umask 077 would
    leave /app unreadable for UID 10001. After the copies (and compileall), /app
    is root's, readable by everyone and writable by no one else."""
    runs = instructions("RUN")
    [fix] = [r for r in runs if "chmod -R" in r]
    assert "chown -R root:root /app" in fix and "chmod -R u=rwX,go=rX /app" in fix
    assert fix.index("compileall") < fix.index("chmod")
    text = re.sub(r"\\\n", " ", read(DOCKERFILE))
    lines = text.splitlines()
    last_copy = max(i for i, line in enumerate(lines) if line.startswith("COPY "))
    fix_line = next(i for i, line in enumerate(lines) if "chmod -R" in line)
    user_line = next(i for i, line in enumerate(lines) if line.startswith("USER "))
    assert last_copy < fix_line < user_line


def test_ci_builds_from_a_private_copy() -> None:
    """The umask-077 case runs for real in CI: built from a chmod -R go-rwx copy,
    the bot must still read its own code."""
    text = read(ROOT / ".github" / "workflows" / "docker.yml")
    step = text.split("- name: Build from a private copy", 1)[1].split("- name:", 1)[0]
    for part in (
        'git archive --format=tar HEAD | tar -x -C "$copy"',
        'chmod -R go-rwx "$copy"',
        'docker build --build-arg CANDYGOOGY_COMMIT="$GITHUB_SHA" -t "$IMAGE-private" "$copy"',
        "python bot.py --check-config",
        '[ "$out" = "OK: the configuration is valid." ]',
    ):
        assert part in step, part


# --- .dockerignore -------------------------------------------------------------------


def test_dockerignore_excludes_everything_but_the_code() -> None:
    patterns = [
        line for line in read(DOCKERIGNORE).splitlines() if line and not line.startswith("#")
    ]
    assert patterns[0] == "*"  # everything out first
    let_in = {p[1:] for p in patterns if p.startswith("!")}
    assert let_in == {"bot.py", "requirements.txt", "LICENSE", "candygoogy/"}
    # Never let back in, even inside the package; these come after the "!" lines.
    for secret in ("**/.env", "**/*.key", "**/__pycache__"):
        assert secret in patterns
        assert patterns.index(secret) > max(i for i, p in enumerate(patterns) if p.startswith("!"))


# --- Compose files ----------------------------------------------------------------------


def compose_variables(path: Path) -> set[str]:
    """Upper-case keys (environment and build args), commented-out ones too."""
    return set(found(r"^\s+(?:#\s*)?([A-Z][A-Z0-9_]*):", read(path)))


@pytest.mark.parametrize(
    "path", [pytest.param(p, id=p.name) for p in (COMPOSE, BUILD_OVERLAY, PROXY_OVERLAY)]
)
def test_compose_variables_are_known(path: Path) -> None:
    names = compose_variables(path)
    assert names
    assert names <= set(settings.KNOWN_VARIABLES)


def test_compose_runs_the_bot_locked_down() -> None:
    text = uncommented(COMPOSE)
    for line in (
        "network_mode: host",
        "env_file: .env",
        f'user: "{BOT_UID}:{BOT_UID}"',
        "read_only: true",
        "cap_drop: [ALL]",
        "- no-new-privileges:true",
        "restart: unless-stopped",
        "- state:/var/lib/candygoogy-bot",
        "- ./ssh:/etc/candygoogy/ssh:ro",
        'max-size: "10m"',
    ):
        assert line in text, line
    assert "cap_add" not in text and "privileged" not in text and "group_add" not in text


def test_compose_runs_the_published_image() -> None:
    """Pulled by default: the latest 1.x unless CANDYGOOGY_TAG pins a version or digest."""
    text = uncommented(COMPOSE)
    assert found(r"^    image: (\S+)$", text) == [f"{PUBLISHED}:${{CANDYGOOGY_TAG:-1}}"]
    assert "build:" not in text
    assert "CANDYGOOGY_TAG=1.0.0@sha256:" in read(COMPOSE)  # how to pin a digest


def test_build_overlay_never_takes_the_published_name() -> None:
    text = uncommented(BUILD_OVERLAY)
    assert "    build:\n      context: .\n" in text
    assert "CANDYGOOGY_COMMIT: ${CANDYGOOGY_COMMIT:-}" in text
    assert found(r"^    image: (\S+)$", text) == ["candygoogy-bot:local"]
    assert "pull_policy: build" in text
    assert "ghcr.io" not in text


def test_ci_checks_every_compose_file() -> None:
    step = read(ROOT / ".github" / "workflows" / "docker.yml").split(
        "- name: Compose files are valid", 1
    )[1]
    for path in (COMPOSE, BUILD_OVERLAY, PROXY_OVERLAY):
        assert f"-f {path.name}" in step, path.name
    assert f"= {PUBLISHED}:1 ]" in step and "= candygoogy-bot:local ]" in step


def test_compose_never_mounts_docker_or_the_host_root() -> None:
    text = uncommented(COMPOSE)
    assert "docker.sock" not in text
    assert "/host" not in text  # HOMELAB_HOST_ROOT's mount is opt-in (commented out)
    assert "target: /host" in read(COMPOSE)


def test_compose_ssh_key_path_is_in_the_mounted_directory() -> None:
    [key] = found(r"^\s+SSH_KEY_PATH: (\S+)", read(COMPOSE))
    assert key.startswith("/etc/candygoogy/ssh/")


def test_compose_setup_lets_the_container_read_the_key() -> None:
    """The folder and the key must be readable by GID 10001 (the container's),
    which a plain `mkdir` (owned by the host user's group) isn't."""
    text = read(COMPOSE)
    assert f"sudo install -d -o root -g {BOT_UID} -m 750 ssh" in text
    assert f"sudo install -o root -g {BOT_UID} -m 640 <your key> ssh/id_ed25519" in text
    assert "mkdir" not in text


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_workflows_use_obvious_fake_user_ids(path: Path) -> None:
    """No plausible real Telegram ID, even in a fake config."""
    for ids in found(r"AUTHORIZED_USER_IDS=(\S+)", read(path)):
        assert ids == "1111", ids


def test_proxy_overlay_is_pinned_and_locked_down() -> None:
    text = uncommented(PROXY_OVERLAY)
    [image] = found(r"image: (\S+)", text)
    assert re.fullmatch(
        r"wollomatic/socket-proxy:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}", image
    )
    for line in ("network_mode: none", "read_only: true", "cap_drop: [ALL]"):
        assert line in text
    assert "-allowPOST" not in text and "-allowDELETE" not in text and "-allowPUT" not in text
    assert "DOCKER_HOST: unix:///run/docker-proxy/docker.sock" in text
    assert "- docker-proxy:/run/docker-proxy:ro" in text  # the bot's side is read-only
    assert f"uid={BOT_UID}" in text and f'user: "{BOT_UID}:' in text


def proxy_allow_get() -> re.Pattern[str]:
    [pattern] = found(r'"-allowGET=(.*)"', read(PROXY_OVERLAY))
    # YAML's "\\." is the regex \. ; the proxy anchors the pattern itself.
    return re.compile(pattern.replace("\\\\", "\\"))


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        (dockerapi.LIST_PATH, True),
        ("/v1.44/containers/json", True),
        ("/v1.52/containers/json", True),
        ("/containers/abc/json", False),  # inspect: other containers' environment
        ("/containers/json/x", False),
        ("/v1.44/containers/abc/logs", False),
        ("/info", False),
        ("/_ping", False),
        ("/v2.0/containers/json", False),
        ("/x/containers/json", False),
    ],
)
def test_proxy_allows_only_the_container_list(path: str, allowed: bool) -> None:
    assert (proxy_allow_get().fullmatch(path) is not None) is allowed


def test_bot_asks_for_the_list_with_get_only() -> None:
    """The proxy only allows GET; dockerapi.py never sends anything else."""
    source = read(ROOT / "candygoogy" / "dockerapi.py")
    assert "client.get(LIST_PATH" in source
    assert not re.search(r"client\.(post|put|delete|patch|request|stream)\(", source)


# --- Pins ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [pytest.param(p, id=p.name) for p in (DOCKERFILE, PROXY_OVERLAY, *WORKFLOWS)]
)
def test_no_placeholder_pins(path: Path) -> None:
    assert "REPLACE_WITH" not in read(path)


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_actions_are_pinned_by_commit(path: Path) -> None:
    uses = found(r"uses: (\S+)", read(path))
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_images_in_workflows_are_pinned_by_digest() -> None:
    for path in WORKFLOWS:
        for image in found(r"docker run --rm -i (\S+)", read(path)):
            assert SHA256.search(image), image
