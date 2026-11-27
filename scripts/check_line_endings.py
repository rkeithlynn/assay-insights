"""Fail if a file that `.gitattributes` marks as LF has CRLF line endings on disk.

Usage (PowerShell, from the repository root):

    uv run scripts/check_line_endings.py          # list the files, exit 1 if there are any
    uv run scripts/check_line_endings.py --fix    # rewrite them with LF line endings

Why it matters. Git stores these files with LF and compares them that way, so a file saved
with CRLF shows no difference in `git status` or `git diff`. But a container image is built
from the bytes on disk, and its tag is their hash (`assay_infra.images.source_tag`): the
image then holds the CRLF copy, its tag differs from a clean checkout's, and it rebuilds the
next time Git rewrites the file. It happened twice (2026-11-23 and 2026-11-25).

Git itself never does it: a file marked `eol=lf` is checked out with LF whatever
`core.autocrlf` says (checked 2026-11-27 with a fresh clone under `core.autocrlf=true`). An
editor or a script that saves with the platform's line endings does: on Windows, Python's
`Path.write_text()` writes CRLF unless it is given `newline="\\n"`.

What this checks: every tracked file, and every untracked file that is not ignored, whose
`eol` attribute is `lf` (`git ls-files --eol`). It reads nothing but what Git reports.
`--fix` changes line endings only, in the working tree only; Git sees no change.

Also run by `tests/unit/test_line_endings.py` (so `uv run pytest` fails on the developer's
machine, where the problem arises) and by CI (where it can only catch CRLF that was
committed, because a Linux checkout has none).
"""

from __future__ import annotations

import argparse
import shutil

# Runs git with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LF_ATTRIBUTE = "eol=lf"
# What `git ls-files --eol` calls line endings that are not all LF.
NOT_LF = frozenset({"crlf", "mixed"})


@dataclass(frozen=True)
class Entry:
    """One line of `git ls-files --eol`: the endings in the index and on disk, and whether
    the file's attributes say LF."""

    path: str
    index: str
    worktree: str
    marked_lf: bool

    @property
    def wrong_on_disk(self) -> bool:
        return self.marked_lf and self.worktree in NOT_LF

    @property
    def wrong_in_git(self) -> bool:
        return self.marked_lf and self.index in NOT_LF


def parse(listing: str) -> list[Entry]:
    """`i/lf    w/crlf  attr/text eol=lf      \\tpath`, one file a line."""
    entries = []
    for line in listing.splitlines():
        info, tab, path = line.partition("\t")
        fields = info.split()
        if not tab or len(fields) < 3:
            continue
        index, worktree = fields[0], fields[1]
        if not (index.startswith("i/") and worktree.startswith("w/")):
            continue
        entries.append(
            Entry(
                path=path,
                index=index[2:],
                worktree=worktree[2:],
                marked_lf=LF_ATTRIBUTE in fields[2:],
            )
        )
    return entries


def git(root: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        sys.exit("git not found on PATH")
    result = subprocess.run(  # noqa: S603  # nosec B603
        [exe, "-c", "core.quotepath=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def entries(root: Path = REPO_ROOT) -> list[Entry]:
    """Every tracked file, and every untracked file that is not ignored."""
    tracked = parse(git(root, "ls-files", "--eol"))
    untracked = parse(git(root, "ls-files", "--eol", "--others", "--exclude-standard"))
    return tracked + untracked


def to_lf(path: Path) -> bool:
    """Rewrite one file with LF line endings. Returns whether it changed."""
    before = path.read_bytes()
    after = before.replace(b"\r\n", b"\n")
    if after == before:
        return False
    path.write_bytes(after)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--fix", action="store_true", help="rewrite the files with LF")
    args = parser.parse_args()

    found = entries()
    on_disk = sorted(e.path for e in found if e.wrong_on_disk)
    in_git = sorted(e.path for e in found if e.wrong_in_git)
    marked = sum(1 for e in found if e.marked_lf)
    if args.fix:
        for path in on_disk:
            to_lf(REPO_ROOT / path)
            print(f"  rewritten with LF: {path}")
        on_disk = sorted(e.path for e in entries() if e.wrong_on_disk)

    for path in on_disk:
        print(f"  CRLF on disk: {path}")
    for path in in_git:
        print(f"  CRLF in Git: {path}")
    if on_disk:
        print(
            f"{len(on_disk)} file(s) that .gitattributes marks as LF have CRLF line endings on "
            "disk. Fix them with: uv run scripts/check_line_endings.py --fix"
        )
    if in_git:
        print(
            f"{len(in_git)} file(s) that .gitattributes marks as LF were committed with CRLF. "
            "Fix them with: git add --renormalize . (then commit)"
        )
    if on_disk or in_git:
        return 1
    print(f"ok: all {marked} files that .gitattributes marks as LF have LF line endings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
