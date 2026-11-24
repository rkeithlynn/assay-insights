"""`resolve`, the Lambda handler and the client, with a fake database reader and a fake STS."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from assay_tss import handler as tss_handler
from assay_tss import naming, service
from assay_tss.client import TssClient
from assay_tss.models import (
    IsolationMode,
    ResolveRequest,
    StorageAccess,
    StorageStatus,
    TenantStorageProfile,
)
from assay_tss.service import CallerGrant, Resolver, ServiceSettings

REGION = "us-east-1"
ACCOUNT = "000000000000"
PREFIX = "assay-dev"
TENANT_ROLE = f"arn:aws:iam::{ACCOUNT}:role/assay-dev-tenant"
TENANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
TENANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
# A caller allowed every access, so the tests below can ask for any of them.
API = CallerGrant(service="api", access=frozenset(StorageAccess))
API_NARROW = CallerGrant(
    service="api", access=frozenset({StorageAccess.READ, StorageAccess.UPLOAD})
)
SETTINGS = ServiceSettings(
    region=REGION,
    name_prefix=PREFIX,
    tenant_role_arn=TENANT_ROLE,
    callers={
        "api-task-role-1a2b3c": API_NARROW,
        "worker-role-9z": CallerGrant(service="worker", access=frozenset({StorageAccess.PIPELINE})),
    },
)
API_CALLER = f"arn:aws:sts::{ACCOUNT}:assumed-role/api-task-role-1a2b3c/session-1"
EXPIRATION = dt.datetime(2026, 11, 23, 12, 15, tzinfo=dt.UTC)


def profile(tenant_id: uuid.UUID, **changes: Any) -> TenantStorageProfile:
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "mode": IsolationMode.DEDICATED,
        "bucket": naming.bucket_name(PREFIX, tenant_id),
        "kms_key_arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant_id}",
        "role_arn": TENANT_ROLE,
        "vector_bucket": naming.vector_bucket_name(PREFIX, tenant_id),
        "vector_index": naming.VECTOR_INDEX,
        "region": REGION,
    }
    return TenantStorageProfile(**(values | changes))


class FakeReader:
    def __init__(self, *profiles: TenantStorageProfile) -> None:
        self.rows = {p.tenant_id: p for p in profiles}
        self.asked: list[uuid.UUID] = []

    def profile(self, tenant_id: uuid.UUID) -> TenantStorageProfile | None:
        self.asked.append(tenant_id)
        return self.rows.get(tenant_id)


class FakeSts:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "secret-from-sts",
                "SessionToken": "token-from-sts",
                "Expiration": EXPIRATION,
            }
        }


def resolver(*profiles: TenantStorageProfile) -> tuple[Resolver, FakeReader, FakeSts]:
    reader, sts = FakeReader(*profiles), FakeSts()
    return Resolver(SETTINGS, reader, sts), reader, sts  # type: ignore[arg-type]


def request(
    tenant_id: uuid.UUID = TENANT_A,
    access: StorageAccess = StorageAccess.PIPELINE,
    source: str = "api.batch-42",
) -> ResolveRequest:
    return ResolveRequest(tenant_id=tenant_id, access=access, source_identity=source)


# --- resolve --------------------------------------------------------------------------------


def test_resolve_assumes_the_tenant_role_for_15_minutes_with_tags_and_source_identity() -> None:
    subject, reader, sts = resolver(profile(TENANT_A), profile(TENANT_B))
    context = subject.resolve(request(), caller=API)

    assert reader.asked == [TENANT_A]
    [call] = sts.calls
    assert call["RoleArn"] == TENANT_ROLE
    assert call["DurationSeconds"] == 900
    assert call["SourceIdentity"] == "api.batch-42"
    assert call["Tags"] == [
        {"Key": "tenant_id", "Value": str(TENANT_A)},
        {"Key": "bucket", "Value": "assay-dev-t-aaaaaaaa"},
        {"Key": "vector_bucket", "Value": "assay-dev-t-aaaaaaaa-vectors"},
    ]
    assert call["RoleSessionName"] == "t-aaaaaaaa-pipeline"

    assert context.tenant_id == TENANT_A
    assert context.bucket == "assay-dev-t-aaaaaaaa"
    assert context.face_collection == "assay-dev-t-aaaaaaaa"
    assert context.credentials.expiration == EXPIRATION
    assert context.credentials.secret_access_key.get_secret_value() == "secret-from-sts"


def test_session_policy_names_the_exact_bucket_key_index_and_collection() -> None:
    subject, _, sts = resolver(profile(TENANT_A), profile(TENANT_B))
    subject.resolve(request(), caller=API)
    policy = json.loads(sts.calls[0]["Policy"])
    resources = {
        r
        for s in policy["Statement"]
        for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])
    }
    # No bucket ARN: no session may list the bucket.
    assert resources == {
        "arn:aws:s3:::assay-dev-t-aaaaaaaa/*",
        f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{TENANT_A}",
        f"arn:aws:s3vectors:{REGION}:{ACCOUNT}:bucket/assay-dev-t-aaaaaaaa-vectors/index/documents",
        f"arn:aws:rekognition:{REGION}:{ACCOUNT}:collection/assay-dev-t-aaaaaaaa",
    }
    assert "bbbbbbbb" not in sts.calls[0]["Policy"]


def test_upload_access_gets_the_narrow_session_policy() -> None:
    subject, _, sts = resolver(profile(TENANT_A))
    subject.resolve(request(access=StorageAccess.UPLOAD), caller=API)
    policy = json.loads(sts.calls[0]["Policy"])
    assert [s["Action"] for s in policy["Statement"]] == [["s3:PutObject"], ["kms:GenerateDataKey"]]
    assert sts.calls[0]["RoleSessionName"] == "t-aaaaaaaa-upload"


def test_a_refused_assume_role_is_reported_with_its_own_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression (2026-11-23): STS refused the TSS (a trust policy fault) and every resolve
    answered a bare `internal_error`. The caller now gets a code that says what failed, and
    the log has STS's own message, which names the refused action."""
    message = (
        "User: arn:aws:sts::000000000000:assumed-role/tss/tss is not authorized to perform: "
        "sts:SetSourceIdentity on resource: arn:aws:iam::000000000000:role/assay-dev-tenant"
    )

    class RefusingSts:
        def assume_role(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": message}}, "AssumeRole")

    subject = Resolver(SETTINGS, FakeReader(profile(TENANT_A)), RefusingSts())  # type: ignore[arg-type]
    with pytest.raises(service.TenantRoleRefused) as raised:
        subject.resolve(request(), caller=API)
    assert "sts:SetSourceIdentity" in str(raised.value)

    with caplog.at_level(logging.INFO):
        response = tss_handler.handle(event(good_body()), SETTINGS, subject)
    assert response["statusCode"] == 500
    # The code only: STS's message, with its ARNs, stays in the log.
    assert json.loads(response["body"]) == {"error": "tenant_role_refused"}
    logged = " ".join(str(r.__dict__) for r in caplog.records)
    assert "sts:SetSourceIdentity" in logged


def test_unknown_tenant_gets_nothing() -> None:
    subject, _, sts = resolver(profile(TENANT_B))
    with pytest.raises(service.UnknownTenant):
        subject.resolve(request(TENANT_A), caller=API)
    assert sts.calls == []


def test_offboarded_storage_gets_nothing() -> None:
    subject, _, sts = resolver(profile(TENANT_A, status=StorageStatus.OFFBOARDED))
    with pytest.raises(service.StorageNotActive):
        subject.resolve(request(), caller=API)
    assert sts.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        # A changed row must never point one tenant at another tenant's resources or role.
        {"bucket": naming.bucket_name(PREFIX, TENANT_B)},
        {"vector_bucket": naming.vector_bucket_name(PREFIX, TENANT_B)},
        {"vector_index": "other"},
        {"role_arn": f"arn:aws:iam::{ACCOUNT}:role/some-other-role"},
        {"region": "us-east-2"},
        {"kms_key_arn": "arn:aws:kms:us-east-1:999999999999:key/elsewhere"},
        {"tenant_id": TENANT_B},
    ],
)
def test_a_row_that_does_not_match_the_tenant_is_refused(changes: dict[str, Any]) -> None:
    row = profile(TENANT_A).model_copy(update=changes)
    reader, sts = FakeReader(), FakeSts()
    reader.rows[TENANT_A] = row
    subject = Resolver(SETTINGS, reader, sts)  # type: ignore[arg-type]
    with pytest.raises(service.StorageProfileInvalid):
        subject.resolve(request(), caller=API)
    assert sts.calls == []


