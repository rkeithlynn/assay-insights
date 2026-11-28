"""scripts/add_tenant.py and scripts/run_isolation.py: the parts that need no AWS."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
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


# --- The provisioning role's probe, as run_isolation.py starts and reads it ------------------


def probe_line(check: str, *, ok: bool, outcome: str = "denied") -> str:
    return json.dumps(
        {
            "time": "2026-11-23T23:00:00+00:00",
            "level": "INFO",
            "message": "probe",
            "check": check,
            "tenant_id": str(TENANT),
            "expected": "denied",
            "outcome": outcome,
            "ok": ok,
        }
    )


def test_probe_task_is_started_with_the_probe_command_and_both_tenants() -> None:
    other = uuid.uuid4()
    overrides = run_isolation.probe_overrides((TENANT, other))
    [container] = overrides["containerOverrides"]
    assert container["name"] == "provision"
    assert container["environment"] == [
        {"name": "PROVISION_COMMAND", "value": "probe"},
        {"name": "PROBE_TENANTS", "value": f"{TENANT},{other}"},
    ]


def test_probe_results_are_read_from_the_tasks_log_lines() -> None:
    messages = [
        "not json",
        json.dumps({"message": "probe finished", "checks": 2, "failed": 0}),
        probe_line("read an object", ok=True),
        probe_line("list vectors", ok=False, outcome="allowed"),
    ]
    checks = run_isolation.probe_checks(messages)
    assert [c["check"] for c in checks] == ["read an object", "list vectors"]


def test_probe_report_passes_only_with_results_all_ok_and_exit_code_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = run_isolation.probe_checks([probe_line("read an object", ok=True)])
    bad = run_isolation.probe_checks(
        [
            probe_line("read an object", ok=True),
            probe_line("list vectors", ok=False, outcome="allowed"),
        ]
    )
    assert run_isolation.report_probe(good, 0) is True
    assert run_isolation.report_probe(bad, 1) is False
    printed = capsys.readouterr().out
    assert "FAIL  provisioning role, tenant aaaaaaaa: list vectors" in printed
    assert "(expected denied, was allowed)" in printed
    # No results is never a pass, and neither is a task that failed for another reason.
    assert run_isolation.report_probe([], 0) is False
    assert run_isolation.report_probe(good, 1) is False
    assert run_isolation.report_probe(good, None) is False


# --- The worker task's probe, as run_isolation.py starts and reads it ------------------------


def worker_line(check: str, *, ok: bool, outcome: str = "denied") -> str:
    return json.dumps(
        {
            "level": "INFO",
            "message": "probe",
            "check": check,
            "expected": "denied",
            "outcome": outcome,
            "ok": ok,
        }
    )


def test_worker_probe_is_started_with_the_probe_command_and_its_cases() -> None:
    cases = {"own": {"document_id": str(TENANT), "attempt": 1, "token": "t" * 43}}
    [container] = run_isolation.worker_overrides(cases)["containerOverrides"]
    assert container["name"] == "worker"
    assert container["environment"][0] == {"name": "WORKER_COMMAND", "value": "probe"}
    assert container["environment"][1]["name"] == "PROBE_CASES"
    assert json.loads(container["environment"][1]["value"]) == cases


def test_worker_probe_lines_carry_checks_and_the_ready_signal() -> None:
    ready = json.dumps({"message": "probe", "signal": "ready_for_end", "document_id": "d"})
    messages = [
        "not json",
        json.dumps({"message": "worker finished"}),
        worker_line("storage: the other tenant's document", ok=True),
        ready,
    ]
    entries = run_isolation.worker_entries(messages)
    assert len(entries) == 2
    assert any(e.get("signal") == run_isolation.WORKER_READY_FOR_END for e in entries)
    # The signal is the probe's own word for it.
    from assay_workers.worker import probe

    assert run_isolation.WORKER_READY_FOR_END == probe.READY_FOR_END
    assert run_isolation.WORKER_CONTAINER == "worker"


def test_worker_probe_report_passes_only_with_results_all_ok_and_exit_code_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready = json.dumps({"message": "probe", "signal": "ready_for_end"})
    good = run_isolation.worker_entries([worker_line("a check", ok=True), ready])
    bad = run_isolation.worker_entries(
        [worker_line("a check", ok=True), worker_line("storage: x", ok=False, outcome="allowed")]
    )
    assert run_isolation.report_worker_probe(good, 0) is True
    assert run_isolation.report_worker_probe(bad, 1) is False
    assert run_isolation.report_worker_probe(good, 1) is False
    printed = capsys.readouterr().out
    assert "FAIL  worker task: storage: x (expected denied, was allowed)" in printed
    # No results is never a pass: a task that never ran proves nothing.
    assert run_isolation.report_worker_probe([], 0) is False
    assert run_isolation.report_worker_probe(run_isolation.worker_entries([ready]), 0) is False
