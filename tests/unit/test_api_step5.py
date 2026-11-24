"""Build step 5 routes against a real PostgreSQL 17: users, tenants, MFA resets, auth events.

The API runs as `app_tenant`, `platform_reader` and `platform_writer` exactly as on Aurora,
so row-level security and the grants are the real ones. Cognito and Step Functions are fakes
(tests/unit/fakes.py) that record what the API asked for.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from assay_api.app import create_app
from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_db.roles import APP_TENANT, PLATFORM_READER, PLATFORM_WRITER
from assay_tss.provision.task import ProvisionRequest
from tests.unit.fakes import FakeCognito, FakeProvisioning
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
VIEWER = {"CloudFront-Viewer-Address": "198.51.100.7:44321", "User-Agent": "pytest-browser"}


@dataclass
class World:
    name: str
    url: str
    platform: uuid.UUID
    a: uuid.UUID
    b: uuid.UUID
    failed: uuid.UUID
    users: dict[str, uuid.UUID]
    client: TestClient
    cognito: FakeCognito
    provisioning: FakeProvisioning


HAIKU, SONNET, OPUS = (uuid.uuid4() for _ in range(3))
CATALOG = (
    (HAIKU, "Model Haiku", "us.example.haiku-v1:0"),
    (SONNET, "Model Sonnet", "us.example.sonnet-v1"),
    (OPUS, "Model Opus", "us.example.opus-v1"),
)

# sub -> (tenant attribute, role, status)
USERS = {
    "m-admin": ("platform", "master-admin", "active"),
    "m-user": ("platform", "master-user", "active"),
    "a-admin": ("a", "tenant-admin", "active"),
    "a-admin2": ("a", "tenant-admin", "active"),
    "a-user": ("a", "tenant-user", "active"),
    "a-invited": ("a", "tenant-user", "invited"),
    "b-admin": ("b", "tenant-admin", "active"),
    "b-user": ("b", "tenant-user", "active"),
}


def _engine(name: str, role: str) -> Engine:
    return create_engine(
        "postgresql+psycopg://", creator=lambda: psycopg.connect(_conninfo(name, role))
    )


@pytest.fixture(scope="module")
def world(db: TestDb) -> Iterator[World]:
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name)
        url = _conninfo(name)
        tenants = {k: uuid.uuid4() for k in ("platform", "a", "b", "failed")}
        users: dict[str, uuid.UUID] = {}
        with psycopg.connect(url) as conn:
            for key, tenant_name, kind, status in (
                ("platform", "Assay Insights, LLC", "platform", "active"),
                ("a", "Tenant A", "business", "active"),
                ("b", "Tenant B", "business", "active"),
                ("failed", "Tenant F", "business", "failed"),
            ):
                conn.execute(
                    "INSERT INTO assay.tenants (id, name, kind, status) VALUES (%s, %s, %s, %s)",
                    (tenants[key], tenant_name, kind, status),
                )
            conn.execute(
                "INSERT INTO assay.audit_log (tenant_id, category, action, outcome, detail) "
                "VALUES (%s, 'tenant', 'created', 'success', %s)",
                (
                    tenants["failed"],
                    '{"after": {"first_admin": {"name": "Fay", "email": "fay@example.test"}}}',
                ),
            )
            for sub, (tenant, role, status) in USERS.items():
                users[sub] = uuid.uuid4()
                conn.execute(
                    "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, "
                    "status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        users[sub],
                        tenants[tenant],
                        sub,
                        f"{sub}@example.test",
                        f"Name {sub}",
                        role,
                        status,
                    ),
                )
            # The catalog, and tenants A and B with the defaults (Haiku ingest, Sonnet query).
            for model_id, model_name, profile in CATALOG:
                conn.execute(
                    "INSERT INTO assay.ai_models (id, name, profile_id, jobs, price_in, "
                    "price_out, status) VALUES (%s, %s, %s, ARRAY['ingest', 'query'], 1, 5, "
                    "'active')",
                    (model_id, model_name, profile),
                )
            for key in ("a", "b"):
                for model_id, job in ((HAIKU, "ingest"), (SONNET, "query")):
                    conn.execute(
                        "INSERT INTO assay.tenant_models (tenant_id, model_id, job) "
                        "VALUES (%s, %s, %s)",
                        (tenants[key], model_id, job),
                    )
                conn.execute(
                    "UPDATE assay.tenants SET extraction_model = %s, answer_model = %s "
                    "WHERE id = %s",
                    (HAIKU, SONNET, tenants[key]),
                )
        cognito, provisioning = FakeCognito(), FakeProvisioning()
        databases = Databases(
            tenant_engine=_engine(name, APP_TENANT),
            platform_engine=_engine(name, PLATFORM_READER),
            writer_engine=_engine(name, PLATFORM_WRITER),
        )
        app = create_app(
            verifier=TokenVerifier([TENANT.settings, PLATFORM.settings]),
            databases=databases,
            cognito=cognito,
            provisioning=provisioning,
        )
        with TestClient(app) as client:
            yield World(
                name=name,
                url=url,
                platform=tenants["platform"],
                a=tenants["a"],
                b=tenants["b"],
                failed=tenants["failed"],
                users=users,
                client=client,
                cognito=cognito,
                provisioning=provisioning,
            )
    finally:
        _drop_database(name)


def token(w: World, sub: str, *, mfa: bool = True, auth_time: int | None = None) -> str:
    tenant, role, _ = USERS.get(sub, ("a", "tenant-user", "active"))
    claims: dict[str, Any] = {"sub": sub, "groups": [role]}
    if tenant == "platform":
        minted = PLATFORM.mint(**claims)
    else:
        minted = TENANT.mint(**claims, tenant_id=getattr(w, tenant), mfa=mfa)
    if auth_time is None:
        return minted
    import jwt

    payload = jwt.decode(minted, options={"verify_signature": False})
    payload["auth_time"] = auth_time
    issuer = PLATFORM if tenant == "platform" else TENANT
    return jwt.encode(payload, issuer.key, algorithm="RS256", headers={"kid": issuer.kid})


def req(w: World, method: str, path: str, sub: str, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {token(w, sub)}", **VIEWER}
    return w.client.request(method, path, headers=headers, **kwargs)


def row(w: World, query: str, *args: object) -> tuple[Any, ...] | None:
    with psycopg.connect(w.url) as conn:
        return conn.execute(query, args).fetchone()


def audits(w: World, action: str, target: uuid.UUID | str) -> list[tuple[Any, ...]]:
    with psycopg.connect(w.url) as conn:
        return conn.execute(
            "SELECT tenant_id, actor_email, actor_role, detail, host(ip_address), user_agent "
            "FROM assay.audit_log WHERE action = %s AND target_id = %s ORDER BY at",
            (action, str(target)),
        ).fetchall()


# --- Users -------------------------------------------------------------------------------------


def test_list_users_shows_only_the_callers_tenant(world: World) -> None:
    response = req(world, "GET", "/api/users", "a-admin")
    assert response.status_code == 200, response.text
    emails = {u["email"] for u in response.json()["users"]}
    assert emails == {f"{s}@example.test" for s in USERS if s.startswith("a-")}


@pytest.mark.parametrize(
    ("sub", "mfa", "code"),
    [
        ("a-user", True, "forbidden"),
        ("a-admin", False, "mfa_required"),
        ("m-admin", True, "wrong_pool"),
    ],
)
def test_only_a_tenant_admin_with_mfa_manages_users(
    world: World, sub: str, mfa: bool, code: str
) -> None:
    headers = {"Authorization": f"Bearer {token(world, sub, mfa=mfa)}"}
    response = world.client.get("/api/users", headers=headers)
    assert (response.status_code, response.json()["detail"]) == (403, code)


def test_invite_takes_the_tenant_from_the_token(world: World) -> None:
    response = req(
        world,
        "POST",
        "/api/users",
        "a-admin",
        json={"name": "New  Person", "email": "new@example.test", "role": "tenant-user"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["role"], body["name"]) == ("invited", "tenant-user", "New Person")
    call = world.cognito.last("invite")
    assert call["tenant_id"] == str(world.a)
    assert call["pool"] == "tenant"
    [(tenant_id, actor, role, detail, ip, agent)] = audits(world, "invited", body["id"])
    assert (tenant_id, actor, role) == (world.a, "a-admin@example.test", "tenant-admin")
    assert detail["after"]["email"] == "new@example.test"
    assert (ip, agent) == ("198.51.100.7", "pytest-browser")


def test_invite_refuses_a_tenant_id_in_the_body_and_a_duplicate(world: World) -> None:
    sneaky = req(
        world,
        "POST",
        "/api/users",
        "a-admin",
        json={
            "name": "X",
            "email": "x@example.test",
            "role": "tenant-user",
            "tenant_id": str(world.b),
        },
    )
    assert sneaky.status_code == 422
    duplicate = req(
        world,
        "POST",
        "/api/users",
        "a-admin",
        json={"name": "X", "email": "A-USER@example.test", "role": "tenant-user"},
    )
    assert (duplicate.status_code, duplicate.json()["detail"]) == (409, "user_exists")


def test_an_address_of_another_tenant_is_refused(world: World) -> None:
    world.cognito.fail["invite"] = "email_in_use"
    response = req(
        world,
        "POST",
        "/api/users",
        "a-admin",
        json={"name": "Y", "email": "y@example.test", "role": "tenant-user"},
    )
    assert (response.status_code, response.json()["detail"]) == (409, "email_in_use")
    assert row(world, "SELECT 1 FROM assay.users WHERE email = 'y@example.test'") is None


def test_another_tenants_user_is_not_found(world: World) -> None:
    target = world.users["b-user"]
    for method, path, body in (
        ("PATCH", f"/api/users/{target}", {"status": "disabled"}),
        ("POST", f"/api/users/{target}/reset-mfa", None),
        ("POST", f"/api/users/{target}/reset-password", None),
    ):
        response = req(world, method, path, "a-admin", json=body)
        assert (response.status_code, response.json()["detail"]) == (404, "user_not_found"), path


def test_an_admin_cannot_change_their_own_role_or_disable_themselves(world: World) -> None:
    me = world.users["a-admin"]
    for body in ({"role": "tenant-user"}, {"status": "disabled"}):
        response = req(world, "PATCH", f"/api/users/{me}", "a-admin", json=body)
        assert (response.status_code, response.json()["detail"]) == (409, "cannot_change_self")


def test_a_role_change_is_audited_and_made_in_cognito(world: World) -> None:
    second = world.users["a-admin2"]
    response = req(world, "PATCH", f"/api/users/{second}", "a-admin", json={"role": "tenant-user"})
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "tenant-user"
    # Their token still says tenant-admin: refused until they sign in again.
    refused = req(world, "GET", "/api/me", "a-admin2")
    assert (refused.status_code, refused.json()["detail"]) == (403, "role_mismatch")
    [(_, _, _, detail, _, _)] = audits(world, "role_changed", second)
    assert detail == {"before": {"role": "tenant-admin"}, "after": {"role": "tenant-user"}}
    assert world.cognito.last("set_role") == {
        "pool": "tenant",
        "tenant_id": str(world.a),
        "email": "a-admin2@example.test",
        "sub": "a-admin2",
        "role": "tenant-user",
    }
    with psycopg.connect(world.url) as conn:
        conn.execute("UPDATE assay.users SET role = 'tenant-admin' WHERE id = %s", (second,))


def test_the_last_admin_check_counts_other_admins_not_disabled(world: World) -> None:
    """Through the API the caller is always another active admin (self-changes are refused
    first), so the check matters when two admins demote or disable each other at once: each
    transaction locks the tenant's admins and sees the other's committed change."""
    from sqlalchemy import text

    from assay_api.routes.users import _other_active_admins

    engine = _engine(world.name, APP_TENANT)
    try:
        for tenant, user, expected in (("b", "b-admin", 0), ("a", "a-admin", 1)):
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(getattr(world, tenant))},
                )
                assert _other_active_admins(conn, world.users[user]) == expected, tenant
        with psycopg.connect(world.url) as conn:
            conn.execute(
                "UPDATE assay.users SET status = 'disabled' WHERE cognito_sub = 'a-admin2'"
            )
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(world.a)})
            assert _other_active_admins(conn, world.users["a-admin"]) == 0
    finally:
        engine.dispose()
        with psycopg.connect(world.url) as conn:
            conn.execute("UPDATE assay.users SET status = 'active' WHERE cognito_sub = 'a-admin2'")


def test_disable_ends_sessions_and_enable_restores(world: World) -> None:
    target = world.users["a-user"]
    old_token = token(world, "a-user", auth_time=int(time.time()) - 60)
    assert (
        world.client.get("/api/me", headers={"Authorization": f"Bearer {old_token}"}).status_code
        == 200
    )
    response = req(world, "PATCH", f"/api/users/{target}", "a-admin", json={"status": "disabled"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"
    assert world.cognito.last("disable")["tenant_id"] == str(world.a)
    refused = world.client.get("/api/me", headers={"Authorization": f"Bearer {old_token}"})
    assert (refused.status_code, refused.json()["detail"]) == (403, "user_disabled")
    enabled = req(world, "PATCH", f"/api/users/{target}", "a-admin", json={"status": "active"})
    assert enabled.json()["status"] == "invited"  # never signed in (no last_sign_in_at)
    # The token from before the disable stays refused: its sessions were revoked.
    after = world.client.get("/api/me", headers={"Authorization": f"Bearer {old_token}"})
    assert (after.status_code, after.json()["detail"]) == (403, "session_revoked")
    assert [a[3] for a in audits(world, "disabled", target)] == [
        {"before": {"status": "active"}, "after": {"status": "disabled"}}
    ]
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "UPDATE assay.users SET status = 'active', sessions_revoked_at = NULL WHERE id = %s",
            (target,),
        )


def test_a_cognito_failure_writes_nothing(world: World) -> None:
    target = world.users["a-user"]
    before = len(audits(world, "disabled", target))
    world.cognito.fail["disable"] = "cognito_failed"
    response = req(world, "PATCH", f"/api/users/{target}", "a-admin", json={"status": "disabled"})
    assert (response.status_code, response.json()["detail"]) == (502, "cognito_failed")
    assert row(
        world, "SELECT status, sessions_revoked_at FROM assay.users WHERE id = %s", target
    ) == ("active", None)
    assert len(audits(world, "disabled", target)) == before


def test_reset_mfa_only_for_tenant_users_and_it_ends_their_sessions(world: World) -> None:
    admin_target = world.users["a-admin2"]
    refused = req(world, "POST", f"/api/users/{admin_target}/reset-mfa", "a-admin")
    assert (refused.status_code, refused.json()["detail"]) == (403, "forbidden")

    target = world.users["a-user"]
    before = token(world, "a-user", auth_time=int(time.time()) - 60)
    response = req(world, "POST", f"/api/users/{target}/reset-mfa", "a-admin")
    assert response.status_code == 204
    assert world.cognito.last("reset_mfa") == {
        "pool": "tenant",
        "tenant_id": str(world.a),
        "email": "a-user@example.test",
        "sub": "a-user",
    }
    old = world.client.get("/api/me", headers={"Authorization": f"Bearer {before}"})
    assert (old.status_code, old.json()["detail"]) == (403, "session_revoked")
    fresh = token(world, "a-user", auth_time=int(time.time()) + 2)
    assert (
        world.client.get("/api/me", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200
    )
    assert len(audits(world, "mfa_reset", target)) == 1
    with psycopg.connect(world.url) as conn:
        conn.execute("UPDATE assay.users SET sessions_revoked_at = NULL WHERE id = %s", (target,))


def test_resend_and_reset_password_follow_the_users_state(world: World) -> None:
    invited, active = world.users["a-invited"], world.users["a-user"]
    assert req(world, "POST", f"/api/users/{invited}/resend-invite", "a-admin").status_code == 204
    wrong = req(world, "POST", f"/api/users/{active}/resend-invite", "a-admin")
    assert (wrong.status_code, wrong.json()["detail"]) == (409, "wrong_state")
    assert req(world, "POST", f"/api/users/{active}/reset-password", "a-admin").status_code == 204
    wrong = req(world, "POST", f"/api/users/{invited}/reset-password", "a-admin")
    assert (wrong.status_code, wrong.json()["detail"]) == (409, "wrong_state")
    assert len(audits(world, "invite_resent", invited)) == 1
    assert len(audits(world, "password_reset_sent", active)) == 1


# --- Tenants ---------------------------------------------------------------------------------


def test_tenants_list_for_both_platform_roles_and_nobody_else(world: World) -> None:
    for sub in ("m-admin", "m-user"):
        response = req(world, "GET", "/api/admin/tenants", sub)
        assert response.status_code == 200
        by_name = {t["name"]: t for t in response.json()["tenants"]}
        users_of_a = row(world, "SELECT count(*) FROM assay.users WHERE tenant_id = %s", world.a)
        assert users_of_a is not None
        assert by_name["Tenant A"]["users"] == users_of_a[0] >= 4
        assert by_name["Tenant A"]["documents"] == 0
    tenant = req(world, "GET", "/api/admin/tenants", "a-admin")
    assert (tenant.status_code, tenant.json()["detail"]) == (403, "wrong_pool")


def test_master_admin_adds_a_tenant_through_the_provisioning_path(world: World) -> None:
    refused = req(
        world,
        "POST",
        "/api/admin/tenants",
        "m-user",
        json={"name": "N", "admin_name": "A", "admin_email": "a@n.test"},
    )
    assert (refused.status_code, refused.json()["detail"]) == (403, "forbidden")
    response = req(
        world,
        "POST",
        "/api/admin/tenants",
        "m-admin",
        json={"name": "Fourth  Tenant", "admin_name": "Fran", "admin_email": "fran@fourth.test"},
    )
    assert response.status_code == 202, response.text
    tenant = response.json()
    assert (tenant["name"], tenant["status"], tenant["kind"]) == (
        "Fourth Tenant",
        "provisioning",
        "business",
    )
    request = world.provisioning.started[-1]
    parsed = ProvisionRequest.model_validate(request)  # exactly what the task accepts
    assert str(parsed.tenant_id) == tenant["id"]
    assert parsed.actor.role == "master-admin"
    assert parsed.actor.user_id == world.users["m-admin"]
    assert parsed.first_admin.email == "fran@fourth.test"
    assert parsed.retry is False
    [(tenant_id, actor, _, _, _, _)] = audits(world, "created", tenant["id"])
    assert (str(tenant_id), actor) == (tenant["id"], "m-admin@example.test")


def test_a_failed_start_leaves_no_tenant(world: World) -> None:
    world.provisioning.error = RuntimeError("states unavailable")
    with pytest.raises(RuntimeError):
        req(
            world,
            "POST",
            "/api/admin/tenants",
            "m-admin",
            json={"name": "Ghost", "admin_name": "G", "admin_email": "g@g.test"},
        )
    assert row(world, "SELECT 1 FROM assay.tenants WHERE name = 'Ghost'") is None


def test_suspend_and_reactivate(world: World) -> None:
    path = f"/api/admin/tenants/{world.b}"
    assert req(world, "PATCH", path, "m-user", json={"status": "suspended"}).status_code == 403
    suspended = req(world, "PATCH", path, "m-admin", json={"status": "suspended"})
    assert suspended.json()["status"] == "suspended"
    refused = req(world, "GET", "/api/me", "b-admin")
    assert (refused.status_code, refused.json()["detail"]) == (403, "tenant_suspended")
    again = req(world, "PATCH", path, "m-admin", json={"status": "suspended"})
    assert (again.status_code, again.json()["detail"]) == (409, "status_change_not_allowed")
    assert (
        req(world, "PATCH", path, "m-admin", json={"status": "active"}).json()["status"] == "active"
    )
    assert [a[3]["after"]["status"] for a in audits(world, "suspended", world.b)] == ["suspended"]
    assert len(audits(world, "reactivated", world.b)) == 1
    platform = req(
        world,
        "PATCH",
        f"/api/admin/tenants/{world.platform}",
        "m-admin",
        json={"status": "suspended"},
    )
    assert platform.status_code == 404


def test_retry_restarts_a_failed_tenant_with_its_first_admin(world: World) -> None:
    not_failed = req(world, "POST", f"/api/admin/tenants/{world.a}/retry", "m-admin")
    assert (not_failed.status_code, not_failed.json()["detail"]) == (409, "not_failed")
    response = req(world, "POST", f"/api/admin/tenants/{world.failed}/retry", "m-admin")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "provisioning"
    parsed = ProvisionRequest.model_validate(world.provisioning.started[-1])
    assert (parsed.retry, parsed.first_admin.email, str(parsed.tenant_id)) == (
        True,
        "fay@example.test",
        str(world.failed),
    )
    assert len(audits(world, "provisioning_retried", world.failed)) == 1


def test_tenant_admins_and_platform_users(world: World) -> None:
    admins = req(world, "GET", f"/api/admin/tenants/{world.a}/admins", "m-user").json()["admins"]
    assert {a["email"] for a in admins} == {"a-admin@example.test", "a-admin2@example.test"}
    assert all(a["mfa_resettable"] for a in admins)
    platform = req(world, "GET", f"/api/admin/tenants/{world.platform}/admins", "m-admin").json()[
        "admins"
    ]
    assert {(a["role"], a["mfa_resettable"]) for a in platform} == {
        ("master-admin", False),
        ("master-user", True),
    }


def test_master_admin_resets_mfa_of_tenant_admins_and_the_master_user_only(world: World) -> None:
    target = world.users["b-admin"]
    assert req(world, "POST", f"/api/admin/users/{target}/reset-mfa", "m-user").status_code == 403
    response = req(world, "POST", f"/api/admin/users/{target}/reset-mfa", "m-admin")
    assert response.status_code == 204, response.text
    assert world.cognito.last("reset_mfa") == {
        "pool": "tenant",
        "tenant_id": str(world.b),
        "email": "b-admin@example.test",
        "sub": "b-admin",
    }
    [(tenant_id, actor, _, _, _, _)] = audits(world, "mfa_reset", target)
    assert (tenant_id, actor) == (world.b, "m-admin@example.test")

    master_user = world.users["m-user"]
    assert (
        req(world, "POST", f"/api/admin/users/{master_user}/reset-mfa", "m-admin").status_code
        == 204
    )
    assert world.cognito.last("reset_mfa")["pool"] == "platform"
    assert "tenant_id" not in world.cognito.last("reset_mfa")

    for sub in ("b-user", "m-admin"):
        response = req(world, "POST", f"/api/admin/users/{world.users[sub]}/reset-mfa", "m-admin")
        assert (response.status_code, response.json()["detail"]) == (404, "user_not_found"), sub
    with psycopg.connect(world.url) as conn:
        conn.execute("UPDATE assay.users SET sessions_revoked_at = NULL")


# --- Auth events -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sub", "tenant"), [("a-user", "a"), ("m-user", "platform"), ("a-admin", "a")]
)
@pytest.mark.parametrize(
    ("path", "action"), [("signed-out", "sign_out"), ("password-changed", "password_changed")]
)
def test_sign_out_and_password_change_are_recorded(
    world: World, sub: str, tenant: str, path: str, action: str
) -> None:
    # A tenant-admin without MFA may record these too.
    headers = {"Authorization": f"Bearer {token(world, sub, mfa=False)}", **VIEWER}
    response = world.client.post(f"/api/auth/{path}", headers=headers)
    assert response.status_code == 204, response.text
    rows = audits(world, action, world.users[sub])
    assert rows and rows[-1][0] == getattr(world, tenant)
    assert rows[-1][4] == "198.51.100.7"