def test_source_identity_must_carry_the_callers_own_service() -> None:
    subject, _, sts = resolver(profile(TENANT_A))
    with pytest.raises(service.SourceIdentityMismatch):
        subject.resolve(request(source="worker.doc-1"), caller=API)
    assert sts.calls == []


@pytest.mark.parametrize(
    ("caller", "access"),
    [
        # The API signs upload links and reads originals; it never gets pipeline access.
        (API_NARROW, StorageAccess.PIPELINE),
        # The pipeline gets nothing but pipeline access.
        (
            CallerGrant(service="api", access=frozenset({StorageAccess.PIPELINE})),
            StorageAccess.READ,
        ),
    ],
)
def test_a_caller_gets_only_the_accesses_it_is_granted(
    caller: CallerGrant, access: StorageAccess
) -> None:
    subject, reader, sts = resolver(profile(TENANT_A))
    with pytest.raises(service.AccessNotAllowed):
        subject.resolve(request(access=access), caller=caller)
    assert sts.calls == [] and reader.asked == []


# --- Who is calling --------------------------------------------------------------------------


def test_caller_service_comes_from_the_iam_role_that_signed_the_request() -> None:
    assert service.caller_service(API_CALLER, SETTINGS.callers) == API_NARROW


@pytest.mark.parametrize(
    "user_arn",
    [
        None,
        "",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/unknown-role/session",
        f"arn:aws:iam::{ACCOUNT}:user/api-task-role-1a2b3c",
        f"arn:aws:iam::{ACCOUNT}:role/api-task-role-1a2b3c",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/api-task-role-1a2b3c",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/x/api-task-role-1a2b3c/y",
    ],
)
def test_unknown_callers_are_refused(user_arn: str | None) -> None:
    with pytest.raises(service.CallerNotAllowed):
        service.caller_service(user_arn, SETTINGS.callers)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("NAME_PREFIX", PREFIX)
    monkeypatch.setenv("TENANT_ROLE_ARN", TENANT_ROLE)
    monkeypatch.setenv(
        "TSS_CALLERS", json.dumps({"role-a": {"service": "api", "access": ["read", "upload"]}})
    )
    settings = ServiceSettings.from_env()
    assert settings.account_id == ACCOUNT
    assert settings.callers == {"role-a": API_NARROW}
    for bad in (
        ["role-a"],
        {"role-a": "api"},
        {"role-a": {"service": "api"}},
        {"role-a": {"service": "api", "access": []}},
        {"role-a": {"service": "api", "access": ["everything"]}},
        {"role-a": {"service": "api", "access": ["read"], "extra": True}},
    ):
        monkeypatch.setenv("TSS_CALLERS", json.dumps(bad))
        with pytest.raises(ValueError):
            ServiceSettings.from_env()


