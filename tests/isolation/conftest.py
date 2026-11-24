"""Fixtures for the isolation tests, which run inside the VPC against the real account.

The two tenants come from the runner's event (`ISOLATION_TENANT_A`, `ISOLATION_TENANT_B`).
Every storage context is issued by the real TSS. "The runner" is this Lambda's own role: a
probe whose IAM policy allows what the tenant resources' own policies must refuse.

The tests write two things: two small fixture objects in each tenant's own bucket, once,
with that tenant's pipeline credentials (`ensure_fixture_objects`), both under the reserved
document ID (`assay_tss.keys.RESERVED_DOCUMENT_ID`) and never in `inbox/`. Every other write
they attempt must be refused, and every database transaction is rolled back.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import Connection, Engine, text

from assay_db.connect import DbTarget, iam_engine
from assay_tss import keys
from assay_tss.client import TssClient
from assay_tss.models import StorageAccess, StorageContext, source_identity

DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT
# Fail fast: an unreachable endpoint must not look like a refusal.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})
DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "403", "Forbidden"})
# A tag no policy looks at. If a tagging probe ever got through, it would change nothing
# that isolation depends on.
PROBE_TAG = "isolation-probe"
# The two objects the tests write, once per tenant, under the reserved document ID.
FIXTURE_BODY = b'{"isolation-test-fixture": true}\n'
QUARANTINE_FIXTURE_BODY = b"isolation-test-fixture: not a PDF, never scanned or processed\n"
FIXTURES = (
    (keys.isolation_fixture_text_key(), FIXTURE_BODY, "application/json"),
    (keys.isolation_fixture_quarantine_key(), QUARANTINE_FIXTURE_BODY, "application/pdf"),
)
# A pipeline session may not list the bucket (build step 6), so S3 answers 403, not 404,
# for a key that does not exist.
MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied"})
# S3's reason for a refusal by the session policy (same-account requests). A missing key
# never carries it: that refusal comes from S3 hiding the key, not from a policy.
SESSION_POLICY_REFUSAL = "because no session policy allows the s3:GetObject action"


@dataclass(frozen=True)
class Env:
    region: str
    name_prefix: str
    tss_endpoint: str
    service: str
    tenant_role_arn: str
    invite_function: str
    # The API's Cognito admin function (build step 5).
    api_cognito_function: str
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID

    @property
    def account_id(self) -> str:
        return self.tenant_role_arn.split(":")[4]

    @property
    def tenant_role_name(self) -> str:
        return self.tenant_role_arn.rsplit("/", 1)[1]


@dataclass(frozen=True)
class Pair:
    """One direction of the test: `own` credentials against `other`'s resources."""

    own: StorageContext
    other: StorageContext


def assert_denied(call: Callable[[], object]) -> str:
    """The call must fail with an access-denied error, not succeed and not fail otherwise.

    Returns S3's message, for the tests that check why it refused.
    """
    with pytest.raises(ClientError) as raised:
        call()
    error = raised.value.response.get("Error", {})
    code = error.get("Code", "")
    assert code in DENIED_CODES, f"expected access denied, got {code!r}"
    return str(error.get("Message", ""))


def client(session: boto3.Session, service: str, region: str) -> object:
    if service == "sts":
        # The regional endpoint: the only one the VPC's STS endpoint serves.
        return session.client(
            "sts",
            region_name=region,
            endpoint_url=f"https://sts.{region}.amazonaws.com",
            config=CLIENT_CONFIG,
        )
    return session.client(service, region_name=region, config=CLIENT_CONFIG)  # type: ignore[call-overload]


@pytest.fixture(scope="session")
def env() -> Env:
    tenant_a = uuid.UUID(os.environ["ISOLATION_TENANT_A"])
    tenant_b = uuid.UUID(os.environ["ISOLATION_TENANT_B"])
    assert tenant_a != tenant_b, "the two test tenants must differ"
    return Env(
        region=os.environ["AWS_REGION"],
        name_prefix=os.environ["NAME_PREFIX"],
        tss_endpoint=os.environ["TSS_ENDPOINT"],
        service=os.environ["TSS_SERVICE"],
        tenant_role_arn=os.environ["TENANT_ROLE_ARN"],
        invite_function=os.environ["INVITE_FUNCTION"],
        api_cognito_function=os.environ["API_COGNITO_FUNCTION"],
        tenant_a=tenant_a,
        tenant_b=tenant_b,
    )


