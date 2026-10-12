"""One version: candygoogy.__version__, pyproject.toml and CHANGELOG.md agree."""

import re
import tomllib
from pathlib import Path
from typing import cast

from candygoogy import __version__

ROOT = Path(__file__).resolve().parent.parent
# "## [1.0.0] - 2026-10-20", or "- UNRELEASED" until the release is tagged.
RELEASE_HEADING = (
    r"^## \[([0-9]+\.[0-9]+\.[0-9]+[^\]]*)\] - ([0-9]{4}-[0-9]{2}-[0-9]{2}|UNRELEASED)$"
)


def changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_is_semver() -> None:
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?", __version__)


def test_pyproject_has_the_same_version() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = cast(dict[str, object], tomllib.loads(text)["project"])
    assert project["version"] == __version__
    assert project["requires-python"] == ">=3.11"


def test_changelog_top_release_is_this_version() -> None:
    headings: list[tuple[str, str]] = re.findall(RELEASE_HEADING, changelog(), re.MULTILINE)
    assert headings, "no release heading in CHANGELOG.md"
    assert headings[0][0] == __version__


def test_changelog_releases_are_newest_first() -> None:
    """Every "## [x]" heading below Unreleased is a release; versions go down and
    dates never go up, so the top one really is the newest."""
    text = changelog()
    names: list[str] = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
    headings: list[tuple[str, str]] = re.findall(RELEASE_HEADING, text, re.MULTILINE)
    assert [v for v, _ in headings] == names[1:]  # each one a well-formed release heading
    versions = [tuple(int(p) for p in v.split("-")[0].split(".")) for v, _ in headings]
    assert versions == sorted(set(versions), reverse=True)
    dates = [d for _, d in headings if d != "UNRELEASED"]
    assert dates == sorted(dates, reverse=True)


def test_changelog_keeps_an_unreleased_section_on_top() -> None:
    first = re.search(r"^## .*$", changelog(), re.MULTILINE)
    assert first is not None and first.group() == "## [Unreleased]"


def test_changelog_links_every_heading() -> None:
    text = changelog()
    names: list[str] = re.findall(r"^## \[([^\]]+)\]", text, re.MULTILINE)
    for name in names:
        assert re.search(rf"^\[{re.escape(name)}\]: https://github\.com/", text, re.MULTILINE)
