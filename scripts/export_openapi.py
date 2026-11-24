"""Write the API's OpenAPI schema for the portal's generated client (BUILD_SPEC.md section 10).

Usage (PowerShell, from the repository root), after changing a route:

    uv run scripts/export_openapi.py
    cd portal; npm run gen:api

The first writes `portal/src/api/openapi.json`; the second turns it into
`portal/src/api/schema.d.ts`. Commit both. `tests/unit/test_openapi.py` fails when the
committed schema no longer matches the API, and CI's portal job fails when `schema.d.ts`
does not match the schema.

`--check` writes nothing and exits 1 if the committed schema is out of date.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from assay_api.app import create_app
from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_api.services import CognitoAdmin, Provisioning

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "portal" / "src" / "api" / "openapi.json"


def schema() -> dict[str, Any]:
    """The schema only reads the routes, so the app gets stand-ins for its services."""
    app = create_app(
        verifier=cast(TokenVerifier, object()),
        databases=cast(Databases, object()),
        cognito=cast(CognitoAdmin, object()),
        provisioning=cast(Provisioning, object()),
    )
    return app.openapi()


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    text = render()
    if args.check:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != text:
            print(f"{SCHEMA_PATH} is out of date: run uv run scripts/export_openapi.py")
            return 1
        return 0
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {SCHEMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
