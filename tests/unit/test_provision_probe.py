"""The provisioning role's probe (assay_tss.provision.probe), with fake AWS clients.

The real run is part of the isolation tests (`scripts/run_isolation.py`). These tests show
that the probe passes only when every data call is refused with an access-denied error and
the role's own settings calls work.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from assay_tss.policies import Principals
from assay_tss.provision import __main__ as entry
from assay_tss.provision.probe import ALLOWED, DENIED, Check, Probe, attempt, parse_tenants
from assay_tss.provision.settings import ProvisionSettings

TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
ROLE = "arn:aws:iam::000000000000:role"
SETTINGS = ProvisionSettings(
    region="us-east-1",
    account_id="000000000000",
    name_prefix="assay-dev",
    principals=Principals(
        tenant_role_arn=f"{ROLE}/tenant",
        break_glass_role_arn=f"{ROLE}/break-glass",
        provisioning_role_arn=f"{ROLE}/provisioning",
        scan_role_arn=f"{ROLE}/scan",
    ),
    state_bucket="state",
    state_key_arn="arn:aws:kms:us-east-1:000000000000:key/state",
    invite_function="invite",
    vector_dimensions=1024,
    default_tags={"app": "assay-insights"},
    work_dir=Path("unused"),
)
# The settings calls the role is allowed. Everything else must be refused.
SETTINGS_CALLS = {"get_bucket_versioning", "describe_key", "get_index"}


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Operation")


class FakeClient:
    """Refuses everything with AccessDenied except the calls named in `allow`."""

    def __init__(self, allow: set[str], overrides: dict[str, Exception | None] | None = None):
        self.allow = allow
        self.overrides = overrides or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            if name in self.overrides:
                failure = self.overrides[name]
                if failure is not None:
                    raise failure
                return {}
            if name not in self.allow:
                raise error("AccessDeniedException" if name != "get_object" else "AccessDenied")
            return {}

        return call


def probe(overrides: dict[str, Exception | None] | None = None) -> tuple[Probe, FakeClient]:
    client = FakeClient(SETTINGS_CALLS, overrides)
    return Probe(SETTINGS, s3=client, vectors=client, kms=client), client  # type: ignore[arg-type]


def by_name(results: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in results}


def test_probe_passes_when_every_data_call_is_refused() -> None:
    subject, client = probe()
    results = subject.run([TENANT])
    assert all(check.ok for check in results)
    expected = {check.name: check.expected for check in results}
    assert expected == {
        "read the bucket's settings": ALLOWED,
        "describe the key": ALLOWED,
        "describe the vector index": ALLOWED,
        "read an object": DENIED,
        "read an object's metadata": DENIED,
        "list object versions": DENIED,
        "write an object": DENIED,
        "list vectors": DENIED,
        "read vectors": DENIED,
        "query vectors": DENIED,
        "write vectors": DENIED,
        "generate a data key directly": DENIED,
        "encrypt directly": DENIED,
    }
    assert {name for name, _ in client.calls} >= SETTINGS_CALLS


def test_probe_targets_the_tenants_own_resources() -> None:
    subject, client = probe()
    subject.run([TENANT])
    calls = dict(client.calls)
    assert calls["get_object"]["Bucket"] == "assay-dev-t-aaaaaaaa"
    assert calls["get_object"]["Key"].startswith("originals/")
    assert calls["put_object"]["Key"].startswith("inbox/")
    assert calls["list_vectors"]["vectorBucketName"] == "assay-dev-t-aaaaaaaa-vectors"
    assert calls["list_vectors"]["indexName"] == "documents"
    assert len(calls["query_vectors"]["queryVector"]["float32"]) == 1024
    assert calls["generate_data_key"]["KeyId"] == "alias/assay-dev-t-aaaaaaaa"


def test_probe_runs_every_check_for_each_tenant() -> None:
    results = probe()[0].run([TENANT, OTHER])
    assert len(results) == 26
    assert {check.tenant_id for check in results} == {str(TENANT), str(OTHER)}


@pytest.mark.parametrize(
    ("call", "check"),
    [
        ("get_object", "read an object"),
        ("list_object_versions", "list object versions"),
        ("put_object", "write an object"),
        ("list_vectors", "list vectors"),
        ("get_vectors", "read vectors"),
        ("query_vectors", "query vectors"),
        ("generate_data_key", "generate a data key directly"),
    ],
)
def test_a_data_call_that_succeeds_fails_the_probe(call: str, check: str) -> None:
    results = by_name(probe({call: None})[0].run([TENANT]))
    assert results[check].outcome == ALLOWED
    assert not results[check].ok
    assert sum(not c.ok for c in results.values()) == 1


def test_a_missing_object_is_not_a_refusal() -> None:
    """NoSuchKey would mean the read was authorized: the object just was not there."""
    results = by_name(probe({"get_object": error("NoSuchKey")})[0].run([TENANT]))
    assert results["read an object"].outcome == "error:NoSuchKey"
    assert not results["read an object"].ok


def test_an_unreachable_endpoint_is_not_a_refusal() -> None:
    timeout = ConnectTimeoutError(endpoint_url="https://s3vectors.example")
    results = by_name(probe({"list_vectors": timeout})[0].run([TENANT]))
    assert results["list vectors"].outcome == "error:ConnectTimeoutError"
    assert not results["list vectors"].ok


def test_a_refused_settings_call_fails_the_probe() -> None:
    """If the role cannot even read settings, the refusals prove nothing."""
    results = by_name(probe({"get_index": error("AccessDeniedException")})[0].run([TENANT]))
    assert results["describe the vector index"].outcome == DENIED
    assert not results["describe the vector index"].ok


def test_attempt_distinguishes_refused_from_everything_else() -> None:
    def raises(exc: Exception) -> Any:
        def call() -> None:
            raise exc

        return call

    assert attempt(lambda: None) == ALLOWED
    for code in ("AccessDenied", "AccessDeniedException", "403", "Forbidden"):
        assert attempt(raises(error(code))) == DENIED
    assert attempt(raises(error("ValidationException"))) == "error:ValidationException"
    assert attempt(raises(error("NotFoundException"))) == "error:NotFoundException"


def test_parse_tenants() -> None:
    assert parse_tenants(f"{TENANT}, {OTHER}") == [TENANT, OTHER]
    with pytest.raises(ValueError, match="PROBE_TENANTS"):
        parse_tenants(" ")
    with pytest.raises(ValueError, match="badly formed"):
        parse_tenants("not-a-uuid")


# --- The task's entry point -----------------------------------------------------------------


class FakeProbe:
    results: list[Check] = []  # noqa: RUF012

    def __init__(self, settings: ProvisionSettings) -> None:
        self.settings = settings

    def run(self, tenants: list[uuid.UUID]) -> list[Check]:
        return self.results


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [(DENIED, 0), (ALLOWED, 1), ("error:NoSuchKey", 1)],
)
def test_probe_command_needs_no_request_and_reports_by_exit_code(
    monkeypatch: pytest.MonkeyPatch, outcome: str, exit_code: int
) -> None:
    FakeProbe.results = [Check(str(TENANT), "read an object", DENIED, outcome)]
    monkeypatch.setattr(entry, "Probe", FakeProbe)
    monkeypatch.setattr(ProvisionSettings, "from_env", classmethod(lambda cls: SETTINGS))
    monkeypatch.setenv("PROVISION_COMMAND", "probe")
    monkeypatch.setenv("PROBE_TENANTS", str(TENANT))
    monkeypatch.delenv("PROVISION_REQUEST", raising=False)
    try:
        assert entry.main() == exit_code
    finally:
        logging.basicConfig(force=True)
