"""The portal's API client is generated from the committed schema; it must match the API."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def test_committed_openapi_schema_matches_the_api() -> None:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    committed = module.SCHEMA_PATH.read_text(encoding="utf-8")
    assert committed == module.render(), (
        "portal/src/api/openapi.json is out of date: run uv run scripts/export_openapi.py, "
        "then npm run gen:api in portal/"
    )
