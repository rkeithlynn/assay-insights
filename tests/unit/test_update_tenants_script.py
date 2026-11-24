"""scripts/update_tenants.py: after an update, each tenant's malware protection plan must be
ACTIVE and scan exactly inbox/, or the script fails loudly."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "update_tenants.py"
BUCKET = "assay-dev-t-cfd08356"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_tenants", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_tenants"] = module
    spec.loader.exec_module(module)
    return module


class FakeGuardDuty:
    """Plans by ID; `statuses` is what successive reads of the tenant's plan return."""

    def __init__(self, statuses: list[str], prefixes: list[str] | None = None) -> None:
        self.statuses = statuses
        self.prefixes = ["inbox/"] if prefixes is None else prefixes
        self.reads = 0

    def list_malware_protection_plans(self, **kwargs: Any) -> dict[str, Any]:
        if "NextToken" not in kwargs:
            return {
                "MalwareProtectionPlans": [{"MalwareProtectionPlanId": "other"}],
                "NextToken": "p2",
            }
        return {"MalwareProtectionPlans": [{"MalwareProtectionPlanId": "mine"}]}

    def get_malware_protection_plan(self, MalwareProtectionPlanId: str) -> dict[str, Any]:
        if MalwareProtectionPlanId == "other":
            return {
                "ProtectedResource": {"S3Bucket": {"BucketName": "someone-else"}},
                "Status": "ACTIVE",
            }
        status = self.statuses[min(self.reads, len(self.statuses) - 1)]
        self.reads += 1
        return {
            "ProtectedResource": {
                "S3Bucket": {"BucketName": BUCKET, "ObjectPrefixes": self.prefixes}
            },
            "Status": status,
            "StatusReasons": [] if status == "ACTIVE" else [{"Code": "X", "Message": "why"}],
        }


def check(script: ModuleType, guardduty: FakeGuardDuty) -> str | None:
    result: str | None = script.check_plan(
        guardduty, BUCKET, settle_seconds=30, poll_seconds=15, sleep=lambda _: None
    )
    return result


def test_an_active_plan_scanning_inbox_passes(script: ModuleType) -> None:
    assert check(script, FakeGuardDuty(["ACTIVE"])) is None


def test_a_status_that_settles_to_active_passes(script: ModuleType) -> None:
    guardduty = FakeGuardDuty(["WARNING", "ACTIVE"])
    assert check(script, guardduty) is None
    assert guardduty.reads == 2


def test_a_plan_that_stays_inactive_fails(script: ModuleType) -> None:
    problem = check(script, FakeGuardDuty(["ERROR"]))
    assert problem is not None and "ERROR" in problem and "not ACTIVE" in problem


def test_wrong_prefixes_fail(script: ModuleType) -> None:
    for prefixes in ([], ["inbox/", "originals/"], [""]):
        problem = check(script, FakeGuardDuty(["ACTIVE"], prefixes))
        assert problem is not None and "scans" in problem, prefixes


def test_a_missing_plan_fails(script: ModuleType) -> None:
    class NoPlans(FakeGuardDuty):
        def list_malware_protection_plans(self, **kwargs: Any) -> dict[str, Any]:
            return {"MalwareProtectionPlans": []}

    problem = check(script, NoPlans(["ACTIVE"]))
    assert problem is not None and "no malware protection plan" in problem


def test_the_planned_status_is_printed_from_and_to(script: ModuleType) -> None:
    line = script.describe(
        {
            "message": "planned change",
            "tenant_id": "t",
            "resource": "malware-protection",
            "type": "aws:guardduty/malwareProtectionPlan:MalwareProtectionPlan",
            "op": "update",
            "diffs": ["protectedResource", "status"],
            "outputs": ["status: ACTIVE -> (known after update)"],
        }
    )
    assert "output status: ACTIVE -> (known after update)" in line
    assert script.SCANNED_PREFIXES == ["inbox/"]
