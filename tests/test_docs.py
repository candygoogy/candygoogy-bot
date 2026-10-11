"""The README, LICENSE, SECURITY.md and the demo GIF stay true to the code, and
nothing in the repo cites documents that aren't in it."""

import re
from pathlib import Path
from typing import cast

import pytest

from candygoogy import handlers, settings

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
GIF = ROOT / "docs" / "demo.gif"
# The one address anywhere in the docs.
CONTACT = "hello@candygoogy.dev"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strings(pattern: str, text: str) -> list[str]:
    return cast(list[str], re.findall(pattern, text, re.MULTILINE))


def table_after(header: str) -> list[str]:
    """The first cells of the markdown table that starts with `header`."""
    lines = read(README).splitlines()
    start = lines.index(header)
    rows: list[str] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line.split("|")[1].strip())
    return rows


def test_readme_commands_are_the_bots() -> None:
    cells = table_after("| Command | What it does |")
    named = {name for cell in cells for name in strings(r"`/([a-z_]+)", cell)}
    assert named == set(handlers.COMMANDS)


def test_readme_variables_are_the_loaders() -> None:
    cells = table_after("| Variable | Default | Meaning |")
    names = [cell.strip("`") for cell in cells]
    assert len(names) == len(set(names))
    assert set(names) == set(settings.KNOWN_VARIABLES) | {"TZ"}


@pytest.mark.parametrize("text", ["wakeonlan", "docker group", "/home/", "TODO"])
def test_readme_has_none_of(text: str) -> None:
    assert text not in read(README)


def test_readme_contents_links_point_at_headings() -> None:
    text = read(README)
    # GitHub's anchors: lower-case, punctuation dropped, spaces as hyphens.
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in strings(r"^#{2,3} (.+)$", text)
    }
    links = strings(r"\]\(#([a-z0-9-]+)\)", text)
    assert links
    for link in links:
        assert link in anchors, link


def test_readme_shows_the_gif_centred_and_not_too_wide() -> None:
    [width] = strings(r'<img src="docs/demo.gif" width="([0-9]+)"', read(README))
    assert 400 <= int(width) <= 480
    assert "Illustration: a mock chat with fake data." in read(README)


def test_demo_gif_is_small_enough() -> None:
    data = GIF.read_bytes()
    assert data[:6] == b"GIF89a"
    assert len(data) <= 2 * 1024 * 1024
    width = int.from_bytes(data[6:8], "little")
    assert width <= 480


def test_readme_code_blocks_say_their_language() -> None:
    fences = strings(r"^ *```(.*)$", read(README))
    openers = fences[0::2]
    assert openers and all(openers), openers


def test_license_is_mit() -> None:
    text = read(ROOT / "LICENSE")
    assert text.startswith("MIT License\n\nCopyright (c) 2026 candygoogy\n")
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_security_policy() -> None:
    text = read(ROOT / "SECURITY.md")
    assert CONTACT in text
    assert "Don't open a public issue" in text
    assert "latest commit on `main`" in text
    assert "(README.md#security-model)" in text
    assert "## Security model" in read(README)
    # GitHub's private reporting first, email as the alternative.
    assert "**Report a vulnerability**" in text
    assert text.index("Report a vulnerability") < text.index(CONTACT)
    assert f"{REPO_URL}/security/advisories/new" in text


# --- Community files ------------------------------------------------------------------------

REPO_URL = "https://github.com/candygoogy/candygoogy-bot"
GITHUB = ROOT / ".github"
ISSUE_FORMS = [
    GITHUB / "ISSUE_TEMPLATE" / name for name in ("bug_report.yml", "feature_request.yml")
]
EACH_YAML = [
    pytest.param(path, id=path.name)
    for path in [*ISSUE_FORMS, GITHUB / "ISSUE_TEMPLATE" / "config.yml", GITHUB / "dependabot.yml"]
]


def top_level_keys(path: Path) -> list[str]:
    return strings(r"^([a-z_]+):", read(path))


@pytest.mark.parametrize("path", EACH_YAML)
def test_yaml_files_are_well_formed(path: Path) -> None:
    """No YAML parser in the dev tools, so the shape YAML cares about: spaces,
    not tabs, two-space indentation steps, and no duplicate top-level keys.
    GitHub validates the forms themselves when they're pushed."""
    text = read(path)
    assert "\t" not in text
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip(" "))
            assert indent % 2 == 0, line
    keys = top_level_keys(path)
    assert len(keys) == len(set(keys)), keys


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in ISSUE_FORMS])
def test_issue_forms(path: Path) -> None:
    text = read(path)
    assert top_level_keys(path)[:2] == ["name", "description"] and "body" in top_level_keys(path)
    ids = strings(r"^    id: ([a-z-]+)$", text)
    assert ids and len(ids) == len(set(ids))
    # Every item has a type, and every input a label.
    assert len(strings(r"^  - type: ", text)) == len(ids) + text.count("type: markdown")
    assert len(strings(r"^      label: ", text)) == len(ids)


