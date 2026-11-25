"""Lambda entry point: run `tests/isolation` with pytest and return the results.

Started by `scripts/run_isolation.py` with the two test tenants:

    {"tenant_a": "<tenant id>", "tenant_b": "<tenant id>"}

A skipped test counts against the run: `ok` is true only when every test passed. Failure
details never include credentials (they are `SecretStr` values, and tracebacks are short).

With an `action`, it prepares or clears the worker probe's fixture instead
(`tests/isolation/worker_fixture.py`): `worker_fixture`, `end_worker_grant`,
`worker_cleanup`. The probe itself runs as the worker task, not here.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
MAX_DETAIL = 2000


class Collector:
    """Collects one result per test."""

    def __init__(self) -> None:
        self.results: list[dict[str, str]] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # The call phase, or a setup or teardown that did not pass.
        if report.when != "call" and report.outcome == "passed":
            return
        self.results.append(
            {
                "test": report.nodeid,
                "phase": report.when or "",
                "outcome": report.outcome,
                "detail": "" if report.passed else str(report.longrepr)[-MAX_DETAIL:],
            }
        )


FIXTURE_ACTIONS = frozenset({"worker_fixture", "end_worker_grant", "worker_cleanup"})


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    if event.get("action") in FIXTURE_ACTIONS:
        from tests.isolation import worker_fixture

        return worker_fixture.handle(event)
    os.environ["ISOLATION_TENANT_A"] = str(uuid.UUID(str(event["tenant_a"])))
    os.environ["ISOLATION_TENANT_B"] = str(uuid.UUID(str(event["tenant_b"])))
    collector = Collector()
    exit_code = pytest.main(
        [
            str(TESTS_DIR),
            f"--rootdir={TESTS_DIR}",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "-q",
        ],
        plugins=[collector],
    )
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for result in collector.results:
        counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
    return {
        "ok": int(exit_code) == 0 and counts["passed"] > 0 and counts["skipped"] == 0,
        "exit_code": int(exit_code),
        **counts,
        "tests": collector.results,
    }
