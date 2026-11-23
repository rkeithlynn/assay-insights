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
