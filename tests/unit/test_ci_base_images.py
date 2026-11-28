"""CI's base images: from the cache, pulled with retries, and never with credentials.

`scripts/ci_base_images.py` runs before the image tests in the `python` job. Amazon ECR
Public answered `429 Too Many Requests` for a pinned base image and failed that job during
build step 5 and on 2026-11-27. Docker is a stand-in here: nothing is pulled.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.test_image_layers import PINNED

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_base_images.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TOO_MANY = (
    "error from registry: unexpected status from GET request to "
    "https://public.ecr.aws/v2/docker/library/python/manifests/sha256:9aaa: 429 Too Many Requests"
)


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_base_images", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = load()


class FakeDocker:
    """Docker's image store, and a registry that can be busy: `failures` pulls fail first."""

    def __init__(self, failures: int = 0, keeps_names_on_load: bool = True) -> None:
        self.failures = failures
        self.keeps_names_on_load = keeps_names_on_load
        self.named: set[str] = set()
        self.calls: list[list[str]] = []

    def pulls(self) -> list[str]:
        return [call[-1] for call in self.calls if call[0] == "pull"]

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(arguments)
        self.calls.append(call)
        status, error = 0, ""
        match call[0]:
            case "image":
                status = 0 if call[-1] in self.named else 1
            case "pull":
                if self.failures > 0:
                    self.failures -= 1
                    status, error = 1, TOO_MANY
                else:
                    self.named.add(call[-1])
            case "save":
                name, target = call[-1], Path(call[call.index("--output") + 1])
                if name in self.named:
                    target.write_text(name, encoding="utf-8")
                else:
                    status, error = 1, "No such image"
            case "load":
                source = Path(call[call.index("--input") + 1])
                content = source.read_text(encoding="utf-8")
                if content == "corrupt":
                    status, error = 1, "unexpected EOF"
                elif self.keeps_names_on_load:
                    self.named.add(content)
        return subprocess.CompletedProcess(call, status, stdout="", stderr=error)


def ensure(docker: FakeDocker, directory: Path, names: Sequence[str]) -> tuple[int, list[float]]:
    waits: list[float] = []
    said: list[str] = []
    status = ci.ensure(directory, names, run=docker, sleep=waits.append, say=said.append)
    return status, waits


# --- Which images ---------------------------------------------------------------------------


def test_the_images_are_the_pinned_ones_of_the_dockerfiles_ci_builds() -> None:
    """Read from the Dockerfiles, and the same three the image tests hold every build to."""
    assert set(ci.images()) == PINNED
    names = {path.relative_to(REPO_ROOT).as_posix() for path in ci.dockerfiles()}
    assert "api/Dockerfile" in names and "workers/worker.Dockerfile" in names
    # The portal's image is built at deploy time only: its Node image is not CI's to fetch.
    assert not [name for name in names if name.startswith("portal/")]
    assert not [name for name in ci.images() if "node" in name]
    # No name is written in the script: they come from the Dockerfiles.
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"sha256:[0-9a-f]{64}", source)
    for name in PINNED:
        assert name.split("@")[0] not in source


def test_every_form_of_a_pinned_from_line_is_found_and_nothing_unpinned(tmp_path: Path) -> None:
    digest = "a" * 64
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "Dockerfile").write_text(
        f"FROM one.test/a:1@sha256:{digest} AS build\n"
        f"FROM --platform=linux/amd64 two.test/b:2@sha256:{digest}\n"
        f"from three.test/c@sha256:{digest}\n"
        "FROM scratch AS empty\nFROM build AS final\nFROM unpinned.test/d:latest\n"
        f"# FROM commented.test/e@sha256:{digest}\n",
        encoding="utf-8",
    )
    (tmp_path / "x" / "other.Dockerfile").write_text(
        f"FROM one.test/a:1@sha256:{digest}\n", encoding="utf-8"
    )
    for skipped in ("portal", "node_modules", ".venv"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "Dockerfile").write_text(
            f"FROM skipped.test/{skipped}@sha256:{digest}\n", encoding="utf-8"
        )
    assert ci.images(tmp_path) == [
        f"one.test/a:1@sha256:{digest}",
        f"three.test/c@sha256:{digest}",
        f"two.test/b:2@sha256:{digest}",
    ]


def test_the_cache_key_follows_the_pinned_names_and_nothing_else() -> None:
    names = sorted(PINNED)
    key = ci.cache_key(names)
    assert re.fullmatch(r"[0-9a-f]{32}", key)
    assert ci.cache_key(list(reversed(names))) == key
    changed = [names[0][:-1] + ("0" if names[0][-1] != "0" else "1"), *names[1:]]
    assert ci.cache_key(changed) != key
    # A cached file is named by a hash: no registry, no image name, in a path.
    file = ci.cache_file(Path("cache"), names[0])
    assert re.fullmatch(r"[0-9a-f]{32}\.tar", file.name)