# --- The Lambda handler -----------------------------------------------------------------------


def event(
    body: Any, *, user_arn: str | None = API_CALLER, resource: str = "/resolve"
) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "resource": resource,
        "body": body if isinstance(body, str) else json.dumps(body),
        "requestContext": {"identity": {"userArn": user_arn}},
    }


def good_body(**changes: Any) -> dict[str, Any]:
    return {
        "tenant_id": str(TENANT_A),
        "access": "read",
        "source_identity": "api.request-7",
    } | changes


def test_handler_returns_a_storage_context() -> None:
    subject, _, _ = resolver(profile(TENANT_A))
    response = tss_handler.handle(event(good_body()), SETTINGS, subject)
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "no-store"
    body = json.loads(response["body"])
    assert body["bucket"] == "assay-dev-t-aaaaaaaa"
    assert body["credentials"]["secret_access_key"] == "secret-from-sts"
    assert body["credentials"]["expiration"] == EXPIRATION.isoformat()


@pytest.mark.parametrize(
    ("request_event", "status", "error"),
    [
        (event(good_body(), user_arn=None), 403, "caller_not_allowed"),
        (event(good_body(source_identity="worker.x")), 403, "source_identity_mismatch"),
        (event(good_body(access="pipeline")), 403, "access_not_allowed"),
        (event(good_body(tenant_id=str(TENANT_B))), 404, "no_storage_for_tenant"),
        (event(good_body(access="everything")), 400, "invalid_request"),
        (event(good_body(bucket="assay-dev-t-bbbbbbbb")), 400, "invalid_request"),
        (event("not json"), 400, "invalid_request"),
        (event(good_body(), resource="/provision"), 404, "not_found"),
    ],
)
def test_handler_refusals(request_event: dict[str, Any], status: int, error: str) -> None:
    subject, _, sts = resolver(profile(TENANT_A))
    response = tss_handler.handle(request_event, SETTINGS, subject)
    assert (response["statusCode"], json.loads(response["body"])) == (status, {"error": error})
    assert sts.calls == []


