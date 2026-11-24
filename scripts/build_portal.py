"""Build the portal in a Node container and upload it to the portal bucket.

Run by Pulumi (`assay_infra.edge`, a `command.local.Command`) during `pulumi up`, and again
only when the portal's sources change; never during a preview. Reads from the environment:

    PORTAL_BUCKET, DISTRIBUTION_ID, PORTAL_CONTEXT (the repository root), AWS_REGION,
    and optionally AWS_PROFILE.

Steps:

1. `docker build` of `portal/Dockerfile` for linux/amd64. The image's build stage runs
   `npm ci --ignore-scripts` (exactly the locked versions, no install scripts), the type
   check, the unit tests and `vite build`; its final stage holds only the built files,
   which are exported to a temporary directory. Nothing is built on the host itself.
2. Upload every file. File names under `assets/` carry a content hash, so they are cached
   for a year; `index.html` is never cached. Older hashed files are kept, so a browser with
   the previous `index.html` open still loads. `config.json` belongs to Pulumi and is never
   written here.
3. Invalidate `/index.html` (and `/`) in CloudFront.
"""

from __future__ import annotations

import mimetypes
import os
import shutil

# Runs the Docker CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

import boto3

DOCKERFILE = "portal/Dockerfile"
CONFIG_KEY = "config.json"  # assay_infra.edge.CONFIG_KEY: written by Pulumi only
IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"
NO_CACHE = "no-cache"
TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def cache_control(key: str) -> str:
    if key == "index.html":
        return NO_CACHE
    if key.startswith("assets/"):
        return IMMUTABLE
    return SHORT


def content_type(key: str) -> str:
    suffix = Path(key).suffix.lower()
    return TYPES.get(suffix) or mimetypes.guess_type(key)[0] or "application/octet-stream"


def build(context: Path, out: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        sys.exit("docker CLI not found on PATH")
    log("Building the portal (npm ci, type check, tests, vite build) ...")
    subprocess.run(  # noqa: S603  # nosec B603
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "-f",
            str(context / DOCKERFILE),
            "--output",
            f"type=local,dest={out}",
            str(context),
        ],
        check=True,
        stdout=sys.stderr,
    )
    if not (out / "index.html").is_file():
        sys.exit("the portal build produced no index.html")


def files(root: Path) -> list[tuple[str, Path]]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            key = path.relative_to(root).as_posix()
            if key == CONFIG_KEY:
                sys.exit("the portal build must not contain config.json (Pulumi writes it)")
            found.append((key, path))
    return found


def main() -> int:
    bucket = os.environ["PORTAL_BUCKET"]
    distribution = os.environ["DISTRIBUTION_ID"]
    context = Path(os.environ["PORTAL_CONTEXT"])
    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None, region_name=os.environ["AWS_REGION"]
    )
    with tempfile.TemporaryDirectory(prefix="assay-portal-") as tmp:
        out = Path(tmp)
        build(context, out)
        s3 = session.client("s3")
        upload = files(out)
        # Hashed assets first, index.html last: a browser never gets an index.html whose
        # files are not there yet.
        upload.sort(key=lambda item: item[0] == "index.html")
        for key, path in upload:
            s3.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"ContentType": content_type(key), "CacheControl": cache_control(key)},
            )
        log(f"Uploaded {len(upload)} files to s3://{bucket}/")
    cloudfront = session.client("cloudfront")
    cloudfront.create_invalidation(
        DistributionId=distribution,
        InvalidationBatch={
            "Paths": {"Quantity": 2, "Items": ["/index.html", "/"]},
            "CallerReference": f"portal-{int(time.time())}",
        },
    )
    log("CloudFront invalidation started for /index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