# --- Retries --------------------------------------------------------------------------------


def test_a_pull_that_fails_is_tried_again_with_growing_waits() -> None:
    docker = FakeDocker(failures=3)
    waits: list[float] = []
    said: list[str] = []
    tries = ci.pull(
        "r.test/i@sha256:1", run=docker, sleep=waits.append, jitter=lambda _: 0.0, say=said.append
    )
    assert tries == 4 and docker.pulls() == ["r.test/i@sha256:1"] * 4
    assert waits == [5, 15, 30]
    # What the registry said is shown each time.
    assert len(said) == 3 and all("429 Too Many Requests" in line for line in said)


def test_the_waits_grow_and_there_are_seven_tries_in_about_six_minutes() -> None:
    assert list(ci.DELAYS) == sorted(ci.DELAYS) and ci.DELAYS[0] >= 5
    assert len(ci.DELAYS) + 1 == 7
    assert 300 <= sum(ci.DELAYS) <= 400
    # The job's time limit leaves room for every wait, at its longest, and the checks.
    limit = int(re.search(r"python:\n(?:.*\n)*?    timeout-minutes: (\d+)", workflow()).group(1))  # type: ignore[union-attr]
    assert limit * 60 >= sum(ci.DELAYS) * (1 + ci.JITTER) + 10 * 60


def test_the_jitter_only_ever_adds_to_a_wait() -> None:
    waits: list[float] = []
    ci.pull("r.test/i@sha256:1", run=FakeDocker(failures=6), sleep=waits.append, say=lambda _: None)
    assert len(waits) == 6
    for wait, base in zip(waits, ci.DELAYS, strict=True):
        assert base <= wait <= base * (1 + ci.JITTER)


def test_a_pull_that_never_succeeds_gives_up_and_says_what_the_registry_answered() -> None:
    docker = FakeDocker(failures=100)
    waits: list[float] = []
    with pytest.raises(ci.PullFailed, match=r"7 tries failed.*429 Too Many Requests"):
        ci.pull(
            "r.test/i@sha256:1",
            run=docker,
            sleep=waits.append,
            jitter=lambda _: 0.0,
            say=lambda _: None,
        )
    assert len(docker.pulls()) == 7 and waits == list(ci.DELAYS)


def test_a_pull_that_works_first_time_waits_for_nothing() -> None:
    docker = FakeDocker()
    waits: list[float] = []
    assert ci.pull("r.test/i@sha256:1", run=docker, sleep=waits.append, say=lambda _: None) == 1
    assert waits == []


# --- The cache ------------------------------------------------------------------------------

NAMES = ["a.test/one@sha256:1", "b.test/two@sha256:2", "c.test/three@sha256:3"]


def test_with_no_cache_every_image_is_pulled_once_and_saved(tmp_path: Path) -> None:
    docker = FakeDocker()
    assert ensure(docker, tmp_path / "cache", NAMES) == (0, [])
    assert docker.pulls() == NAMES
    assert sorted(p.name for p in (tmp_path / "cache").glob("*.tar")) == sorted(
        ci.cache_file(tmp_path, name).name for name in NAMES
    )


def test_with_the_cache_nothing_is_asked_of_a_registry(tmp_path: Path) -> None:
    ensure(FakeDocker(), tmp_path, NAMES)
    # The next run: a new runner, with the cache restored and a registry that refuses.
    docker = FakeDocker(failures=100)
    assert ensure(docker, tmp_path, NAMES) == (0, [])
    assert docker.pulls() == []
    assert [call[0] for call in docker.calls].count("load") == 3
    # Nothing is saved twice.
    assert [call[0] for call in docker.calls].count("save") == 0


def test_where_a_loaded_image_loses_its_name_it_is_pulled_and_the_pull_is_retried(
    tmp_path: Path,
) -> None:
    """Docker's older image store does not keep a digest's name through save and load. The
    layers are there, so the pull is a request for the manifest alone, and it is retried."""
    ensure(FakeDocker(), tmp_path, NAMES)
    docker = FakeDocker(failures=2, keeps_names_on_load=False)
    status, waits = ensure(docker, tmp_path, NAMES)
    assert status == 0 and len(waits) == 2
    assert docker.pulls() == [NAMES[0]] * 3 + NAMES[1:]