def test_handler_never_logs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    subject, _, _ = resolver(profile(TENANT_A))
    with caplog.at_level(logging.INFO):
        tss_handler.handle(event(good_body()), SETTINGS, subject)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "storage context issued" in logged
    assert "secret-from-sts" not in logged and "token-from-sts" not in logged


def test_unhandled_errors_do_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("password=hunter2 host=db.internal")

    monkeypatch.setattr(tss_handler, "_state", boom)
    response = tss_handler.handler(event(good_body()), None)
    assert response["statusCode"] == 500
    assert response["body"] == json.dumps({"error": "internal_error"})


# --- The client ------------------------------------------------------------------------------


def client() -> TssClient:
    session = boto3.Session(
        aws_access_key_id="AKIAFAKE", aws_secret_access_key="fake", region_name=REGION
    )
    return TssClient(
        "https://abc123.execute-api.us-east-1.amazonaws.com/v1", region=REGION, session=session
    )


def test_client_signs_requests_for_execute_api() -> None:
    signed = client().signed_request("/resolve", b"{}")
    assert signed.full_url == "https://abc123.execute-api.us-east-1.amazonaws.com/v1/resolve"
    assert signed.get_method() == "POST"
    authorization = signed.get_header("Authorization")
    assert authorization is not None
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKE/")
    assert f"/{REGION}/execute-api/aws4_request" in authorization


def test_client_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https"):
        TssClient("http://abc123.execute-api.us-east-1.amazonaws.com/v1", region=REGION)


def test_client_parses_the_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    subject, _, _ = resolver(profile(TENANT_A))
    wire = subject.resolve(request(access=StorageAccess.READ), caller=API).to_wire()
    sent: list[bytes] = []
    tss = client()

    def fake_send(req: Any) -> dict[str, Any]:
        sent.append(req.data)
        return wire

    monkeypatch.setattr(tss, "send", fake_send)
    context = tss.resolve(TENANT_A, StorageAccess.READ, "api.batch-42")
    assert json.loads(sent[0]) == {
        "tenant_id": str(TENANT_A),
        "access": "read",
        "source_identity": "api.batch-42",
    }
    assert context.bucket == "assay-dev-t-aaaaaaaa"
    assert context.credentials.session_token.get_secret_value() == "token-from-sts"


def test_client_does_not_provision_or_offboard() -> None:
    with pytest.raises(NotImplementedError):
        client().provision(TENANT_A, IsolationMode.DEDICATED)
    with pytest.raises(NotImplementedError):
        client().offboard(TENANT_A)