@pytest.fixture(scope="session")
def tss(env: Env) -> TssClient:
    return TssClient(env.tss_endpoint, region=env.region)


@pytest.fixture(scope="session")
def resolve(env: Env, tss: TssClient) -> Callable[[uuid.UUID, StorageAccess], StorageContext]:
    def _resolve(tenant_id: uuid.UUID, access: StorageAccess) -> StorageContext:
        return tss.resolve(tenant_id, access, source_identity(env.service, "isolation-tests"))

    return _resolve


@pytest.fixture(scope="session")
def context_a(
    env: Env, resolve: Callable[[uuid.UUID, StorageAccess], StorageContext]
) -> StorageContext:
    return resolve(env.tenant_a, StorageAccess.PIPELINE)


@pytest.fixture(scope="session")
def context_b(
    env: Env, resolve: Callable[[uuid.UUID, StorageAccess], StorageContext]
) -> StorageContext:
    return resolve(env.tenant_b, StorageAccess.PIPELINE)


@pytest.fixture(params=["a-against-b", "b-against-a"])
def pair(
    request: pytest.FixtureRequest, context_a: StorageContext, context_b: StorageContext
) -> Pair:
    """Every cross-tenant test runs in both directions."""
    if request.param == "a-against-b":
        return Pair(own=context_a, other=context_b)
    return Pair(own=context_b, other=context_a)


@pytest.fixture(params=["tenant-a", "tenant-b"])
def tenant(request: pytest.FixtureRequest) -> StorageContext:
    """Each tenant in turn, with its full (pipeline) access.

    Only the named tenant's context is resolved, so a failure for one tenant does not hide
    the other tenant's results.
    """
    name = "context_a" if request.param == "tenant-a" else "context_b"
    context: StorageContext = request.getfixturevalue(name)
    return context


def ensure_fixture_objects(context: StorageContext) -> None:
    """Two small objects in the tenant's bucket, written once with the tenant's credentials.

    A refused read proves something only for an object that exists: S3 refuses a missing key
    too, to any caller that may not list the bucket. These are the only things the isolation
    tests write: `derived/text/{reserved}.json` and `quarantine/{reserved}.pdf`. Object Lock
    keeps them, so later runs find them and write nothing. The pipeline never processes the
    reserved ID, and nothing scans or processes `quarantine/`.
    """
    api: Any = context.session().client("s3", region_name=context.region, config=CLIENT_CONFIG)
    for key, body, content_type in FIXTURES:
        try:
            api.head_object(Bucket=context.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in MISSING_CODES:
                raise
            api.put_object(Bucket=context.bucket, Key=key, Body=body, ContentType=content_type)


@pytest.fixture(scope="session")
def object_key() -> Callable[[StorageContext], str]:
    """The key of the tenant's text fixture, given the tenant's pipeline context."""
    known: set[uuid.UUID] = set()

    def _key(context: StorageContext) -> str:
        if context.tenant_id not in known:
            ensure_fixture_objects(context)
            known.add(context.tenant_id)
        return keys.isolation_fixture_text_key()

    return _key


@pytest.fixture(scope="session")
def quarantine_key(
    object_key: Callable[[StorageContext], str],
) -> Callable[[StorageContext], str]:
    """The key of the tenant's quarantine fixture (written with the text fixture)."""

    def _key(context: StorageContext) -> str:
        object_key(context)
        return keys.isolation_fixture_quarantine_key()

    return _key


@pytest.fixture(scope="session")
def runner(env: Env) -> boto3.Session:
    """This Lambda's own role: allowed by IAM, to be refused by the resources."""
    return boto3.Session(region_name=env.region)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    db = iam_engine(DbTarget.from_env(), DB_USER, application_name="assay-isolation-tests")
    try:
        yield db
    finally:
        db.dispose()


@pytest.fixture
def as_tenant(engine: Engine) -> Iterator[Callable[[uuid.UUID | None], Connection]]:
    """A transaction as `app_tenant` scoped to a tenant (or to none). Always rolled back."""
    connections: list[Connection] = []

    def _connect(tenant_id: uuid.UUID | None) -> Connection:
        conn = engine.connect()
        connections.append(conn)
        if tenant_id is not None:
            conn.execute(
                text("SELECT pg_catalog.set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
        return conn

    try:
        yield _connect
    finally:
        for conn in connections:
            conn.rollback()
            conn.close()
