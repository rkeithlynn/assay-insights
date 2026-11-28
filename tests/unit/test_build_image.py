"""scripts/build_image.py: the isolated Docker config and the manifest and token checks."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_image.py"
_spec = importlib.util.spec_from_file_location("build_image", SCRIPT)
assert _spec is not None and _spec.loader is not None
build_image: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_image)


def test_isolated_config_has_no_credential_store() -> None:
    config = build_image.isolated_config(["C:/Users/u/.docker/cli-plugins"])
    assert "credsStore" not in config
    assert "credHelpers" not in config
    assert config["auths"] == {}
    assert config["cliPluginsExtraDirs"] == ["C:/Users/u/.docker/cli-plugins"]


def test_isolated_env_points_docker_at_the_temporary_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("DOCKER_CONFIG", "C:/Users/u/.docker")
    env = build_image.isolated_env(tmp_path, "npipe:////./pipe/dockerDesktopLinuxEngine")
    assert env["DOCKER_CONFIG"] == tmp_path.as_posix()
    assert env["DOCKER_HOST"] == "npipe:////./pipe/dockerDesktopLinuxEngine"
    assert "DOCKER_CONTEXT" not in env


def test_path_drops_directories_with_credential_helpers(tmp_path: Path) -> None:
    helpers = tmp_path / "docker-bin"
    helpers.mkdir()
    (helpers / "docker-credential-wincred.exe").write_text("")
    (helpers / "docker.exe").write_text("")
    other = tmp_path / "tools"
    other.mkdir()
    path = os.pathsep.join([str(other), str(helpers), "", str(tmp_path / "missing")])
    kept = build_image.path_without_credential_helpers(path).split(os.pathsep)
    assert kept == [str(other), str(tmp_path / "missing")]


def test_isolated_env_hides_credential_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helpers = tmp_path / "bin"
    helpers.mkdir()
    (helpers / "docker-credential-desktop.exe").write_text("")
    monkeypatch.setenv("PATH", os.pathsep.join([str(helpers), str(tmp_path)]))
    env = build_image.isolated_env(tmp_path, "npipe:////./pipe/x")
    assert env["PATH"] == str(tmp_path)


def test_token_must_stay_in_the_temporary_file(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"auths": {"registry.test": {"auth": "x"}}}))
    build_image.assert_token_kept_in_file(tmp_path, "registry.test")

    config.write_text(json.dumps({"auths": {}, "credsStore": "desktop"}))
    with pytest.raises(RuntimeError, match="credential store"):
        build_image.assert_token_kept_in_file(tmp_path, "registry.test")

    config.write_text(json.dumps({"auths": {}}))
    with pytest.raises(RuntimeError, match="did not record"):
        build_image.assert_token_kept_in_file(tmp_path, "registry.test")


def test_remove_tree_deletes_read_only_files(tmp_path: Path) -> None:
    target = tmp_path / "cfg"
    target.mkdir()
    secret = target / "config.json"
    secret.write_text("{}")
    secret.chmod(0o444)
    build_image._remove_tree(target)
    assert not target.exists()


def test_decode_ecr_token() -> None:
    token = base64.b64encode(b"AWS:" + b"p" * 3000).decode()
    assert build_image.decode_ecr_token(token) == ("AWS", "p" * 3000)
    with pytest.raises(ValueError, match="format"):
        build_image.decode_ecr_token(base64.b64encode(b"no-separator").decode())


@pytest.mark.parametrize(
    ("media_type", "ok"),
    [
        ("application/vnd.oci.image.manifest.v1+json", True),
        ("application/vnd.docker.distribution.manifest.v2+json", True),
        ("application/vnd.oci.image.index.v1+json", False),
        ("application/vnd.docker.distribution.manifest.list.v2+json", False),
    ],
)
def test_only_single_image_manifests_pass(media_type: str, ok: bool) -> None:
    raw = json.dumps({"mediaType": media_type})
    if ok:
        build_image.check_single_manifest(raw)
    else:
        with pytest.raises(ValueError, match="single image manifest"):
            build_image.check_single_manifest(raw)


def test_build_turns_off_attestations_and_targets_amd64() -> None:
    args = build_image.build_args(ref="r:t", dockerfile="d", context=".", metadata_file="m")
    assert {"--provenance=false", "--sbom=false", "--push"} <= set(args)
    assert args[args.index("--platform") + 1] == "linux/amd64"


# --- Confirming the pushed image, and using one that is already there -----------------------------
#
# `pulumi up` failed twice with "<repository>@sha256:...: not found" for an image ECR had
# accepted (2026-11-24, 2026-11-25): the check was a second registry client whose request
# never reached ECR, and which reported its fallback's "not found" instead.

DIGEST = "sha256:" + "ab" * 32
OCI = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
URL = "123456789012.dkr.ecr.us-east-1.amazonaws.com/assay-test-worker"


def _image(media_type: str = OCI, digest: str = DIGEST) -> dict[str, Any]:
    return {
        "images": [
            {
                "imageId": {"imageDigest": digest, "imageTag": "src-1"},
                "imageManifest": json.dumps({"schemaVersion": 2}),
                "imageManifestMediaType": media_type,
            }
        ],
        "failures": [],
    }


def _missing(code: str = "ImageNotFound") -> dict[str, Any]:
    return {"images": [], "failures": [{"failureCode": code}]}


class FakeEcr:
    """Answers `batch_get_image` from a list: a response, or an exception to raise."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.asked: list[dict[str, Any]] = []

    def batch_get_image(self, **request: Any) -> dict[str, Any]:
        self.asked.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def _connection_error() -> Exception:
    from botocore.exceptions import ConnectTimeoutError

    return ConnectTimeoutError(endpoint_url="https://api.ecr.us-east-1.amazonaws.com")


