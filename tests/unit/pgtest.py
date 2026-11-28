"""A throwaway PostgreSQL 17 for the database and API tests, shared by every test module.

Needs `ASSAY_TEST_DATABASE_URL`: a superuser URL for a throwaway server, for example

    docker run -d --name assay-pg-test -e POSTGRES_HOST_AUTH_METHOD=trust `
        -p 127.0.0.1:55432:5432 postgres:17
    $env:ASSAY_TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:55432/postgres"

The server must be throwaway and must trust local sign-ins: at the start of a run the `db`
fixture drops leftover `assay_test_*` databases and the application roles (roles are
cluster-wide), so that the bootstrap creates them from scratch. That reset happens once per
run, which is why `db` is a session fixture registered in conftest.py and every module that
needs a database depends on it. CI sets `ASSAY_REQUIRE_DB_TESTS=1` so that a missing database
fails the run instead of skipping it.

Aurora's master user is not a superuser, so neither is the role the bootstrap runs as here.
`rds_master_test` mimics it: CREATEROLE, CREATEDB, owner of the database, ADMIN OPTION on a
stand-in `rds_iam`, and no SUPERUSER, BYPASSRLS or REPLICATION. Migrations and the role
checks sign in directly as `migrator`, `app_tenant` and the rest (on Aurora they use IAM
tokens). The superuser only sets up the server, seeds rows and reads catalogs that a
non-superuser cannot see.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from unittest import mock

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from assay_db import roles, runner
from assay_db.roles import (
    ALL_ROLES,
    MIGRATOR,
    RDS_IAM,
    BootstrapResult,
    RoleVerificationError,
    bootstrap,
)

ADMIN_URL = os.environ.get("ASSAY_TEST_DATABASE_URL")
REQUIRED = os.environ.get("ASSAY_REQUIRE_DB_TESTS") == "1"

# Every module that uses the database sets `pytestmark = requires_db`.
requires_db = pytest.mark.skipif(
    not ADMIN_URL and not REQUIRED, reason="ASSAY_TEST_DATABASE_URL is not set"
)

# The Alembic head every test database is migrated to.
HEAD_REVISION = "0012"

# Stands in for the Aurora master user (rds_superuser member, not a superuser).
MASTER = "rds_master_test"


@dataclass(frozen=True)
class Seed:
    platform: uuid.UUID
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    user_platform: uuid.UUID
    user_a: uuid.UUID
    user_b: uuid.UUID
    batch_a: uuid.UUID
    batch_b: uuid.UUID
    doc_a: uuid.UUID
    doc_b: uuid.UUID


@dataclass(frozen=True)
class TestDb:
    __test__ = False  # not a test class, despite the name

    name: str
    url: str  # superuser, for seeding and catalog checks only
    seed: Seed
    # The forced failure of the very first bootstrap (see `db`) and the first real one.
    roles_after_failed_attempt: list[str]
    first_bootstrap: BootstrapResult


def _conninfo(dbname: str, user: str | None = None) -> str:
    if not ADMIN_URL:
        pytest.fail("ASSAY_REQUIRE_DB_TESTS=1 but ASSAY_TEST_DATABASE_URL is not set")
    if user is None:
        return make_conninfo(ADMIN_URL, dbname=dbname)
    return make_conninfo(ADMIN_URL, dbname=dbname, user=user)


def _admin() -> psycopg.Connection:
    return psycopg.connect(_conninfo("postgres"), autocommit=True)


def _existing_app_roles(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(ALL_ROLES),),
    ).fetchall()
    return [r[0] for r in rows]


def _create_database(name: str) -> None:
    """A database owned by the master stand-in, as RDS creates the cluster's database."""
    with _admin() as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(MASTER)
            )
        )


def _bootstrap_as_master(name: str) -> BootstrapResult:
    with psycopg.connect(_conninfo(name, MASTER)) as conn:
        row = conn.execute(
            "SELECT current_user::text, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert row == (MASTER, False, False), row
        return bootstrap(conn, name)


def _drop_database(name: str) -> None:
    with _admin() as conn:
        drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
        conn.execute(drop.format(sql.Identifier(name)))


def migrator_engine(name: str) -> Engine:
    """Signs in as migrator itself, as the Lambda does (no superuser session, no SET ROLE)."""
    return create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(_conninfo(name, MIGRATOR)),
        poolclass=NullPool,
    )


def _migrate(name: str, revision: str = "head", *, down: bool = False) -> None:
    engine = migrator_engine(name)
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT session_user::text")).scalar_one() == MIGRATOR
            if down:
                runner.downgrade(conn, revision)
            else:
                runner.upgrade(conn, revision)
    finally:
        engine.dispose()


