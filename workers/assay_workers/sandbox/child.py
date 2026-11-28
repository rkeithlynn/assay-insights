"""The sandbox child: `python -I -B -m assay_workers.sandbox.child <job> <path>`.

Started by `runner` as the sandbox user, with an empty environment, the resource limits and
the first seccomp filter already in place. It forbids itself any further `exec`, runs one
job on one file and prints one JSON object. It imports no AWS client and no database driver,
and has neither credentials nor a network to use them on.

A job that reads or draws pages (`jobs.PAGE_JOBS`) is started as
`... <job> <path> <descriptor> <arguments...>`: the descriptor is the page channel, a pipe the
parent opened and reads as the child writes. Such a job still prints one small JSON object of
numbers at the end.

Exit status: 0 with the result on stdout; `EXIT_MEMORY` when the address-space limit stopped
it; anything else is a failure the parent reports without reading further.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from assay_workers.sandbox import confine, jobs

EXIT_USAGE = 64
EXIT_MEMORY = 12


def _page_job(argv: list[str]) -> dict[str, object] | None:
    """`<job> <path> <descriptor> <arguments...>`: a job that writes to the page channel, the
    pipe the parent left open for it at that descriptor. None if that is not what was asked."""
    if len(argv) < 4 or not argv[3].isdigit() or int(argv[3]) < 3:
        return None
    with os.fdopen(int(argv[3]), "wb") as channel:
        return jobs.PAGE_JOBS[argv[1]](Path(argv[2]), channel, *argv[4:])


def main(argv: list[str]) -> int:
    paged = len(argv) > 1 and argv[1] in jobs.PAGE_JOBS
    if not paged and (len(argv) != 3 or argv[1] not in jobs.JOBS):
        return EXIT_USAGE
    confine.forbid_exec()
    try:
        result = _page_job(argv) if paged else jobs.JOBS[argv[1]](Path(argv[2]))
    except MemoryError:
        return EXIT_MEMORY
    if result is None:
        return EXIT_USAGE
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