def test_bug_report_asks_for_what_helps() -> None:
    text = read(ISSUE_FORMS[0])
    for asked in ("how does the bot run?", "version", "operating system", "logs", "steps"):
        assert asked in text.lower(), asked
    for option in ("Docker", "systemd", "Locally", "Windows", "Linux", "macOS"):
        assert f"        - {option}" in text, option
    reminder = "**Remove your bot token, Telegram IDs, IPs and MACs first.**"
    assert reminder in text


def test_issue_config_sends_vulnerabilities_elsewhere() -> None:
    text = read(GITHUB / "ISSUE_TEMPLATE" / "config.yml")
    assert "blank_issues_enabled: false" in text
    urls = strings(r"^    url: (\S+)$", text)
    assert urls == [f"{REPO_URL}/security/advisories/new", f"{REPO_URL}#security-model"]


def test_pull_request_template() -> None:
    text = read(GITHUB / "pull_request_template.md")
    assert "## What and why" in text
    for check in ("pytest", "ruff check", "ruff format", "basedpyright", ".env.example"):
        assert check in text, check
    assert "Nothing from a chat reaches a command line" in text


def test_dependabot_covers_actions_and_the_base_image_only() -> None:
    path = GITHUB / "dependabot.yml"
    text = read(path)
    assert top_level_keys(path) == ["version", "updates"]
    assert "version: 2" in text
    ecosystems = strings(r"package-ecosystem: (\S+)", text)
    assert ecosystems == ["github-actions", "docker"]
    assert text.count("interval: weekly") == 2
    assert text.count("update-types: [minor, patch]") == 2


def test_contributing() -> None:
    text = read(ROOT / "CONTRIBUTING.md")
    assert "(README.md#development)" in text and "## Development" in read(README)
    assert "tests/test_subprocess_invariant.py" in text
    assert "(SECURITY.md)" in text
    # The same lock commands as the README.
    commands = strings(r"^uv pip compile .*$", text)
    assert len(commands) == 2
    assert commands == strings(r"^uv pip compile .*$", read(README))


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("README.md", "/opt/candygoogy-bot/venv/bin/pip install --no-cache-dir --require-hashes"),
        ("deploy/candygoogy-bot.service", "venv/bin/pip install --no-cache-dir"),
        ("deploy/deploy.sh", "pip install --quiet --no-cache-dir --require-hashes"),
    ],
)
def test_the_service_users_pip_keeps_no_cache(name: str, line: str) -> None:
    """Its home (the state folder) may not exist yet when pip runs."""
    assert line in read(ROOT / name)


# --- References to documents that aren't in the repo ------------------------------------

# Task, finding and research notes, decision IDs and roles from how the project
# was made. Assembled from parts so this file doesn't match itself.
INTERNAL = re.compile(
    "|".join(
        (
            "TASK" + "-[0-9]",
            "FINDINGS" + "-[0-9]",
            "RESEARCH" + "-",
            r"\b[DR][0-9]{1,2}\b",
            r"\bthe " + "lead" + r"\b",
            r"\bthe " + "worker" + r"\b",
        )
    )
)
SKIPPED_DIRS = {".git", "venv", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache"}
TEXT_SUFFIXES = {".py", ".md", ".yml", ".toml", ".sh", ".service", ".rules", ".txt", ".in"}
TEXT_NAMES = {"Dockerfile", ".dockerignore", ".env.example", ".gitignore", ".gitattributes"}


def repo_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if SKIPPED_DIRS & set(path.relative_to(ROOT).parts) or not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES or path.parent.name == "targets":
            files.append(path)
    return files


def test_no_references_to_internal_documents() -> None:
    files = repo_text_files()
    assert ROOT / "README.md" in files and ROOT / "deploy" / "targets" / "macos-sudoers" in files
    hits = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for path in files
        for n, line in enumerate(read(path).splitlines(), start=1)
        if INTERNAL.search(line)
    ]
    assert hits == []


def test_the_only_email_address_anywhere_is_the_contact() -> None:
    files = repo_text_files()
    assert GITHUB / "ISSUE_TEMPLATE" / "bug_report.yml" in files
    found = {
        f"{path.relative_to(ROOT)}: {address}"
        for path in files
        for address in strings(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", read(path))
        if address != CONTACT
    }
    assert found == set()