def _no_wait(_: float) -> None:
    return None


def test_the_pushed_image_is_confirmed_through_ecr_not_a_registry_client() -> None:
    """No `imagetools` call is left: that client turned a failed request into "not found"."""
    code = SCRIPT.read_text(encoding="utf-8").split('"""', 2)[2]  # after the module docstring
    assert "imagetools" not in code
    ecr = FakeEcr([_image()])
    build_image.confirm_pushed(ecr, "assay-test-worker", DIGEST, sleep=_no_wait)
    [request] = ecr.asked
    assert request["repositoryName"] == "assay-test-worker"
    assert request["imageIds"] == [{"imageDigest": DIGEST}]
    assert OCI in request["acceptedMediaTypes"]
    assert INDEX in request["acceptedMediaTypes"]


def test_a_request_that_fails_or_an_image_not_yet_listed_is_looked_for_again() -> None:
    waited: list[float] = []
    ecr = FakeEcr([_connection_error(), _missing(), _image()])
    build_image.confirm_pushed(ecr, "assay-test-worker", DIGEST, sleep=waited.append)
    assert len(ecr.asked) == 3
    assert waited == list(build_image.CONFIRM_WAITS[:2])


def test_an_image_that_never_appears_fails_and_says_what_the_last_answer_was() -> None:
    tries = len(build_image.CONFIRM_WAITS) + 1
    with pytest.raises(build_image.ImageCheckFailed, match="has no such image"):
        build_image.confirm_pushed(
            FakeEcr([_missing()] * tries), "assay-test-worker", DIGEST, sleep=_no_wait
        )
    # A connection that keeps failing is reported as that, never as a missing image.
    with pytest.raises(build_image.ImageCheckFailed, match="request to ECR failed") as failed:
        build_image.confirm_pushed(
            FakeEcr([_connection_error() for _ in range(tries)]),
            "assay-test-worker",
            DIGEST,
            sleep=_no_wait,
        )
    assert "no such image" not in str(failed.value)
    # Longer than the 37 seconds after which both failed checks gave up.
    assert sum(build_image.CONFIRM_WAITS) >= 60


def test_an_image_index_is_refused_and_not_looked_for_again() -> None:
    ecr = FakeEcr([_image(INDEX), _image()])
    with pytest.raises(ValueError, match="single image manifest"):
        build_image.confirm_pushed(ecr, "assay-test-worker", DIGEST, sleep=_no_wait)
    assert len(ecr.asked) == 1


def test_an_answer_that_is_neither_an_image_nor_not_found_is_an_error() -> None:
    with pytest.raises(build_image.ImageCheckFailed, match="KmsError"):
        build_image.stored_image(FakeEcr([_missing("KmsError")]), "r", {"imageTag": "t"})


def test_an_image_already_under_its_tag_is_used_without_building() -> None:
    """A run that failed after its push left the image there. Tags are the hash of the sources
    and cannot be overwritten, so the next run uses it: no rebuild, no upload."""
    built: list[str] = []

    def build() -> str:
        built.append("built")
        return "sha256:" + "cd" * 32

    ecr = FakeEcr([_image()])
    digest = build_image.image_digest(ecr, repository_url=URL, tag="src-1", build=build)
    assert (digest, built) == (DIGEST, [])
    assert ecr.asked[0]["imageIds"] == [{"imageTag": "src-1"}]
    assert ecr.asked[0]["repositoryName"] == "assay-test-worker"


def test_a_new_tag_is_built_and_then_confirmed_by_digest() -> None:
    ecr = FakeEcr([_missing(), _image()])
    digest = build_image.image_digest(ecr, repository_url=URL, tag="src-2", build=lambda: DIGEST)
    assert digest == DIGEST
    assert [request["imageIds"] for request in ecr.asked] == [
        [{"imageTag": "src-2"}],
        [{"imageDigest": DIGEST}],
    ]
