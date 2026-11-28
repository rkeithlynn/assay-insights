"""Each image's dependencies and code are two layers, and what is installed did not change.

Owner decision, 2026-11-25. Every image that installs locked dependencies copied them and the
code as one layer, so one edited source file pushed every dependency of every image to ECR
again (26 to 53 MB an image, over about 5 Mbit/s). They are two layers now: dependencies
first, which change only with `uv.lock`, then the code.

The owner's conditions: the image contents and the pinned base image digests must not change.
`tests/unit/image_packages.json` records every distribution each image's interpreter could
see **before** the split, and the Docker tests here build each image and hold it to that
record. (The split itself was also compared file by file, once: every path, mode, owner, size
and SHA-256 in all eight images, before and after. See CLAUDE.md.)

The Docker tests skip without Docker, except in CI (`ASSAY_REQUIRE_IMAGE_TESTS=1`).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_migrate_image import REQUIRED, _docker_available

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "image_packages.py"
_spec = importlib.util.spec_from_file_location("image_packages", SCRIPT)
assert _spec is not None and _spec.loader is not None
tool: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

requires_docker = pytest.mark.skipif(
    not REQUIRED and not _docker_available(), reason="Docker is not available"
)

# The base images, pinned by digest. A change here is a change to every image's contents:
# make it on purpose, with `uv run scripts/image_packages.py --write`, and read what the
# record gained and lost.
PINNED = {
    "ghcr.io/astral-sh/uv:0.13.9"
    "@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d",
    "public.ecr.aws/docker/library/python:3.14-slim"
    "@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f",
    "public.ecr.aws/lambda/python:3.14"
    "@sha256:497df5d229e6d5c54279a6a48e7f597ff386bd02a9b3b37e788bb881108fc081",
}
# Images with no dependencies of their own: the Lambda base image and a few source files.
CODE_ONLY = (
    "api/auth-trigger.Dockerfile",
    "api/cognito-admin.Dockerfile",
    "api/pre-token.Dockerfile",
)
FROM = re.compile(r"^FROM (\S+)(?: AS (\S+))?$", re.MULTILINE)
COPY_FROM_BUILD = re.compile(r"^COPY --from=build(?: (--chmod=\S+))? (/\S+) (\S+)$", re.MULTILINE)


def _text(dockerfile: str) -> str:
    return (REPO_ROOT / dockerfile).read_text(encoding="utf-8")


def test_the_record_covers_every_image_that_installs_dependencies() -> None:
    """A new Dockerfile that installs locked dependencies has to be added to the script, and
    so to the record, before this passes."""
    installing = {
        path.relative_to(REPO_ROOT).as_posix()
        for pattern in ("*/Dockerfile", "*/*.Dockerfile", "tests/*/Dockerfile")
        for path in REPO_ROOT.glob(pattern)
        if "uv export" in path.read_text(encoding="utf-8")
    }
    assert installing == set(tool.IMAGES)
    assert set(tool.recorded()) == set(tool.IMAGES)
    for dockerfile, packages in tool.recorded().items():
        assert packages == sorted(packages) and len(packages) > 3, dockerfile


@pytest.mark.parametrize("dockerfile", [*tool.IMAGES, *CODE_ONLY])
def test_every_base_image_is_one_of_the_pinned_digests(dockerfile: str) -> None:
    stages = {alias for _, alias in FROM.findall(_text(dockerfile)) if alias}
    bases = {image for image, _ in FROM.findall(_text(dockerfile))} - stages - {"scratch"}
    assert bases, dockerfile
    assert bases <= PINNED, f"{dockerfile}: {sorted(bases - PINNED)}"


@pytest.mark.parametrize("dockerfile", list(tool.IMAGES))
def test_dependencies_and_code_are_copied_as_two_layers_in_that_order(dockerfile: str) -> None:
    text = _text(dockerfile)
    final_stage = text[text.rindex("\nFROM ") :]
    copies = COPY_FROM_BUILD.findall(final_stage)
    assert [source for _, source, _ in copies] == ["/deps", "/code"], dockerfile
    # The same destination and the same modes for both: together they are what the one
    # layer was.
    (deps_mode, _, deps_target), (code_mode, _, code_target) = copies
    assert (deps_mode, deps_target) == (code_mode, code_target)
    # The build stage installs the locked, hash-checked wheels on their own, away from the
    # code, so that the dependencies layer changes only when the lock does.
    build_stage = text[: text.rindex("\nFROM ")]
    assert build_stage.count("--target /deps") == 1
    assert "--require-hashes --only-binary :all:" in build_stage
    assert "/out" not in text
    # A COPY may run over several lines; its destination is on the last.
    for line in build_stage.replace("\\\n", " ").splitlines():
        if line.startswith("COPY ") and "--from=" not in line and "/code/" not in line:
            # What else the build stage copies from the context: the manifests, for the lock.
            assert "pyproject.toml" in line, line


@requires_docker
@pytest.mark.parametrize("dockerfile", list(tool.IMAGES))
def test_the_installed_packages_are_what_they_were_before_the_split(dockerfile: str) -> None:
    """Build the image and list every distribution its interpreter can see. The record was
    written from the images as they were before the layers were split."""
    lines = tool.difference(tool.recorded()[dockerfile], tool.built_packages(dockerfile))
    assert not lines, f"{dockerfile}: the installed packages changed:\n" + "\n".join(lines)


def test_a_difference_names_what_was_gained_and_lost() -> None:
    before = ["boto3==1.44.9", "psycopg==3.2.9"]
    assert tool.difference(before, before) == []
    assert tool.difference(before, ["boto3==1.44.10", "psycopg==3.2.9", "extra==1.0"]) == [
        "- boto3==1.44.9",
        "+ boto3==1.44.10",
        "+ extra==1.0",
    ]