def _seed(url: str) -> Seed:
    """Two business tenants and the platform tenant, inserted as superuser (bypasses RLS)."""
    ids = {k: uuid.uuid4() for k in Seed.__dataclass_fields__}
    s = Seed(**ids)
    with psycopg.connect(url) as conn:
        for tid, name, kind in (
            (s.platform, "Platform", "platform"),
            (s.tenant_a, "Tenant A", "business"),
            (s.tenant_b, "Tenant B", "business"),
        ):
            conn.execute(
                "INSERT INTO assay.tenants (id, name, kind, status) VALUES (%s, %s, %s, 'active')",
                (tid, name, kind),
            )
        conn.execute(
            "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role) "
            "VALUES (%s, %s, 'sub-platform', 'master@example.test', 'Master', 'master-admin')",
            (s.user_platform, s.platform),
        )
        for tid, uid, batch, doc, tag in (
            (s.tenant_a, s.user_a, s.batch_a, s.doc_a, "a"),
            (s.tenant_b, s.user_b, s.batch_b, s.doc_b, "b"),
        ):
            conn.execute(
                "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role) "
                "VALUES (%s, %s, %s, %s, %s, 'tenant-admin')",
                (uid, tid, f"sub-{tag}", f"admin-{tag}@example.test", f"Admin {tag}"),
            )
            conn.execute(
                "INSERT INTO assay.batches (id, tenant_id, opened_by) VALUES (%s, %s, %s)",
                (batch, tid, uid),
            )
            conn.execute(
                "INSERT INTO assay.documents (id, tenant_id, batch_id, uploaded_by, file_name, "
                "page_count, size_bytes) VALUES (%s, %s, %s, %s, %s, 3, 1000)",
                (doc, tid, batch, uid, f"secret-name-{tag}.pdf"),
            )
            conn.execute(
                "INSERT INTO assay.document_parts (tenant_id, document_id, start_page, end_page, "
                "form_type, method) VALUES (%s, %s, 1, 3, 'GeneralForm', 'rule')",
                (tid, doc),
            )
            conn.execute(
                "INSERT INTO assay.tenant_storage (tenant_id, mode, bucket, kms_key_arn, role_arn, "
                "vector_bucket, vector_index, region, status) "
                "VALUES (%s, 'dedicated', %s, 'k', 'r', 'vb', 'vi', 'region', 'active')",
                (tid, f"bucket-{tag}"),
            )
            conn.execute(
                "INSERT INTO assay.audit_log (tenant_id, category, action, outcome) "
                "VALUES (%s, 'tenant', 'created', 'success')",
                (tid,),
            )
    return s


def _reset_server() -> None:
    """Drop leftovers from earlier runs and set up the master stand-in."""
    with _admin() as conn:
        leftovers = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'assay\\_test\\_%'"
        ).fetchall()
        for (name,) in leftovers:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        for role in ALL_ROLES:
            try:
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            except errors.DependentObjectsStillExist as exc:
                pytest.fail(f"role {role} owns objects outside the test databases: {exc}")
        for role, attributes in (
            (RDS_IAM, "NOLOGIN"),
            (MASTER, "LOGIN CREATEROLE CREATEDB"),
        ):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH {} NOSUPERUSER NOBYPASSRLS NOREPLICATION").format(
                    sql.Identifier(role), sql.SQL(attributes)
                )
            )
        # As on RDS: the master user may grant rds_iam.
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                sql.Identifier(RDS_IAM), sql.Identifier(MASTER)
            )
        )


@pytest.fixture(scope="session")
def db() -> Iterator[TestDb]:
    _reset_server()
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        # The failed production run, reproduced: the very first bootstrap fails part-way,
        # after its CREATE ROLE statements. Nothing may survive the rollback.
        with (
            mock.patch.object(roles, "verify_roles", side_effect=RoleVerificationError("boom")),
            pytest.raises(RoleVerificationError),
        ):
            _bootstrap_as_master(name)
        with _admin() as conn:
            left_behind = _existing_app_roles(conn)

        first = _bootstrap_as_master(name)
        _migrate(name)
        url = _conninfo(name)
        yield TestDb(
            name=name,
            url=url,
            seed=_seed(url),
            roles_after_failed_attempt=left_behind,
            first_bootstrap=first,
        )
    finally:
        _drop_database(name)


@contextmanager
def as_role(
    db: TestDb, role: str, tenant: uuid.UUID | str | None = None
) -> Iterator[psycopg.Connection]:
    """A transaction signed in as `role`, with `SET LOCAL app.tenant_id` when given a tenant."""
    with psycopg.connect(_conninfo(db.name, role)) as conn:
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        try:
            yield conn
        finally:
            conn.rollback()
