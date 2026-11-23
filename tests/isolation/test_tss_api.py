"""The TSS's private API refuses what it should (BUILD_SPEC.md section 6, rule 2)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import pytest

from assay_tss.client import TssClient, TssRequestError
from assay_tss.models import StorageAccess
from tests.isolation.conftest import Env


def test_unsigned_request_is_refused(env: Env) -> None:
    body = json.dumps(
        {
            "tenant_id": str(env.tenant_a),
            "access": StorageAccess.READ.value,
            "source_identity": f"{env.service}.probe",
        }
    ).encode()
    assert env.tss_endpoint.startswith("https://")
    request = urllib.request.Request(  # noqa: S310
        f"{env.tss_endpoint}/resolve",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(request, timeout=20)  # noqa: S310
    assert raised.value.code == 403


def test_unknown_tenant_gets_no_context(env: Env, tss: TssClient) -> None:
    with pytest.raises(TssRequestError) as raised:
        tss.resolve(uuid.uuid4(), StorageAccess.READ, f"{env.service}.probe")
    assert (raised.value.status, raised.value.code) == (404, "no_storage_for_tenant")


def test_caller_cannot_use_another_services_name(env: Env, tss: TssClient) -> None:
    """The service part of the source identity comes from the caller's IAM role."""
    with pytest.raises(TssRequestError) as raised:
        tss.resolve(env.tenant_a, StorageAccess.READ, "api.probe")
    assert (raised.value.status, raised.value.code) == (403, "source_identity_mismatch")


def test_unknown_route_is_refused(env: Env, tss: TssClient) -> None:
    with pytest.raises(TssRequestError) as raised:
        tss.send(tss.signed_request("/provision", b"{}"))
    assert raised.value.status in {403, 404}
