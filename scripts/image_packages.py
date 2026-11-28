"""Which Python packages each container image holds: record them, or check them.

Usage (PowerShell, from the repository root; needs Docker):

    uv run scripts/image_packages.py            # build each image and compare with the record
    uv run scripts/image_packages.py --write    # build each image and rewrite the record

The record is `tests/unit/image_packages.json`: for every image that installs locked
dependencies, each distribution its interpreter can see, as `name==version`. It was first
written from the images as they were before their last layer was split into a dependencies
layer and a code layer (owner, 2026-11-25), and `tests/unit/test_image_layers.py` holds every
image to it: a change to a Dockerfile must not change what is installed.

Rewrite the record only when `uv.lock` or a base image changes on purpose, and read the
difference before committing it: it is the list of what the change brought in or took out.

Nothing here reaches AWS. Images are built locally, for linux/amd64, and removed afterwards.
"""

from __future__ import annotations

import argparse
import json
import shutil

# Runs the Docker CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORD = REPO_ROOT / "tests" / "unit" / "image_packages.json"
PLATFORM = "linux/amd64"

# Every Dockerfile with a dependencies layer, and the interpreter its image runs.
LAMBDA_PYTHON = "/var/lang/bin/python3"
TASK_PYTHON = "/usr/local/bin/python3"
IMAGES: dict[str, str] = {
    "api/Dockerfile": TASK_PYTHON,
    "api/lambda.Dockerfile": LAMBDA_PYTHON,
    "db/Dockerfile": LAMBDA_PYTHON,
    "tss/Dockerfile": LAMBDA_PYTHON,
    "tss/provision.Dockerfile": TASK_PYTHON,
    "workers/lambda.Dockerfile": LAMBDA_PYTHON,
    "workers/worker.Dockerfile": TASK_PYTHON,
    "tests/isolation/Dockerfile": LAMBDA_PYTHON,
}

# Run inside the image: every distribution on the interpreter's path, as the image is set up
# (its own PYTHONPATH and working directory).
LIST_PACKAGES = (
    "import importlib.metadata as m, json; "
    "print(json.dumps(sorted({f\"{d.metadata['Name'].lower()}=={d.version}\" "
    "for d in m.distributions()})))"
)


def docker(*args: str, timeout: int = 1800) -> str:
    exe = shutil.which("docker")
    if exe is None:
        sys.exit("docker CLI not found on PATH (is Docker Desktop running?)")
    result = subprocess.run(  # noqa: S603  # nosec B603
        [exe, *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode != 0:
        sys.exit(f"docker {args[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def build(dockerfile: Path, tag: str) -> None:
    docker(
        "build",
        "--platform",
        PLATFORM,
        "--provenance=false",
        "--sbom=false",
        "-f",
        str(dockerfile),
        "-t",
        tag,
        str(REPO_ROOT),
    )


def packages(image: str, python: str) -> list[str]:
    """Every distribution the image's interpreter can see, as `name==version`, sorted."""
    out = docker(
        "run", "--rm", "--network", "none", "--entrypoint", python, image, "-c", LIST_PACKAGES
    )
    found: list[str] = json.loads(out.strip().splitlines()[-1])
    return found


def built_packages(dockerfile: str) -> list[str]:
    """Build one of `IMAGES`, list its packages, and remove the image."""
    tag = f"assay-packages-{uuid.uuid4().hex[:12]}"
    build(REPO_ROOT / dockerfile, tag)
    try:
        return packages(tag, IMAGES[dockerfile])
    finally:
        docker("rmi", "-f", tag)


def recorded() -> dict[str, list[str]]:
    record: dict[str, list[str]] = json.loads(RECORD.read_text(encoding="utf-8"))["images"]
    return record


def difference(before: list[str], after: list[str]) -> list[str]:
    """What differs between two package lists, one line each; empty if they are the same."""
    gone = [f"- {package}" for package in sorted(set(before) - set(after))]
    new = [f"+ {package}" for package in sorted(set(after) - set(before))]
    return gone + new


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the record")
    args = parser.parse_args()

    found = {}
    for dockerfile in IMAGES:
        print(f"Building {dockerfile} ...", flush=True)
        found[dockerfile] = built_packages(dockerfile)
    if args.write:
        note = (
            "Every distribution each image's interpreter can see. Written by "
            "scripts/image_packages.py --write; checked by tests/unit/test_image_layers.py."
        )
        RECORD.write_text(
            json.dumps({"note": note, "images": found}, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Wrote {RECORD.relative_to(REPO_ROOT).as_posix()}.")
        return 0
    record = recorded()
    failed = False
    for dockerfile, now in found.items():
        lines = difference(record.get(dockerfile, []), now)
        print(
            f"{dockerfile}: {len(now)} packages, " + ("as recorded" if not lines else "DIFFERENT")
        )
        for line in lines:
            print(f"    {line}")
        failed = failed or bool(lines)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
