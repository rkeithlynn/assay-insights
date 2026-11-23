"""Build a linux/amd64 container image in Docker, push it to ECR and print its digest.

Run by Pulumi (a `command.local.Command` in infra/assay_infra/migrations.py) during
`pulumi up`, not by hand. Inputs come from the environment:

    IMAGE_REPOSITORY_URL   <account>.dkr.ecr.<region>.amazonaws.com/<name>
    IMAGE_TAG              tag to push (the repository is tag-immutable)
    IMAGE_DOCKERFILE       path to the Dockerfile, relative to the build context
    IMAGE_CONTEXT          build context directory
    AWS_PROFILE, AWS_REGION

Two things this script works around:

- Lambda accepts only a single-architecture image manifest. Docker Engine 29 adds provenance
  attestations by default, which turns the push into an image index that Lambda rejects, so
  attestations are turned off and the pushed manifest is checked before the digest is printed.
- The ECR token is too long for the Windows Credential Manager, which Docker Desktop uses as
  its credential store ("The stub received bad data"). Login, build, push and inspect run
  with DOCKER_CONFIG pointing at a temporary directory whose config.json has no credsStore,
  and with the docker-credential-* helpers removed from PATH (otherwise the CLI falls back
  to wincred), so the token lives only in that file, which is deleted afterwards. The
  script checks the file after login and stops if a credential store crept in. The user's own Docker
  configuration is never read for credentials or changed. The temporary config keeps the
  current Docker endpoint (DOCKER_HOST) and the buildx plugin directory, so buildx still
  talks to the same engine.

The digest is the only thing written to stdout.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import boto3

SINGLE_IMAGE_MANIFESTS = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
PLATFORM = "linux/amd64"
CONFIG_PREFIX = "assay-docker-config-"
CREDENTIAL_HELPER_PREFIX = "docker-credential-"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def docker() -> str:
    exe = shutil.which("docker")
    if exe is None:
        sys.exit("docker CLI not found on PATH (is Docker Desktop running?)")
    return exe


def run(
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603
        [docker(), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if check and result.returncode != 0:
        log(result.stdout)
        log(result.stderr)
        sys.exit(f"docker {args[0]} failed with exit code {result.returncode}")
    return result.stdout


def registry_of(repository_url: str) -> str:
    return repository_url.split("/", 1)[0]


def decode_ecr_token(token: str) -> tuple[str, str]:
    user, _, password = base64.b64decode(token).decode().partition(":")
    if not user or not password:
        raise ValueError("unexpected ECR authorization token format")
    return user, password


def check_single_manifest(raw_manifest: str) -> None:
    media_type = json.loads(raw_manifest).get("mediaType")
    if media_type not in SINGLE_IMAGE_MANIFESTS:
        raise ValueError(
            f"pushed manifest has media type {media_type!r}; Lambda needs a single image manifest"
        )


def build_args(*, ref: str, dockerfile: str, context: str, metadata_file: str) -> list[str]:
    return [
        "buildx",
        "build",
        "--platform",
        PLATFORM,
        "--provenance=false",
        "--sbom=false",
        "--pull",
        "--file",
        dockerfile,
        "--tag",
        ref,
        "--metadata-file",
        metadata_file,
        "--push",
        context,
    ]


# --- Isolated Docker configuration ----------------------------------------------------


def current_docker_host() -> str:
    """The engine endpoint of the user's current Docker context (read, never changed)."""
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    host = run("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").strip()
    if not host:
        sys.exit("could not read the current Docker context's endpoint")
    return host


def buildx_plugin_dirs() -> list[str]:
    plugins = json.loads(run("info", "--format", "{{json .ClientInfo.Plugins}}") or "[]")
    dirs = sorted({Path(p["Path"]).parent.as_posix() for p in plugins if p.get("Name") == "buildx"})
    if not dirs:
        sys.exit("the docker buildx plugin was not found")
    return dirs


def isolated_config(plugin_dirs: list[str]) -> dict[str, object]:
    """config.json with no credsStore and no credHelpers: credentials stay in the file."""
    return {"auths": {}, "cliPluginsExtraDirs": plugin_dirs}


def _holds_credential_helper(entry: str) -> bool:
    try:
        return any(Path(entry).glob(f"{CREDENTIAL_HELPER_PREFIX}*"))
    except OSError:
        return False