def test_only_what_is_missing_is_pulled(tmp_path: Path) -> None:
    ensure(FakeDocker(), tmp_path, NAMES[:2])
    docker = FakeDocker()
    assert ensure(docker, tmp_path, NAMES)[0] == 0
    assert docker.pulls() == [NAMES[2]]
    assert len(list(tmp_path.glob("*.tar"))) == 3


def test_a_cached_file_that_cannot_be_loaded_is_removed_and_its_image_pulled(
    tmp_path: Path,
) -> None:
    ensure(FakeDocker(), tmp_path, NAMES)
    ci.cache_file(tmp_path, NAMES[1]).write_text("corrupt", encoding="utf-8")
    docker = FakeDocker()
    assert ensure(docker, tmp_path, NAMES)[0] == 0
    assert docker.pulls() == [NAMES[1]]
    # Saved again, whole.
    assert ci.cache_file(tmp_path, NAMES[1]).read_text(encoding="utf-8") == NAMES[1]


def test_a_registry_that_refuses_to_the_end_fails_the_step(tmp_path: Path) -> None:
    docker = FakeDocker(failures=100)
    status, waits = ensure(docker, tmp_path, NAMES)
    assert status == 1 and len(waits) == len(ci.DELAYS)
    # It stops at the first image it cannot get, and saves nothing it does not have.
    assert set(docker.pulls()) == {NAMES[0]} and list(tmp_path.glob("*.tar")) == []


def test_an_image_that_cannot_be_saved_does_not_fail_the_step(tmp_path: Path) -> None:
    class NoSave(FakeDocker):
        def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "save":
                return subprocess.CompletedProcess(
                    list(arguments), 1, stdout="", stderr="disk full"
                )
            return super().__call__(arguments)

    assert ensure(NoSave(), tmp_path, NAMES)[0] == 0
    assert list(tmp_path.glob("*.tar")) == []


# --- The workflow ---------------------------------------------------------------------------


def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def python_job() -> str:
    text = workflow()
    return text[text.index("\n  python:\n") : text.index("\n  portal:\n")]


def test_the_python_job_gets_its_base_images_before_the_tests_build_from_them() -> None:
    job = python_job()
    order = [
        "uv sync --locked",
        "uv run scripts/ci_base_images.py key",
        "actions/cache/restore@",
        'uv run scripts/ci_base_images.py ensure --cache "$RUNNER_TEMP/base-images"',
        "actions/cache/save@",
        "run: uv run pytest",
    ]
    positions = [job.index(step) for step in order]
    assert positions == sorted(positions)
    # One key and one path, the same for the restore and the save.
    assert job.count("key: base-images-${{ runner.os }}-${{ steps.base-images.outputs.key }}") == 2
    assert job.count("path: ${{ runner.temp }}/base-images") == 2
    # Saved as soon as the images are there, and only when the cache did not have them.
    assert "if: steps.base-images-cache.outputs.cache-hit != 'true'" in job


def test_the_cache_action_is_pinned_to_one_commit_with_its_release_named() -> None:
    uses = re.findall(r"uses: (actions/cache/(?:restore|save))@(\S+) # (v[0-9.]+)", workflow())
    assert sorted(action for action, _, _ in uses) == [
        "actions/cache/restore",
        "actions/cache/save",
    ]
    commits = {commit for _, commit, _ in uses}
    assert len(commits) == 1 and re.fullmatch(r"[0-9a-f]{40}", commits.pop())
    assert len({release for _, _, release in uses}) == 1


def test_every_action_in_the_workflow_is_pinned_to_a_full_commit() -> None:
    uses = re.findall(r"^\s*-? ?uses: (\S+)(.*)$", workflow(), flags=re.MULTILINE)
    assert len(uses) >= 7
    for action, rest in uses:
        assert re.search(r"@[0-9a-f]{40}$", action), action
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", rest), (action, rest)


def test_ci_has_no_aws_credentials_and_signs_in_to_no_registry() -> None:
    """Owner, 2026-11-27: the 429 is cured with a cache and retries, never by signing CI in.
    Every image is public and pinned by digest."""
    text = workflow().lower()
    for forbidden in (
        "aws-actions",
        "configure-aws-credentials",
        "amazon-ecr-login",
        "docker/login-action",
        "docker login",
        "get-login-password",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "role-to-assume",
        "id-token",
        "secrets.",
    ):
        assert forbidden not in text, forbidden
    assert "permissions:\n  contents: read\n" in workflow()
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ('"login"', "get-login-password", "boto3", "AWS_", "password"):
        assert forbidden not in source, forbidden
    # Docker is asked to do five things, and none of them signs in.
    assert set(re.findall(r'run\(\["(\w+)"', source)) == {"image", "pull", "load", "save"}
