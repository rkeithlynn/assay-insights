"""The pipeline and the API never work from a listing of any prefix (build step 6).

They work from database rows and scan events only; no session they get may list a bucket
either (assay_tss.policies, decision 16). This test reads the source: no listing call may
appear in the API, the pipeline or the shared schemas. The isolation tests and the
provisioning role's probe list on purpose (to prove a refusal, or the one key they read),
and are not part of either.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKED = ("api/assay_api", "workers/assay_workers", "schemas/assay_schemas")
LISTING_CALLS = frozenset(
    {
        "list_objects",
        "list_objects_v2",
        "list_object_versions",
        "list_multipart_uploads",
        "list_vectors",
        "get_paginator",
        "ListObjects",
        "ListObjectsV2",
        "ListObjectVersions",
    }
)


def _sources() -> list[Path]:
    return sorted(p for tree in CHECKED for p in (ROOT / tree).rglob("*.py"))


def test_there_are_sources_to_check() -> None:
    names = {p.name for p in _sources()}
    assert {"scan.py", "steps.py", "sweeper.py", "batches.py", "storage.py"} <= names


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_listing_call(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        assert name not in LISTING_CALLS, f"{path.name}: {name}"
