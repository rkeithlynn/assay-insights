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


def test_the_workers_route_refuses_every_other_caller(env: Env, tss: TssClient) -> None:
    """`/resolve-worker` serves the pipeline's worker task only (owner condition b, build
    step 6). This Lambda's role is not it: API Gateway refuses the request before the service
    sees it, because the role's policy and the API's policy name only `/resolve` for it.
    The worker's own side is tried by its probe (`assay_workers.worker.probe`)."""
    with pytest.raises(TssRequestError) as raised:
        tss.resolve_worker(uuid.uuid4(), 1, "A" * 43)
    assert raised.value.status == 403
    assert raised.value.code != "worker_grant_refused"  # it never reached a grant lookup


def test_worker_access_cannot_be_asked_for_by_name(env: Env, tss: TssClient) -> None:
    """No caller of `/resolve` is issued the worker's access, for any tenant."""
    with pytest.raises(TssRequestError) as raised:
        tss.resolve(env.tenant_a, StorageAccess.WORKER, f"{env.service}.probe")
    assert (raised.value.status, raised.value.code) == (403, "access_not_allowed")
