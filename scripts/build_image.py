"""Build a linux/amd64 container image in Docker, push it to ECR and print its digest.

Run by Pulumi (a `command.local.Command` in infra/assay_infra/migrations.py) during
`pulumi up`, not by hand. Inputs come from the environment:

    IMAGE_REPOSITORY_URL   <account>.dkr.ecr.<region>.amazonaws.com/<name>
    IMAGE_TAG              tag to push (the repository is tag-immutable)
    IMAGE_DOCKERFILE       path to the Dockerfile, relative to the build context
    IMAGE_CONTEXT          build context directory
    AWS_PROFILE, AWS_REGION

Lambda accepts only a single-architecture image manifest. Docker Engine 29 adds provenance
attestations by default, which turns the push into an image index that Lambda rejects, so
attestations are turned off and the pushed manifest is checked before the digest is printed.
The digest is the only thing written to stdout.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

import boto3

SINGLE_IMAGE_MANIFESTS = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
PLATFORM = "linux/amd64"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def docker() -> str:
    exe = shutil.which("docker")
    if exe is None:
        sys.exit("docker CLI not found on PATH (is Docker Desktop running?)")
    return exe


def run(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603
        [docker(), *args], input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
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


def main() -> int:
    repository_url = os.environ["IMAGE_REPOSITORY_URL"]
    tag = os.environ["IMAGE_TAG"]
    context = Path(os.environ["IMAGE_CONTEXT"])
    dockerfile = (context / os.environ["IMAGE_DOCKERFILE"]).as_posix()
    ref = f"{repository_url}:{tag}"
    registry = registry_of(repository_url)

    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None,
        region_name=os.environ["AWS_REGION"],
    )
    auth = session.client("ecr").get_authorization_token(registryIds=[registry.split(".", 1)[0]])
    user, password = decode_ecr_token(auth["authorizationData"][0]["authorizationToken"])
    run("login", "--username", user, "--password-stdin", registry, stdin=password)

    log(f"Building {ref} for {PLATFORM} ...")
    with tempfile.TemporaryDirectory() as tmp:
        metadata_path = Path(tmp) / "metadata.json"
        run(
            *build_args(
                ref=ref,
                dockerfile=dockerfile,
                context=context.as_posix(),
                metadata_file=metadata_path.as_posix(),
            )
        )
        digest = json.loads(metadata_path.read_text())["containerimage.digest"]

    check_single_manifest(
        run("buildx", "imagetools", "inspect", "--raw", pushed := f"{repository_url}@{digest}")
    )
    log(f"Pushed {pushed}")
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
