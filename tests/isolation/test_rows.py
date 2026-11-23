"""Row-level security in Aurora, as `app_tenant` (BUILD_SPEC.md section 6, rule 5).

With `app.tenant_id` set to one tenant, the other tenant's rows cannot be read or written.
Nothing is committed: every transaction is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from psycopg import errors
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from tests.isolation.conftest import Env

AsTenant = Callable[[uuid.UUID | None], Connection]

# Tenant tables app_tenant may read. Each must show no row of another tenant.
READABLE_TABLES = ("users", "batches", "documents", "pages", "chat_conversations", "fraud_scores")


def _pairs(env: Env) -> list[tuple[uuid.UUID, uuid.UUID]]:
    return [(env.tenant_a, env.tenant_b), (env.tenant_b, env.tenant_a)]


def test_a_tenant_sees_only_its_own_tenant_row(env: Env, as_tenant: AsTenant) -> None:
    for own, other in _pairs(env):
        conn = as_tenant(own)
        assert conn.execute(text("SELECT id FROM assay.tenants")).scalars().all() == [own]
        found = conn.execute(text("SELECT id FROM assay.tenants WHERE id = :id"), {"id": other})
        assert found.all() == []


def test_a_tenant_sees_its_own_users_and_not_the_others(env: Env, as_tenant: AsTenant) -> None:
    for own, other in _pairs(env):
        conn = as_tenant(own)
        tenants = conn.execute(text("SELECT DISTINCT tenant_id FROM assay.users")).scalars().all()
        # Provisioning inserted the first admin, so each tenant has at least one user.
        assert tenants == [own]
        count = conn.execute(
            text("SELECT count(*) FROM assay.users WHERE tenant_id = :id"), {"id": other}
        ).scalar_one()
        assert count == 0


@pytest.mark.parametrize("table", READABLE_TABLES)
def test_no_table_shows_another_tenants_rows(env: Env, as_tenant: AsTenant, table: str) -> None:
    assert table in READABLE_TABLES  # the name goes into SQL; it is one of ours
    for own, other in _pairs(env):
        conn = as_tenant(own)
        count = conn.execute(
            text(f"SELECT count(*) FROM assay.{table} WHERE tenant_id = :id"),
            {"id": other},
        ).scalar_one()
        assert count == 0


def test_a_tenant_cannot_insert_rows_for_another(env: Env, as_tenant: AsTenant) -> None:
    for own, other in _pairs(env):
        conn = as_tenant(own)
        with pytest.raises(ProgrammingError) as raised:
            conn.execute(
                text("INSERT INTO assay.batches (tenant_id, opened_by) VALUES (:tid, :uid)"),
                {"tid": other, "uid": uuid.uuid4()},
            )
        assert isinstance(raised.value.orig, errors.InsufficientPrivilege)
        assert "row-level security" in str(raised.value.orig)


def test_a_tenant_cannot_write_the_other_tenants_audit_rows(env: Env, as_tenant: AsTenant) -> None:
    for own, other in _pairs(env):
        conn = as_tenant(own)
        with pytest.raises(ProgrammingError) as raised:
            conn.execute(
                text(
                    "INSERT INTO assay.audit_log (tenant_id, category, action, outcome) "
                    "VALUES (:tid, 'tenant', 'isolation_probe', 'success')"
                ),
                {"tid": other},
            )
        assert isinstance(raised.value.orig, errors.InsufficientPrivilege)


def test_no_tenant_set_shows_nothing(as_tenant: AsTenant) -> None:
    conn = as_tenant(None)
    assert conn.execute(text("SELECT count(*) FROM assay.tenants")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM assay.users")).scalar_one() == 0


def test_app_tenant_cannot_read_tenant_storage(env: Env, as_tenant: AsTenant) -> None:
    """Only the TSS's database role may read where a tenant's data lives."""
    conn = as_tenant(env.tenant_a)
    with pytest.raises(ProgrammingError) as raised:
        conn.execute(text("SELECT bucket FROM assay.tenant_storage"))
    assert isinstance(raised.value.orig, errors.InsufficientPrivilege)
