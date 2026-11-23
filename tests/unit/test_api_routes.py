"""GET /me and the account check every route runs, against a real PostgreSQL 17.

The API signs in as `app_tenant` and `platform_reader` exactly as it does on Aurora (here
through trust authentication instead of IAM tokens), so row-level security, the platform
users view and the grants are the real ones. Tokens are signed locally (tests/unit/tokens.py).

Besides GET /me, the app gets one probe route per dependency in assay_api.security, so the
account check is tested the way every later tenant and admin route will use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from assay_api.app import create_app
from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_api.security import (
    ACCOUNT_CHECK_ATTRIBUTE,
    AnySignedIn,
    Caller,
    MasterAdmin,
    PlatformMember,
    TenantAdmin,
    TenantMember,
)
from assay_db.roles import APP_TENANT, PLATFORM_READER
from tests.unit.pgtest import (
    TestDb,
    _bootstrap_as_master,
    _conninfo,
    _create_database,
    _drop_database,
    _migrate,
    requires_db,
)
from tests.unit.tokens import make_issuer

pytestmark = requires_db

TENANT = make_issuer("tenant")
PLATFORM = make_issuer("platform")

# Routes that may be called without the account check.
UNCHECKED_PATHS = {"/api/health", "/api/openapi.json"}


@dataclass(frozen=True)
class Tenants:
    platform: uuid.UUID
    a: uuid.UUID
    b: uuid.UUID
    suspended: uuid.UUID
    provisioning: uuid.UUID


@dataclass(frozen=True)
class ApiDb:
    name: str
    url: str
    tenants: Tenants


# (sub, tenant attribute, role, status)
USERS = (
    ("m-admin", "platform", "master-admin", "active"),
    ("m-user", "platform", "master-user", "active"),
    ("a-admin", "a", "tenant-admin", "active"),
    ("a-user", "a", "tenant-user", "active"),
    ("a-invited", "a", "tenant-user", "invited"),
    ("a-disabled", "a", "tenant-user", "disabled"),
    ("b-admin", "b", "tenant-admin", "active"),
    ("s-user", "suspended", "tenant-user", "active"),
    ("p-admin", "provisioning", "tenant-admin", "invited"),
)


@pytest.fixture(scope="module")
def api_db(db: TestDb) -> Iterator[ApiDb]:
    """A database of its own, so these rows never touch the shared one's counts."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name)
        url = _conninfo(name)
        t = Tenants(*(uuid.uuid4() for _ in range(5)))
        with psycopg.connect(url) as conn:
            for tid, tenant_name, kind, status in (
                (t.platform, "Assay Insights, LLC", "platform", "active"),
                (t.a, "Tenant A", "business", "active"),
                (t.b, "Tenant B", "business", "active"),
                (t.suspended, "Tenant S", "business", "suspended"),
                (t.provisioning, "Tenant P", "business", "provisioning"),
            ):
                conn.execute(
                    "INSERT INTO assay.tenants (id, name, kind, status) VALUES (%s, %s, %s, %s)",
                    (tid, tenant_name, kind, status),
                )
            for sub, tenant, role, status in USERS:
                conn.execute(
                    "INSERT INTO assay.users (tenant_id, cognito_sub, email, name, role, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (getattr(t, tenant), sub, f"{sub}@example.test", f"Name {sub}", role, status),
                )
        yield ApiDb(name=name, url=url, tenants=t)
    finally:
        _drop_database(name)


def _engine(name: str, role: str) -> Engine:
    return create_engine(
        "postgresql+psycopg://", creator=lambda: psycopg.connect(_conninfo(name, role))
    )


