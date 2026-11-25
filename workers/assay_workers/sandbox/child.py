"""The sandbox child: `python -I -B -m assay_workers.sandbox.child <job> <path>`.

Started by `runner` as the sandbox user, with an empty environment, the resource limits and
the first seccomp filter already in place. It forbids itself any further `exec`, runs one
job on one file and prints one JSON object. It imports no AWS client and no database driver,
and has neither credentials nor a network to use them on.

Exit status: 0 with the result on stdout; `EXIT_MEMORY` when the address-space limit stopped
it; anything else is a failure the parent reports without reading further.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from assay_workers.sandbox import confine, jobs

EXIT_USAGE = 64
EXIT_MEMORY = 12


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in jobs.JOBS:
        return EXIT_USAGE
    confine.forbid_exec()
    try:
        result = jobs.JOBS[argv[1]](Path(argv[2]))
    except MemoryError:
        return EXIT_MEMORY
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
