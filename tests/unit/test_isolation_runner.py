"""The isolation test runner's Lambda handler (tests/isolation/runner.py).

The real tests need the VPC, so this points the handler at a small throwaway test directory
and checks how it reports: every result is listed, and a failed or skipped test means the
run is not ok. The handler runs in its own interpreter, as it does in Lambda.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT_A, TENANT_B = str(uuid.uuid4()), str(uuid.uuid4())

PASSING = """
import os

def test_tenants_arrive_in_the_environment():
    assert os.environ["ISOLATION_TENANT_A"] != os.environ["ISOLATION_TENANT_B"]
"""
FAILING = """
def test_refused():
    assert False, "expected access denied, got 'NoSuchKey'"
"""
SKIPPING = """
import pytest

def test_skipped():
    pytest.skip("endpoint unreachable")
"""

SCRIPT = """
import json, sys
from pathlib import Path
from tests.isolation import runner

runner.TESTS_DIR = Path(sys.argv[1])
result = runner.handler({"tenant_a": sys.argv[2], "tenant_b": sys.argv[3]}, None)
print("RESULT " + json.dumps(result))
"""


def run_handler(tests_dir: Path, tenant_a: str = TENANT_A, tenant_b: str = TENANT_B) -> Any:
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tests_dir), tenant_a, tenant_b],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    if not lines:
        pytest.fail(f"the handler printed no result:\n{completed.stdout}\n{completed.stderr}")
    return json.loads(lines[-1].removeprefix("RESULT "))


def write(directory: Path, **files: str) -> Path:
    for name, source in files.items():
        (directory / f"test_{name}.py").write_text(source)
    return directory


def test_all_passing_is_ok(tmp_path: Path) -> None:
    result = run_handler(write(tmp_path, good=PASSING))
    assert result["ok"] is True
    assert (result["passed"], result["failed"], result["skipped"]) == (1, 0, 0)
    [test] = result["tests"]
    assert test["outcome"] == "passed" and test["detail"] == ""


def test_one_failure_fails_the_run_and_says_why(tmp_path: Path) -> None:
    result = run_handler(write(tmp_path, good=PASSING, bad=FAILING))
    assert result["ok"] is False
    assert (result["passed"], result["failed"]) == (1, 1)
    [failed] = [t for t in result["tests"] if t["outcome"] == "failed"]
    assert "test_refused" in failed["test"]
    assert "expected access denied" in failed["detail"]


def test_a_skipped_test_is_not_a_pass(tmp_path: Path) -> None:
    result = run_handler(write(tmp_path, good=PASSING, skip=SKIPPING))
    assert result["ok"] is False
    assert result["skipped"] == 1


def test_no_tests_is_not_a_pass(tmp_path: Path) -> None:
    assert run_handler(tmp_path)["ok"] is False


def test_tenant_ids_must_be_uuids(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path), "not-a-uuid", TENANT_B],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode != 0
    assert "ValueError" in completed.stderr
