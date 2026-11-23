"""scripts/add_tenant.py and scripts/run_isolation.py: the parts that need no AWS."""

from __future__ import annotations

import datetime as dt
import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest

from assay_tss.provision.task import ProvisionRequest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OPERATOR = "arn:aws:sts::000000000000:assumed-role/Admin/someone"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


add_tenant = load("add_tenant")
run_isolation = load("run_isolation")


def test_execution_names_identify_the_tenant_and_fit_the_limit() -> None:
    name = add_tenant.execution_name(TENANT, dt.datetime(2026, 11, 23, 16, 5, 9, tzinfo=dt.UTC))
    assert name == f"t-{TENANT}-20261123160509"
    assert name.startswith(add_tenant.execution_prefix(TENANT))
    assert len(name) <= 80  # Step Functions' limit


def test_new_request_is_what_the_provisioning_task_accepts() -> None:
    request = add_tenant.new_request(
        tenant_id=TENANT,
        name="Isolation Test A",
        admin_name="Ada Admin",
        admin_email="ada@example.test",
        principal=OPERATOR,
    )
    parsed = ProvisionRequest.model_validate(request)
    assert parsed.tenant_id == TENANT
    assert parsed.retry is False
    assert (parsed.actor.role, parsed.actor.principal) == ("operator", OPERATOR)
    assert parsed.first_admin.email == "ada@example.test"


def test_retry_reuses_the_last_request_and_records_who_retried() -> None:
    first = add_tenant.new_request(
        tenant_id=TENANT,
        name="Isolation Test A",
        admin_name="Ada Admin",
        admin_email="ada@example.test",
        principal="arn:aws:sts::000000000000:assumed-role/Admin/first-operator",
    )
    again = add_tenant.retry_request(first, OPERATOR)
    parsed = ProvisionRequest.model_validate(again)
    assert parsed.retry is True
    assert parsed.actor.principal == OPERATOR
    assert (parsed.tenant_id, parsed.name, parsed.first_admin) == (
        TENANT,
        "Isolation Test A",
        ProvisionRequest.model_validate(first).first_admin,
    )
    assert first["retry"] is False  # the original is not changed


def test_isolation_tenants_come_as_two_different_ids() -> None:
    other = uuid.uuid4()
    assert run_isolation.parse_tenants(f" {TENANT} , {other} ") == (TENANT, other)
    for bad in (str(TENANT), f"{TENANT},{TENANT}", f"{TENANT},{other},{uuid.uuid4()}", ""):
        with pytest.raises(ValueError, match="tenant ID"):
            run_isolation.parse_tenants(bad)


def test_isolation_report_passes_only_when_the_runner_says_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = {
        "ok": False,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "tests": [
            {"test": "test_a", "outcome": "passed", "detail": ""},
            {"test": "test_b", "outcome": "failed", "detail": "expected access denied"},
        ],
    }
    assert run_isolation.report(result) is False
    printed = capsys.readouterr().out
    assert "PASS  test_a" in printed and "FAIL  test_b" in printed
    assert "expected access denied" in printed
    assert "1 passed, 1 failed, 0 skipped" in printed
    assert run_isolation.report({"ok": True, "passed": 2, "tests": []}) is True
