"""CI: have every pinned base image in Docker before the image tests build from it.

    uv run scripts/ci_base_images.py key
    uv run scripts/ci_base_images.py ensure --cache <directory>

The image tests (`tests/unit/test_*image*.py`) build the project's Dockerfiles, and a build
asks the registry for each `FROM` image. The registries are public and the CI runner is not
signed in to any of them, so it shares their anonymous limits with everyone else on GitHub's
addresses: Amazon ECR Public answered `429 Too Many Requests` for the Python base image and
failed the `python` job during build step 5 and again on 2026-11-27.

So the job does this first, in one step:

1. `docker load` every image that the workflow's cache restored into `<directory>`.
2. For each pinned image that Docker still does not have under its pinned name: `docker pull`,
   and after a failure wait and try again, with growing waits (`DELAYS`), before giving up.
3. `docker save` each image that has no file in `<directory>` yet, for the cache to keep.

A build then finds each base image in Docker and does not ask the registry for it.

What the cache spares depends on Docker's image store. Where a loaded image keeps its pinned
name, step 2 has nothing to do and no request is made at all. Where it does not, step 2
still pulls, but the layers are there: the pull is a request for the manifest alone. Either
way the request that remains is retried.

`key` prints a hash of the pinned names, for the cache's key: a new digest in a Dockerfile is
a new cache.

No sign-in and no credentials, on purpose (owner, 2026-11-27): every image is public, and it
is pinned by digest, so what is loaded or pulled can only be the image the Dockerfile names.
The names come from the Dockerfiles themselves and are written nowhere else.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Never walked: nothing in them is a Dockerfile of this project.
SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", ".portal-render", ".test-files"})
# The portal's image is built at deploy time only (`scripts/build_portal.py`), never in CI.
NOT_BUILT_IN_CI = frozenset({"portal"})
# `FROM <name>@sha256:<digest>`, with or without a platform and a stage name.
PINNED_FROM = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(\S+@sha256:[0-9a-f]{64})(?:\s|$)", re.IGNORECASE | re.MULTILINE
)
# Seconds to wait after each failed pull before the next try: seven tries in all, about six
# minutes at most. A quarter of each wait is added at random, so that jobs that failed
# together do not all come back together.
DELAYS: tuple[float, ...] = (5, 15, 30, 60, 120, 120)
JITTER = 0.25

Run = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def docker(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # A fixed program, with arguments from this file and the Dockerfiles; no shell.
    return subprocess.run(  # noqa: S603  # nosec B603 B607
        ["docker", *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


def dockerfiles(root: Path = ROOT) -> list[Path]:
    """Every Dockerfile the image tests can build: all of them but the portal's."""
    found = []
    for directory, names, files in os.walk(root):
        here = Path(directory)
        names[:] = sorted(
            n
            for n in names
            if n not in SKIPPED_DIRECTORIES and not (here == root and n in NOT_BUILT_IN_CI)
        )
        found += [here / f for f in files if f == "Dockerfile" or f.endswith(".Dockerfile")]
    return sorted(found)


def images(root: Path = ROOT) -> list[str]:
    """The pinned base images those Dockerfiles build from, by their pinned names."""
    names: set[str] = set()
    for path in dockerfiles(root):
        names.update(PINNED_FROM.findall(path.read_text(encoding="utf-8")))
    return sorted(names)


def cache_key(names: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:32]


def cache_file(directory: Path, name: str) -> Path:
    """IDs and fixed text only: the file is named by a hash of the image's pinned name."""
    return directory / f"{hashlib.sha256(name.encode()).hexdigest()[:32]}.tar"


def present(name: str, run: Run = docker) -> bool:
    """Docker has the image under its pinned name, so a build will not ask the registry."""
    return run(["image", "inspect", "--format", "{{.Id}}", name]).returncode == 0


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no message"


class PullFailed(Exception):
    """Every try failed. Carries what the registry or Docker said the last time."""


def pull(
    name: str,
    *,
    run: Run = docker,
    sleep: Callable[[float], None] = time.sleep,
    # Spreads retries out; nothing secret depends on it.
    jitter: Callable[[float], float] = lambda wait: random.uniform(0, wait * JITTER),  # noqa: S311  # nosec B311
    delays: Sequence[float] = DELAYS,
    say: Callable[[str], None] = print,
) -> int:
    """Pull one image, trying again after each failure. Returns how many tries it took."""
    tries = len(delays) + 1
    for attempt in range(1, tries + 1):
        result = run(["pull", "--quiet", name])
        if result.returncode == 0:
            return attempt
        reason = last_line(result.stderr or result.stdout)
        if attempt == tries:
            raise PullFailed(f"{name}: {tries} tries failed; the last answer: {reason}")
        wait = delays[attempt - 1]
        wait += jitter(wait)
        say(f"  try {attempt} of {tries} failed ({reason}); waiting {wait:.0f} s")
        sleep(wait)
    raise AssertionError("unreachable")


def load_cache(directory: Path, run: Run = docker, say: Callable[[str], None] = print) -> int:
    """`docker load` every cached file. A file Docker cannot load is removed: the image is
    pulled instead, and saved again."""
    loaded = 0
    for path in sorted(directory.glob("*.tar")):
        result = run(["load", "--quiet", "--input", str(path)])
        if result.returncode == 0:
            loaded += 1
        else:
            say(f"  a cached file could not be loaded and was removed ({last_line(result.stderr)})")
            path.unlink()
    return loaded


def ensure(
    directory: Path,
    names: Sequence[str],
    *,
    run: Run = docker,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> int:
    """Load, pull what is missing, save what is new. Returns the exit status."""
    directory.mkdir(parents=True, exist_ok=True)
    say(f"{len(names)} pinned base images; {load_cache(directory, run, say)} loaded from the cache")
    for name in names:
        if present(name, run):
            say(f"present, no request made: {name}")
            continue
        say(f"pulling: {name}")
        try:
            tries = pull(name, run=run, sleep=sleep, say=say)
        except PullFailed as failed:
            say(f"FAILED: {failed}")
            return 1
        say(f"  pulled on try {tries}")
    for name in names:
        target = cache_file(directory, name)
        if target.exists():
            continue
        result = run(["save", "--output", str(target), name])
        if result.returncode != 0:
            # The images are in Docker, which is what the tests need. Only the cache is poorer.
            say(f"  not saved for the cache ({last_line(result.stderr)}): {name}")
            target.unlink(missing_ok=True)
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key", help="print the cache key for the pinned base images")
    ensured = commands.add_parser("ensure", help="load, pull what is missing, save what is new")
    ensured.add_argument("--cache", type=Path, required=True, help="the cache's directory")
    options = parser.parse_args(arguments)
    names = images()
    if not names:
        print("no pinned base image found in any Dockerfile", file=sys.stderr)
        return 1
    if options.command == "key":
        print(cache_key(names))
        return 0
    return ensure(options.cache, names)


if __name__ == "__main__":
    sys.exit(main())
