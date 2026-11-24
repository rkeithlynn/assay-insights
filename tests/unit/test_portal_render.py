"""The rendered portal checks (portal/e2e) run on the browsers they were written for.

The Playwright image carries the browsers of one Playwright release; `@playwright/test` in
portal/package.json must be that release, and CI must use the same image as
`scripts/check_portal_render.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_portal_render", ROOT / "scripts" / "check_portal_render.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_image_is_pinned_by_digest() -> None:
    image = _script().IMAGE
    assert re.fullmatch(r"mcr\.microsoft\.com/playwright@sha256:[0-9a-f]{64}", image)


def test_playwright_in_the_portal_matches_the_image() -> None:
    package = json.loads((ROOT / "portal" / "package.json").read_text(encoding="utf-8"))
    assert package["devDependencies"]["@playwright/test"] == _script().PLAYWRIGHT_VERSION
    lock = json.loads((ROOT / "portal" / "package-lock.json").read_text(encoding="utf-8"))
    for name in ("@playwright/test", "playwright", "playwright-core"):
        assert lock["packages"][f"node_modules/{name}"]["version"] == _script().PLAYWRIGHT_VERSION


def test_ci_renders_with_the_same_image() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    images = re.findall(r"image:\s*(mcr\.microsoft\.com/playwright@sha256:[0-9a-f]{64})", workflow)
    assert images == [_script().IMAGE]


def test_both_desktop_widths_are_rendered() -> None:
    config = (ROOT / "portal" / "playwright.config.ts").read_text(encoding="utf-8")
    widths = {int(w) for w in re.findall(r"viewport: \{ width: (\d+),", config)}
    assert widths == {1280, 1800}