def _probe_app(app: FastAPI) -> None:
    """One route per dependency. Each reports what its transaction can see."""
    probes = APIRouter(prefix="/api/probe")

    def report(caller: Caller) -> dict[str, Any]:
        setting = caller.conn.execute(
            text("SELECT current_user::text, current_setting('app.tenant_id', true)")
        ).one()
        return {"user": setting[0], "tenant_setting": setting[1], "role": caller.account.role}

    @probes.get("/any")
    def any_signed_in(caller: AnySignedIn) -> dict[str, Any]:
        return report(caller)

    @probes.get("/tenant-member")
    def tenant_member(caller: TenantMember) -> dict[str, Any]:
        visible = caller.conn.execute(text("SELECT count(*) FROM assay.users")).scalar_one()
        return {**report(caller), "visible_users": visible}

    @probes.get("/tenant-admin")
    def tenant_admin(caller: TenantAdmin) -> dict[str, Any]:
        return report(caller)

    @probes.get("/platform-member")
    def platform_member(caller: PlatformMember) -> dict[str, Any]:
        return report(caller)

    @probes.get("/master-admin")
    def master_admin(caller: MasterAdmin) -> dict[str, Any]:
        return report(caller)

    app.include_router(probes)


@pytest.fixture(scope="module")
def client(api_db: ApiDb) -> Iterator[TestClient]:
    databases = Databases(
        tenant_engine=_engine(api_db.name, APP_TENANT),
        platform_engine=_engine(api_db.name, PLATFORM_READER),
    )
    app = create_app(
        verifier=TokenVerifier([TENANT.settings, PLATFORM.settings]), databases=databases
    )
    _probe_app(app)
    with TestClient(app) as test_client:
        yield test_client


def tenant_token(api_db: ApiDb, sub: str, *, mfa: bool = True, **overrides: Any) -> str:
    tenant, role = next((t, r) for s, t, r, _ in USERS if s == sub)
    args: dict[str, Any] = {
        "sub": sub,
        "groups": [role],
        "tenant_id": getattr(api_db.tenants, tenant),
        "mfa": mfa,
    }
    args.update(overrides)
    return TENANT.mint(**args)


def platform_token(sub: str, **overrides: Any) -> str:
    role = next(r for s, _, r, _ in USERS if s == sub)
    args: dict[str, Any] = {"sub": sub, "groups": [role]}
    args.update(overrides)
    return PLATFORM.mint(**args)