def path_without_credential_helpers(path: str) -> str:
    """PATH minus every directory holding a docker-credential-* helper.

    With no credsStore in config.json, the Docker CLI still picks the platform's default
    store (wincred on Windows) when its helper is on PATH, and `docker login` then writes
    credsStore back into the config. Hiding the helpers keeps the credentials in the file.
    Docker itself is run by its full path, so it does not need to be on PATH.
    """
    kept = [e for e in path.split(os.pathsep) if e and not _holds_credential_helper(e)]
    return os.pathsep.join(kept)


def isolated_env(config_dir: Path, docker_host: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"DOCKER_CONTEXT", "PATH"}}
    env["PATH"] = path_without_credential_helpers(os.environ.get("PATH", ""))
    env["DOCKER_CONFIG"] = config_dir.as_posix()
    env["DOCKER_HOST"] = docker_host
    return env


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(func: object, target: str, _exc: BaseException) -> None:
        Path(target).chmod(stat.S_IWRITE)
        if callable(func):
            func(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)
    if path.exists():
        raise RuntimeError(f"could not delete {path}; it holds a registry token, delete it by hand")


@contextmanager
def isolated_docker() -> Iterator[tuple[dict[str, str], Path]]:
    """Environment for docker subprocesses that never touches the user's credential store."""
    docker_host = current_docker_host()
    plugin_dirs = buildx_plugin_dirs()
    config_dir = Path(tempfile.mkdtemp(prefix=CONFIG_PREFIX))
    try:
        (config_dir / "config.json").write_text(json.dumps(isolated_config(plugin_dirs)))
        env = isolated_env(config_dir, docker_host)
        run("buildx", "version", env=env)  # buildx must still load with this config
        yield env, config_dir
    finally:
        _remove_tree(config_dir)


def assert_token_kept_in_file(config_dir: Path, registry: str) -> None:
    config = json.loads((config_dir / "config.json").read_text())
    if config.get("credsStore") or config.get("credHelpers"):
        raise RuntimeError("temporary Docker config gained a credential store")
    if registry not in config.get("auths", {}):
        raise RuntimeError(f"docker login did not record {registry} in the temporary config")


# --- Build and push -------------------------------------------------------------------


def publish(
    *,
    repository_url: str,
    tag: str,
    dockerfile: str,
    context: str,
    username: str,
    password: str,
) -> str:
    """Log in, build, push and verify the image; return its digest."""
    registry = registry_of(repository_url)
    ref = f"{repository_url}:{tag}"
    with isolated_docker() as (env, config_dir):
        run(
            "login",
            "--username",
            username,
            "--password-stdin",
            registry,
            env=env,
            stdin=password,
        )
        try:
            assert_token_kept_in_file(config_dir, registry)
            log(f"Building {ref} for {PLATFORM} ...")
            metadata_path = config_dir / "metadata.json"
            run(
                *build_args(
                    ref=ref,
                    dockerfile=dockerfile,
                    context=context,
                    metadata_file=metadata_path.as_posix(),
                ),
                env=env,
            )
            digest: str = json.loads(metadata_path.read_text())["containerimage.digest"]
            pushed = f"{repository_url}@{digest}"
            check_single_manifest(run("buildx", "imagetools", "inspect", "--raw", pushed, env=env))
        finally:
            run("logout", registry, env=env, check=False)
    log(f"Pushed {pushed}")
    return digest


def ecr_credentials(repository_url: str) -> tuple[str, str]:
    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None,
        region_name=os.environ["AWS_REGION"],
    )
    registry_id = registry_of(repository_url).split(".", 1)[0]
    auth = session.client("ecr").get_authorization_token(registryIds=[registry_id])
    return decode_ecr_token(auth["authorizationData"][0]["authorizationToken"])


def main() -> int:
    repository_url = os.environ["IMAGE_REPOSITORY_URL"]
    context = Path(os.environ["IMAGE_CONTEXT"])
    username, password = ecr_credentials(repository_url)
    digest = publish(
        repository_url=repository_url,
        tag=os.environ["IMAGE_TAG"],
        dockerfile=(context / os.environ["IMAGE_DOCKERFILE"]).as_posix(),
        context=context.as_posix(),
        username=username,
        password=password,
    )
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
