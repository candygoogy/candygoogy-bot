"""The release workflow and the shared image smoke tests, checked as text.

Like test_docker.py, these read the files line by line (no YAML parser in the
dev tools). GitHub runs the real thing on a v* tag, or as a dry run by hand.
"""

import re
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS_DIR / "release.yml"
DOCKER = WORKFLOWS_DIR / "docker.yml"
SMOKE = ROOT / ".github" / "scripts" / "smoke-image.sh"
EACH_WORKFLOW = [pytest.param(p, id=p.name) for p in sorted(WORKFLOWS_DIR.glob("*.yml"))]
PUSH_ONLY = "if: github.event_name == 'push'"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_blocks(text: str) -> list[str]:
    """The shell of every `run:` key, inline or as an indented block."""
    blocks: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^( *)(?:- )?run: ?(.*)$", line)
        if not match:
            continue
        indent, value = len(match[1]), match[2]
        if value not in ("|", ">"):
            blocks.append(value)
            continue
        body: list[str] = []
        for following in lines[i + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


def jobs(text: str) -> dict[str, str]:
    """Each job's text, by its id (two-space keys under `jobs:`)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def steps(job: str) -> list[str]:
    return job.split("\n      - ")[1:]


# --- Every workflow ---------------------------------------------------------------------


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_no_expressions_inside_shell(path: Path) -> None:
    """${{ }} in run: is pasted into the script before it runs; values go
    through env: instead, so nothing from a tag or a branch name is code."""
    blocks = run_blocks(read(path))
    assert blocks
    for block in blocks:
        assert "${{" not in block, block


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_workflows_default_to_a_read_only_token(path: Path) -> None:
    assert "\npermissions:\n  contents: read\n\n" in read(path)


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_every_job_has_a_timeout(path: Path) -> None:
    for name, job in jobs(read(path)).items():
        assert "\n    timeout-minutes: " in job, name


@pytest.mark.parametrize("path", EACH_WORKFLOW)
def test_checkouts_keep_no_credentials(path: Path) -> None:
    text = read(path)
    checkouts = text.count("uses: actions/checkout@")
    assert text.count("persist-credentials: false") == checkouts


# --- release.yml ------------------------------------------------------------------------


def test_release_runs_on_version_tags_and_by_hand_only() -> None:
    text = read(RELEASE)
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert on == '  push:\n    tags: ["v*"]\n  workflow_dispatch:'


def test_only_the_pushing_jobs_can_write() -> None:
    by_job = jobs(read(RELEASE))
    assert set(by_job) == {"gate", "build", "merge"}
    assert "permissions:" not in by_job["gate"]
    assert "packages: write" in by_job["build"]
    for permission in ("id-token: write", "attestations: write"):
        assert permission not in by_job["build"]
        assert permission in by_job["merge"]
    assert "packages: write" in by_job["merge"]
    assert "artifact-metadata" not in read(RELEASE)


def test_the_gate_checks_tag_version_and_changelog() -> None:
    gate = jobs(read(RELEASE))["gate"]
    assert "candygoogy/__init__.py" in gate and "CHANGELOG.md" in gate
    assert '"$REF_NAME" != "v$version"' in gate
    assert "REF_NAME: ${{ github.ref_name }}" in gate  # through env:, not pasted
    assert "UNRELEASED" not in gate  # any non-date fails a tag run
    assert "::error::" in gate


def test_both_platforms_are_built_natively() -> None:
    build = jobs(read(RELEASE))["build"]
    assert "platform: linux/amd64" in build and "runner: ubuntu-latest" in build
    assert "platform: linux/arm64" in build and "runner: ubuntu-24.04-arm" in build
    assert "setup-qemu" not in read(RELEASE)


def test_smoke_tests_run_before_anything_is_pushed() -> None:
    build = steps(jobs(read(RELEASE))["build"])
    smoke = next(i for i, s in enumerate(build) if "smoke-image.sh" in s)
    assert "load: true" in build[smoke - 1] and "push: true" not in build[smoke - 1]
    for step in build[: smoke + 1]:
        assert "push=true" not in step and "login-action" not in step
    for step in build[smoke + 1 :]:
        assert PUSH_ONLY in step, step
    assert any("push-by-digest=true" in s for s in build[smoke + 1 :])


def test_the_dry_run_neither_pushes_nor_attests() -> None:
    merge = jobs(read(RELEASE))["merge"]
    assert merge.startswith(f"    name: Publish the multi-arch image\n    {PUSH_ONLY}\n")
    assert "needs: [gate, build]" in merge


def test_the_index_is_tagged_and_attested() -> None:
    merge = jobs(read(RELEASE))["merge"]
    for tag in ("pattern={{version}}", "pattern={{major}}.{{minor}}", "pattern={{major}}"):
        assert f"type=semver,{tag}" in merge
    assert "flavor: latest=auto" in merge  # never latest for a pre-release
    assert "docker buildx imagetools create" in merge
    assert '[ "$platforms" = "linux/amd64,linux/arm64" ]' in merge
    attest = next(s for s in steps(merge) if "uses: actions/attest@" in s)
    assert "subject-digest: ${{ steps.index.outputs.digest }}" in attest
    assert "push-to-registry: true" in attest
    assert "GITHUB_STEP_SUMMARY" in merge


def test_the_image_is_labelled_with_the_release() -> None:
    text = read(RELEASE)
    assert text.count("VERSION=${{ needs.gate.outputs.version }}") == 2  # load and push
    assert text.count("CANDYGOOGY_COMMIT=${{ github.sha }}") == 2
    dockerfile = read(ROOT / "Dockerfile")
    assert "ARG VERSION=" in dockerfile
    assert 'org.opencontainers.image.version="${VERSION}"' in dockerfile


def test_buildkit_is_pinned_by_digest() -> None:
    [image] = cast(list[str], re.findall(r"driver-opts: image=(\S+)", read(RELEASE)))
    assert re.fullmatch(r"moby/buildkit:v[0-9.]+@sha256:[0-9a-f]{64}", image)


# --- smoke-image.sh -----------------------------------------------------------------------


def test_both_image_workflows_share_the_smoke_tests() -> None:
    assert '.github/scripts/smoke-image.sh "$IMAGE"' in read(DOCKER)
    assert ".github/scripts/smoke-image.sh candygoogy-bot:smoke" in read(RELEASE)
    step = read(DOCKER).split("- name: Smoke tests", 1)[1].split("- name:", 1)[0]
    assert "docker run" not in step  # no second copy of the checks


def test_smoke_script_is_strict_and_cleans_up() -> None:
    text = read(SMOKE)
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    assert 'mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/' in text
    assert "trap cleanup EXIT" in text


def test_smoke_script_changes_sysctl_only_on_github_hosted_runners() -> None:
    lines = read(SMOKE).splitlines()
    [sudo] = [i for i, line in enumerate(lines) if "sudo" in line and not line.startswith("#")]
    guard = lines[sudo - 2]
    assert '"${GITHUB_ACTIONS:-}" = true' in guard
    assert '"${RUNNER_ENVIRONMENT:-}" = github-hosted' in guard
    assert "ping_group_range fix" in "\n".join(lines[sudo:])  # elsewhere: fail, change nothing


def test_smoke_script_uses_obvious_fake_values() -> None:
    text = read(SMOKE)
    assert "AUTHORIZED_USER_IDS=1111\n" in text
    assert "BOT_TOKEN=123456:TEST-TOKEN-not-a-real-token-0000\n" in text