def get(client: TestClient, path: str, token: str | None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.get(path, headers=headers)


# --- GET /me -----------------------------------------------------------------------------


def test_me_for_a_tenant_user(client: TestClient, api_db: ApiDb) -> None:
    response = get(client, "/api/me", tenant_token(api_db, "a-user", mfa=False))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "a-user@example.test"
    assert body["user"]["name"] == "Name a-user"
    assert body["role"] == "tenant-user"
    assert body["tenant"] == {"id": str(api_db.tenants.a), "name": "Tenant A"}
    assert body["pool"] == "tenant"
    assert body["mfa_required"] is False


@pytest.mark.parametrize(("sub", "role"), [("m-admin", "master-admin"), ("m-user", "master-user")])
def test_me_for_platform_users(client: TestClient, api_db: ApiDb, sub: str, role: str) -> None:
    response = get(client, "/api/me", platform_token(sub))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == role
    assert body["tenant"] == {"id": str(api_db.tenants.platform), "name": "Assay Insights, LLC"}
    assert body["pool"] == "platform"
    assert body["mfa_required"] is False


def test_tenant_admin_without_mfa_gets_me_but_nothing_else(
    client: TestClient, api_db: ApiDb
) -> None:
    token = tenant_token(api_db, "a-admin", mfa=False)
    me = get(client, "/api/me", token)
    assert me.status_code == 200
    assert me.json()["mfa_required"] is True
    for path in ("/api/probe/tenant-member", "/api/probe/tenant-admin"):
        refused = get(client, path, token)
        assert (refused.status_code, refused.json()["detail"]) == (403, "mfa_required"), path


def test_tenant_admin_with_mfa(client: TestClient, api_db: ApiDb) -> None:
    token = tenant_token(api_db, "a-admin", mfa=True)
    assert get(client, "/api/me", token).json()["mfa_required"] is False
    assert get(client, "/api/probe/tenant-admin", token).status_code == 200


def test_an_invited_user_who_signed_in_is_accepted(client: TestClient, api_db: ApiDb) -> None:
    assert get(client, "/api/me", tenant_token(api_db, "a-invited")).status_code == 200


# --- The account check, on every kind of route ----------------------------------------------

TENANT_PATHS = ("/api/me", "/api/probe/any", "/api/probe/tenant-member")


@pytest.mark.parametrize("path", TENANT_PATHS)
@pytest.mark.parametrize(
    ("sub", "tenant", "expected"),
    [
        ("a-disabled", "a", "user_disabled"),
        ("s-user", "suspended", "tenant_suspended"),
        ("nobody", "a", "account_not_found"),
        # Tenant A's token with tenant B's user: row-level security hides B's row.
        ("b-admin", "a", "account_not_found"),
    ],
)
def test_account_check_refuses(
    client: TestClient, api_db: ApiDb, path: str, sub: str, tenant: str, expected: str
) -> None:
    token = TENANT.mint(
        sub=sub,
        groups=["tenant-user"] if sub != "b-admin" else ["tenant-admin"],
        tenant_id=getattr(api_db.tenants, tenant),
        mfa=True,
    )
    response = get(client, path, token)
    assert (response.status_code, response.json()["detail"]) == (403, expected)


def test_users_of_a_tenant_still_provisioning_are_refused(
    client: TestClient, api_db: ApiDb
) -> None:
    response = get(client, "/api/me", tenant_token(api_db, "p-admin"))
    assert (response.status_code, response.json()["detail"]) == (403, "tenant_not_active")


@pytest.mark.parametrize("path", TENANT_PATHS)
def test_group_must_match_the_users_role(client: TestClient, api_db: ApiDb, path: str) -> None:
    token = tenant_token(api_db, "a-user", groups=["tenant-admin"])
    response = get(client, path, token)
    assert (response.status_code, response.json()["detail"]) == (403, "role_mismatch")


@pytest.mark.parametrize("path", ["/api/me", "/api/probe/platform-member"])
def test_platform_account_check(client: TestClient, path: str) -> None:
    response = get(client, path, platform_token("m-user", groups=["master-admin"]))
    assert (response.status_code, response.json()["detail"]) == (403, "role_mismatch")
    unknown = get(client, path, PLATFORM.mint(sub="a-admin", groups=["master-admin"]))
    # A tenant user's sub is not in the platform tenant, so the platform view has no row.
    assert (unknown.status_code, unknown.json()["detail"]) == (403, "account_not_found")


@pytest.mark.parametrize(
    ("change", "undo", "expected"),
    [
        (
            "UPDATE assay.users SET status = 'disabled' WHERE cognito_sub = 'a-user'",
            "UPDATE assay.users SET status = 'active' WHERE cognito_sub = 'a-user'",
            "user_disabled",
        ),
        (
            "UPDATE assay.tenants SET status = 'suspended' WHERE name = 'Tenant A'",
            "UPDATE assay.tenants SET status = 'active' WHERE name = 'Tenant A'",
            "tenant_suspended",
        ),
        (
            "UPDATE assay.users SET role = 'tenant-admin' WHERE cognito_sub = 'a-user'",
            "UPDATE assay.users SET role = 'tenant-user' WHERE cognito_sub = 'a-user'",
            "role_mismatch",
        ),
    ],
)
def test_a_change_takes_effect_on_the_next_request_with_the_same_token(
    client: TestClient, api_db: ApiDb, change: str, undo: str, expected: str
) -> None:
    token = tenant_token(api_db, "a-user")
    assert get(client, "/api/probe/tenant-member", token).status_code == 200
    with psycopg.connect(api_db.url, autocommit=True) as admin:
        admin.execute(change)
        try:
            for path in TENANT_PATHS:
                response = get(client, path, token)
                assert (response.status_code, response.json()["detail"]) == (403, expected)
        finally:
            admin.execute(undo)
    assert get(client, "/api/probe/tenant-member", token).status_code == 200


def test_platform_user_disabled_on_the_next_request(client: TestClient, api_db: ApiDb) -> None:
    token = platform_token("m-user")
    assert get(client, "/api/probe/platform-member", token).status_code == 200
    with psycopg.connect(api_db.url, autocommit=True) as admin:
        admin.execute("UPDATE assay.users SET status = 'disabled' WHERE cognito_sub = 'm-user'")
        try:
            response = get(client, "/api/probe/platform-member", token)
            assert (response.status_code, response.json()["detail"]) == (403, "user_disabled")
        finally:
            admin.execute("UPDATE assay.users SET status = 'active' WHERE cognito_sub = 'm-user'")


# --- Pools, roles and the transaction a route gets -------------------------------------------


@pytest.mark.parametrize(
    ("path", "sub", "pool", "expected"),
    [
        ("/api/probe/platform-member", "a-admin", "tenant", "wrong_pool"),
        ("/api/probe/master-admin", "a-admin", "tenant", "wrong_pool"),
        ("/api/probe/tenant-member", "m-admin", "platform", "wrong_pool"),
        ("/api/probe/tenant-admin", "m-admin", "platform", "wrong_pool"),
        ("/api/probe/tenant-admin", "a-user", "tenant", "forbidden"),
        ("/api/probe/master-admin", "m-user", "platform", "forbidden"),
    ],
)
def test_pool_and_role_gates(
    client: TestClient, api_db: ApiDb, path: str, sub: str, pool: str, expected: str
) -> None:
    token = tenant_token(api_db, sub) if pool == "tenant" else platform_token(sub)
    response = get(client, path, token)
    assert (response.status_code, response.json()["detail"]) == (403, expected)


def test_tenant_route_runs_as_app_tenant_scoped_to_the_tokens_tenant(
    client: TestClient, api_db: ApiDb
) -> None:
    body = get(client, "/api/probe/tenant-member", tenant_token(api_db, "a-user")).json()
    assert body["user"] == APP_TENANT
    assert body["tenant_setting"] == str(api_db.tenants.a)
    # Tenant A has four users; nobody else's are visible.
    assert body["visible_users"] == 4


def test_admin_route_runs_as_platform_reader(client: TestClient) -> None:
    body = get(client, "/api/probe/master-admin", platform_token("m-admin")).json()
    assert body["user"] == PLATFORM_READER
    assert body["tenant_setting"] in (None, "")


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer ", "Bearer not-a-token"])
def test_missing_or_bad_token_is_401(client: TestClient, header: str | None) -> None:
    headers = {"Authorization": header} if header is not None else {}
    response = client.get("/api/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/api/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


# --- Every route runs the account check -------------------------------------------------------


def _has_account_check(route: APIRoute) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if getattr(dependant.call, ACCOUNT_CHECK_ATTRIBUTE, False):
            return True
        pending.extend(dependant.dependencies)
    return False


def api_routes(app: FastAPI) -> dict[str, APIRoute]:
    """Every endpoint by its full path, including those of included routers."""
    routes: dict[str, APIRoute] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path:
            routes[context.path] = route
    return routes


def unchecked_routes(app: FastAPI) -> list[str]:
    return sorted(
        path
        for path, route in api_routes(app).items()
        if path not in UNCHECKED_PATHS and not _has_account_check(route)
    )


def test_every_api_route_runs_the_account_check() -> None:
    app = create_app(
        verifier=TokenVerifier([TENANT.settings, PLATFORM.settings]),
        databases=Databases(
            tenant_engine=create_engine("sqlite://"), platform_engine=create_engine("sqlite://")
        ),
    )
    assert set(api_routes(app)) >= {"/api/me", "/api/health"}
    assert unchecked_routes(app) == []


def test_the_route_check_notices_a_route_without_it() -> None:
    app = FastAPI()

    @app.get("/api/unguarded")
    def unguarded() -> dict[str, str]:
        return {}

    assert unchecked_routes(app) == ["/api/unguarded"]
