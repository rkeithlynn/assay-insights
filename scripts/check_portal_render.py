"""Render the built portal in headless Chromium and run the layout checks (portal/e2e).

    uv run scripts/check_portal_render.py

Runs in the pinned Playwright image (linux/amd64), on a copy of `portal/` and `assets/`, so
nothing in the working tree changes: `npm ci --ignore-scripts`, the production build
(`vite build`), then `playwright test` at 1280 and 1800 px wide against `vite preview`.
Screenshots of every screen land in `.portal-render/screens/` (gitignored) for a person to
look at; the exit status is Playwright's. Needs Docker. Nothing here calls AWS.

The image's Playwright version must equal `@playwright/test` in portal/package.json, because
the image carries the browsers that version drives; `test_portal_render.py` checks it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".portal-render"
# v1.64.1-noble, pinned by digest. Change together with @playwright/test in package.json.
PLAYWRIGHT_VERSION = "1.64.1"
IMAGE = (
    "mcr.microsoft.com/playwright"
    "@sha256:2f05f8a734509a7b3f26a912639488ee08f58a7b292d14d62271bcff9038768f"
)

# Inside the container: copy the sources (read-only mount) to a scratch directory, build, test.
SCRIPT = """
set -e
mkdir -p /work/portal
cp -r /src/assets /work/
cd /src/portal
tar --exclude=node_modules --exclude=dist -cf - . | (cd /work/portal && tar xf -)
cd /work/portal
npm ci --ignore-scripts --no-fund --no-audit > /dev/null
npx vite build --logLevel warn
PORTAL_RENDER_OUT=/out/results npx playwright test "$@"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Anything else (for example --project=1800 or a test name) goes to `playwright test`.
    _, playwright_args = parser.parse_known_args()
    if shutil.which("docker") is None:
        print("Docker is needed to render the portal.", file=sys.stderr)
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--ipc=host",
        "--network",
        "bridge",
        "-v",
        f"{ROOT.as_posix()}:/src:ro",
        "-v",
        f"{OUT.as_posix()}:/out",
        IMAGE,
        "bash",
        "-c",
        SCRIPT,
        "render",
        *playwright_args,
    ]
    result = subprocess.run(command, check=False)  # noqa: S603 - fixed command, no shell
    print(f"Screenshots: {OUT / 'results' / 'screens'}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
