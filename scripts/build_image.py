"""Build a linux/amd64 container image in Docker, push it to ECR and print its digest.

Run by Pulumi (a `command.local.Command` in infra/assay_infra/migrations.py) during
`pulumi up`, not by hand. Inputs come from the environment:

    IMAGE_REPOSITORY_URL   <account>.dkr.ecr.<region>.amazonaws.com/<name>
    IMAGE_TAG              tag to push (the repository is tag-immutable)
    IMAGE_DOCKERFILE       path to the Dockerfile, relative to the build context
    IMAGE_CONTEXT          build context directory
    AWS_PROFILE, AWS_REGION

Three things this script works around:

- Lambda accepts only a single-architecture image manifest. Docker Engine 29 adds provenance
  attestations by default, which turns the push into an image index that Lambda rejects, so
  attestations are turned off and the pushed manifest is checked before the digest is printed.
- The pushed image is confirmed through ECR's own API (`BatchGetImage`), with retries, never
  with a second registry client. Twice (2026-11-24 and 2026-11-25) `pulumi up` failed with
  "<repository>@sha256:...: not found" for an image ECR had accepted 37 seconds earlier. Both
  times every image was uploading at once, and CloudTrail shows what happened: the check's
  request for the manifest never reached ECR (no `BatchGetImage` for the digest). All ECR
  saw was the registry client's fallback, a lookup of the manifest's digest as a layer, which
  is "not found" by definition, and the client reported that answer instead of the failed
  request. So a slow connection read as a missing image. This check says what failed, and
  tries again.
- A run that fails after its push has left the image in the repository under its tag, which
  is the hash of its sources in a repository whose tags cannot be overwritten. The next run
  finds that tag and uses the image as it is: no rebuild, no upload, and no second push that
  the repository would refuse if the rebuild's bytes differed.
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
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

SINGLE_IMAGE_MANIFESTS = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
# What ECR is asked for. The index types are accepted so that an index is returned, and then
# refused here with a message that says what it is.
ACCEPTED_MANIFESTS = sorted(
    SINGLE_IMAGE_MANIFESTS
    | {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
PLATFORM = "linux/amd64"
# Looking for the image just pushed: at once, then after each of these waits (about 90
# seconds in all). ECR has always had it at once; the waits are for a request that fails.
CONFIRM_WAITS = (2, 4, 8, 15, 30, 30)
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


def check_single_manifest(raw_manifest: str, media_type: str | None = None) -> None:
    """`media_type` is ECR's own record of the manifest's type, when the caller has it."""
    media_type = media_type or json.loads(raw_manifest).get("mediaType")
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


# --- What ECR holds --------------------------------------------------------------------


class ImageCheckFailed(RuntimeError):
    """The image could not be confirmed. The message says what failed."""


def repository_of(repository_url: str) -> str:
    return repository_url.split("/", 1)[1]


def stored_image(ecr: Any, repository: str, image_id: Mapping[str, str]) -> str | None:
    """The digest of the image ECR holds under a tag or a digest, checked to be a single
    image. None if it holds none. Any other answer raises."""
    answer = ecr.batch_get_image(
        repositoryName=repository,
        imageIds=[dict(image_id)],
        acceptedMediaTypes=ACCEPTED_MANIFESTS,
    )
    images = answer.get("images") or []
    if not images:
        codes = sorted(str(f.get("failureCode")) for f in answer.get("failures") or [])
        if all(code == "ImageNotFound" for code in codes):
            return None
        raise ImageCheckFailed(f"ECR could not return the image: {', '.join(codes)}")
    image = images[0]
    check_single_manifest(image["imageManifest"], image.get("imageManifestMediaType"))
    return str(image["imageId"]["imageDigest"])


def confirm_pushed(
    ecr: Any, repository: str, digest: str, *, sleep: Callable[[float], None] = time.sleep
) -> None:
    """ECR holds the image that was just pushed, as a single image. Tries again after a
    request that failed or an answer of "not found", and says which it was each time."""
    problem = ""
    for wait in (*CONFIRM_WAITS, None):
        try:
            if stored_image(ecr, repository, {"imageDigest": digest}) is not None:
                return
            problem = "ECR answered that it has no such image"
        except (BotoCoreError, ClientError) as exc:
            problem = f"the request to ECR failed ({type(exc).__name__}: {exc})"
        if wait is not None:
            log(f"{repository}@{digest}: {problem}; looking again in {wait} seconds")
            sleep(wait)
    raise ImageCheckFailed(
        f"{repository}@{digest} could not be confirmed after {len(CONFIRM_WAITS) + 1} tries: "
        f"{problem}. If the push succeeded, the next `pulumi up` finds the image by its tag "
        "and does not build it again."
    )


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
    """Log in, build and push the image; return the digest the build reports."""
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
        finally:
            run("logout", registry, env=env, check=False)
    log(f"Pushed {repository_url}@{digest}")
    return digest


def ecr_client() -> Any:
    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None,
        region_name=os.environ["AWS_REGION"],
    )
    # Several images upload at once and can fill the connection: give each request time, and
    # let the SDK try a failed one again before this script does.
    config = Config(
        connect_timeout=20, read_timeout=60, retries={"mode": "standard", "max_attempts": 5}
    )
    return session.client("ecr", config=config)


def ecr_credentials(ecr: Any, repository_url: str) -> tuple[str, str]:
    registry_id = registry_of(repository_url).split(".", 1)[0]
    auth = ecr.get_authorization_token(registryIds=[registry_id])
    return decode_ecr_token(auth["authorizationData"][0]["authorizationToken"])


def image_digest(ecr: Any, *, repository_url: str, tag: str, build: Callable[[], str]) -> str:
    """The digest of the image for this tag: the one ECR already holds under it, or the one
    `build` pushes, confirmed in ECR before it is returned."""
    repository = repository_of(repository_url)
    existing = stored_image(ecr, repository, {"imageTag": tag})
    if existing is not None:
        # The tag is the hash of the image's sources and the repository's tags cannot be
        # overwritten, so this is the image these sources built (an earlier run pushed it).
        log(f"{repository_url}:{tag} is already in the repository; not built again")
        return existing
    digest = build()
    confirm_pushed(ecr, repository, digest)
    return digest


def main() -> int:
    repository_url = os.environ["IMAGE_REPOSITORY_URL"]
    tag = os.environ["IMAGE_TAG"]
    context = Path(os.environ["IMAGE_CONTEXT"])
    ecr = ecr_client()

    def build() -> str:
        username, password = ecr_credentials(ecr, repository_url)
        return publish(
            repository_url=repository_url,
            tag=tag,
            dockerfile=(context / os.environ["IMAGE_DOCKERFILE"]).as_posix(),
            context=context.as_posix(),
            username=username,
            password=password,
        )

    try:
        digest = image_digest(ecr, repository_url=repository_url, tag=tag, build=build)
    except (ImageCheckFailed, ValueError) as exc:
        sys.exit(str(exc))
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
