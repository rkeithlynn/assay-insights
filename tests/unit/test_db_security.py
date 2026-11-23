"""Roles, row-level security and grants, against a real PostgreSQL 17 (BUILD_SPEC.md 6, 7, 7a).

Needs `ASSAY_TEST_DATABASE_URL`: a superuser URL for a throwaway server, for example

    docker run -d --name assay-pg-test -e POSTGRES_HOST_AUTH_METHOD=trust `
        -p 127.0.0.1:55432:5432 postgres:17
    $env:ASSAY_TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:55432/postgres"

Each run creates and drops its own databases. CI sets `ASSAY_REQUIRE_DB_TESTS=1` so that
a missing database fails the run instead of skipping these tests.

The bootstrap runs as the superuser (standing in for the RDS master user) and the migrations
run under `SET ROLE migrator`, as the migration Lambda does. `rds_iam` exists only on RDS, so
a stand-in role is created.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from psycopg import errors, sql
from sqlalchemy import create_engine, text

from assay_db import runner
from assay_db.models import SCHEMA, TENANT_TABLES, Base
from assay_db.roles import (
    ALL_ROLES,
    APP_TENANT,
    AUDIT_WRITER,
    LOGIN_ROLES,
    MIGRATOR,
    PLATFORM_READER,
    PLATFORM_VIEW_OWNER,
    PLATFORM_WRITER,
    RDS_IAM,
    TSS,
    bootstrap,
)

ADMIN_URL = os.environ.get("ASSAY_TEST_DATABASE_URL")
REQUIRED = os.environ.get("ASSAY_REQUIRE_DB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not ADMIN_URL and not REQUIRED, reason="ASSAY_TEST_DATABASE_URL is not set"
)

# Roles the application uses. None may change or remove an audit row.
APPLICATION_ROLES = (APP_TENANT, PLATFORM_READER, PLATFORM_WRITER, AUDIT_WRITER, TSS)

APP_TENANT_READ_WRITE = frozenset(TENANT_TABLES) - {
    "tenants",
    "tenant_models",
    "tenant_storage",
    "audit_log",
}

SEL, INS, UPD, DEL = "SELECT", "INSERT", "UPDATE", "DELETE"
TABLE_PRIVILEGES = (SEL, INS, UPD, DEL, "TRUNCATE", "REFERENCES", "TRIGGER")

# Exact table-level privileges per role. Anything not listed must be absent.
EXPECTED_PRIVILEGES: dict[str, dict[str, set[str]]] = {
    APP_TENANT: {
        **{t: {SEL, INS, UPD} for t in APP_TENANT_READ_WRITE},
        "tenants": {SEL},
        "tenant_models": {SEL},
        "ai_models": {SEL},
        "audit_log": {INS},
    },
    PLATFORM_READER: {"tenants": {SEL}, "audit_log": {SEL}},
    PLATFORM_WRITER: {
        "tenants": {SEL, INS, UPD},
        "tenant_models": {SEL, INS, UPD, DEL},
        "tenant_storage": {INS},
        "users": {INS},
        "audit_log": {INS},
        "ai_models": {SEL},
    },
    AUDIT_WRITER: {"audit_log": {INS}},
    TSS: {"tenant_storage": {SEL}},
    PLATFORM_VIEW_OWNER: {},
    MIGRATOR: {},  # owner; checked separately
}

INGESTION_VIEW_COLUMNS = [
    "document_id",
    "tenant_id",
    "tenant_name",
    "batch_id",
    "status",
    "error_code",
    "page_count",
    "size_bytes",
    "has_companion",
    "part_count",
    "received_at",
    "completed_at",
]


@dataclass(frozen=True)
class Seed:
    platform: uuid.UUID
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    user_a: uuid.UUID
    user_b: uuid.UUID
    batch_a: uuid.UUID
    batch_b: uuid.UUID
    doc_a: uuid.UUID
    doc_b: uuid.UUID


@dataclass(frozen=True)
class TestDb:
    url: str
    seed: Seed


def _db_url(name: str) -> str:
    assert ADMIN_URL
    return ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"


def _sqlalchemy_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def _create_database(name: str) -> str:
    if not ADMIN_URL:
        pytest.fail("ASSAY_REQUIRE_DB_TESTS=1 but ASSAY_TEST_DATABASE_URL is not set")
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RDS_IAM,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(RDS_IAM)))
    url = _db_url(name)
    with psycopg.connect(url) as conn:
        bootstrap(conn, name)
    return url


def _drop_database(name: str) -> None:
    assert ADMIN_URL
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
        conn.execute(drop.format(sql.Identifier(name)))


def _migrate(url: str, revision: str = "head", *, down: bool = False) -> None:
    engine = create_engine(_sqlalchemy_url(url))
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET ROLE {MIGRATOR}"))
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


@pytest.fixture(scope="module")
def db() -> Iterator[TestDb]:
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    url = _create_database(name)
    try:
        _migrate(url)
        yield TestDb(url=url, seed=_seed(url))
    finally:
        _drop_database(name)


@contextmanager
def as_role(
    db: TestDb, role: str, tenant: uuid.UUID | str | None = None
) -> Iterator[psycopg.Connection]:
    """A transaction as `role`, with `SET LOCAL app.tenant_id` when a tenant is given."""
    with psycopg.connect(db.url) as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        try:
            yield conn
        finally:
            conn.rollback()


def _count(conn: psycopg.Connection, table: str) -> int:
    query = sql.SQL("SELECT count(*) FROM {}.{}").format(
        sql.Identifier(SCHEMA), sql.Identifier(table)
    )
    row = conn.execute(query).fetchone()
    assert row is not None
    return int(row[0])


# --- Structure ------------------------------------------------------------------------


def test_every_table_except_ai_models_has_tenant_id_and_forced_rls(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                           AND a.attname = 'tenant_id' AND NOT a.attisdropped),
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'assay' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
              AND c.relname <> 'alembic_version'
            """
        ).fetchall()
    tables = {r[0]: r[1:] for r in rows}
    assert set(tables) == {*TENANT_TABLES, "ai_models"}
    assert len(TENANT_TABLES) == 23
    for name, (rls, forced, has_tenant_id, policies) in tables.items():
        if name == "ai_models":
            assert not has_tenant_id
            continue
        assert rls and forced, f"{name}: row-level security must be enabled and forced"
        assert policies > 0, f"{name}: no policy"
        assert has_tenant_id or name == "tenants", f"{name}: no tenant_id column"


def test_role_attributes(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
            "rolcanlogin, rolpassword IS NOT NULL, pg_has_role(rolname, %s, 'MEMBER') "
            "FROM pg_authid WHERE rolname = ANY(%s)",
            (RDS_IAM, list(ALL_ROLES)),
        ).fetchall()
    assert {r[0] for r in rows} == set(ALL_ROLES)
    for name, superuser, bypass, createrole, createdb, repl, login, password, iam in rows:
        assert not (superuser or bypass or createrole or createdb or repl), name
        assert not password, f"{name} must have no password (IAM authentication only)"
        assert login == (name in LOGIN_ROLES), name
        assert iam == (name in LOGIN_ROLES), name


def test_migrator_cannot_inherit_the_view_owner(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            "SELECT pg_has_role(%s, %s, 'USAGE'), pg_has_role(%s, %s, 'SET')",
            (MIGRATOR, PLATFORM_VIEW_OWNER, MIGRATOR, PLATFORM_VIEW_OWNER),
        ).fetchone()
    assert row == (False, True)


def test_table_privileges_are_exactly_as_designed(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'assay' AND c.relkind IN ('r', 'p', 'v')"
            ).fetchall()
        ]
        assert any(t.startswith("audit_log_2") for t in tables)  # partitions are included
        for role, expected in EXPECTED_PRIVILEGES.items():
            if role == MIGRATOR:
                continue
            for table in tables:
                held = {
                    p
                    for p in TABLE_PRIVILEGES
                    if conn.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (role, f"assay.{table}", p)
                    ).fetchone()
                    == (True,)
                }
                assert held == expected.get(table, set()), f"{role} on {table}: {sorted(held)}"


def test_tenant_admin_may_update_only_model_selection_columns(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT attname, has_column_privilege(%s, 'assay.tenants', attname, 'UPDATE') "
            "FROM pg_attribute WHERE attrelid = 'assay.tenants'::regclass AND attnum > 0",
            (APP_TENANT,),
        ).fetchall()
    assert {name for name, allowed in rows if allowed} == {"extraction_model", "answer_model"}


def test_alembic_matches_models(db: TestDb) -> None:
    engine = create_engine(_sqlalchemy_url(db.url))
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET ROLE {MIGRATOR}"))
            context = MigrationContext.configure(
                conn,
                opts={
                    "include_schemas": True,
                    "include_name": lambda name, type_, _p: (
                        name == SCHEMA if type_ == "schema" else True
                    ),
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        not (
                            type_ == "table"
                            and reflected
                            and compare_to is None
                            and (name or "").startswith("audit_log_")
                        )
                    ),
                    "compare_type": True,
                    "version_table_schema": SCHEMA,
                },
            )
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diffs == []


# --- Tenant isolation (app_tenant) ----------------------------------------------------


@pytest.mark.parametrize("tenant", [None, ""])
def test_unset_tenant_sees_nothing(db: TestDb, tenant: str | None) -> None:
    with as_role(db, APP_TENANT, tenant) as conn:
        for table in ("tenants", "users", "batches", "documents", "document_parts"):
            assert _count(conn, table) == 0, table


def test_tenant_sees_only_its_own_rows(db: TestDb) -> None:
    s = db.seed
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM assay.tenants").fetchall()] == [
            s.tenant_a
        ]
        docs = conn.execute("SELECT id, tenant_id FROM assay.documents").fetchall()
        assert docs == [(s.doc_a, s.tenant_a)]
        users = conn.execute("SELECT tenant_id FROM assay.users").fetchall()
        assert users == [(s.tenant_a,)]
        # Guessing another tenant's document ID finds nothing.
        guess = conn.execute("SELECT 1 FROM assay.documents WHERE id = %s", (s.doc_b,))
        assert guess.fetchall() == []


def test_tenant_cannot_insert_rows_for_another_tenant(db: TestDb) -> None:
    s = db.seed
    with (
        as_role(db, APP_TENANT, s.tenant_a) as conn,
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
    ):
        conn.execute(
            "INSERT INTO assay.batches (tenant_id, opened_by, status) VALUES (%s, %s, 'closed')",
            (s.tenant_b, s.user_b),
        )


def test_tenant_cannot_move_a_row_to_another_tenant(db: TestDb) -> None:
    s = db.seed
    with (
        as_role(db, APP_TENANT, s.tenant_a) as conn,
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
    ):
        conn.execute(
            "UPDATE assay.document_parts SET tenant_id = %s WHERE document_id = %s",
            (s.tenant_b, s.doc_a),
        )


def test_tenant_cannot_change_another_tenants_rows(db: TestDb) -> None:
    s = db.seed
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        cur = conn.execute("UPDATE assay.documents SET status = 'failed' WHERE id = %s", (s.doc_b,))
        assert cur.rowcount == 0


def test_tenant_cannot_delete(db: TestDb) -> None:
    with (
        as_role(db, APP_TENANT, db.seed.tenant_a) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("DELETE FROM assay.documents")


def test_foreign_keys_reject_cross_tenant_references(db: TestDb) -> None:
    """Even a role that bypasses RLS cannot attach tenant A's document to B's batch."""
    s = db.seed
    with psycopg.connect(db.url) as conn, pytest.raises(errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO assay.documents (tenant_id, batch_id, uploaded_by, file_name) "
            "VALUES (%s, %s, %s, 'x.pdf')",
            (s.tenant_a, s.batch_b, s.user_a),
        )


def test_one_open_batch_per_user(db: TestDb) -> None:
    s = db.seed
    with as_role(db, APP_TENANT, s.tenant_a) as conn, pytest.raises(errors.UniqueViolation):
        conn.execute(
            "INSERT INTO assay.batches (tenant_id, opened_by) VALUES (%s, %s)",
            (s.tenant_a, s.user_a),
        )


def test_table_owner_is_subject_to_rls(db: TestDb) -> None:
    """FORCE ROW LEVEL SECURITY: even migrator, which owns the tables, sees no rows."""
    with as_role(db, MIGRATOR, db.seed.tenant_a) as conn:
        assert _count(conn, "documents") == 0
        assert _count(conn, "tenant_storage") == 0


# --- Audit trail (section 7a) ---------------------------------------------------------


@pytest.mark.parametrize("role", APPLICATION_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE assay.audit_log SET outcome = 'changed'",
        "DELETE FROM assay.audit_log",
        "TRUNCATE assay.audit_log",
        "DELETE FROM assay.audit_log_default",
    ],
)
def test_audit_log_is_append_only(db: TestDb, role: str, statement: str) -> None:
    with as_role(db, role, db.seed.tenant_a) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)


@pytest.mark.parametrize("role", [APP_TENANT, AUDIT_WRITER])
def test_tenant_side_audit_writers_insert_only_for_their_tenant(db: TestDb, role: str) -> None:
    s = db.seed
    insert = (
        "INSERT INTO assay.audit_log (tenant_id, category, action, outcome) "
        "VALUES (%s, 'auth', 'sign_in', 'success')"
    )
    with as_role(db, role, s.tenant_a) as conn:
        conn.execute(insert, (s.tenant_a,))
    with (
        as_role(db, role, s.tenant_a) as conn,
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
    ):
        conn.execute(insert, (s.tenant_b,))
    with as_role(db, role, s.tenant_a) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM assay.audit_log")


def test_audit_partitions_cover_this_month_and_the_next_twelve(db: TestDb) -> None:
    today = dt.datetime.now(dt.UTC).date().replace(day=1)
    expected = set()
    y, m = today.year, today.month
    for _ in range(13):
        expected.add(f"audit_log_{y:04d}_{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    with psycopg.connect(db.url) as conn:
        parts = {
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'assay.audit_log'::regclass"
            ).fetchall()
        }
        landed = conn.execute(
            "SELECT DISTINCT tableoid::regclass::text FROM assay.audit_log"
        ).fetchall()
    assert parts == expected | {"audit_log_default"}
    assert landed == [(f"assay.audit_log_{today:%Y_%m}",)]

    engine = create_engine(_sqlalchemy_url(db.url))
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET ROLE {MIGRATOR}"))
            assert runner.ensure_audit_partitions(conn) == []
    finally:
        engine.dispose()


# --- Platform roles -------------------------------------------------------------------


def test_platform_reader_sees_tenants_view_and_audit_across_tenants(db: TestDb) -> None:
    s = db.seed
    with as_role(db, PLATFORM_READER) as conn:
        assert _count(conn, "tenants") == 3
        assert _count(conn, "audit_log") >= 2
        rows = conn.execute(
            "SELECT document_id, tenant_id, tenant_name, part_count "
            "FROM platform.platform_ingestion_v ORDER BY tenant_name"
        ).fetchall()
    assert rows == [(s.doc_a, s.tenant_a, "Tenant A", 1), (s.doc_b, s.tenant_b, "Tenant B", 1)]


@pytest.mark.parametrize(
    "table", sorted((set(TENANT_TABLES) | {"ai_models"}) - {"tenants", "audit_log"})
)
def test_platform_reader_has_no_access_to_other_tables(db: TestDb, table: str) -> None:
    with as_role(db, PLATFORM_READER) as conn, pytest.raises(errors.InsufficientPrivilege):
        _count(conn, table)


def test_ingestion_view_exposes_metadata_only(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        columns = [
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'platform' AND table_name = 'platform_ingestion_v' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        owner = conn.execute(
            "SELECT viewowner FROM pg_views WHERE schemaname = 'platform' "
            "AND viewname = 'platform_ingestion_v'"
        ).fetchone()
        file_name_readable = conn.execute(
            "SELECT has_column_privilege(%s, 'assay.documents', 'file_name', 'SELECT')",
            (PLATFORM_VIEW_OWNER,),
        ).fetchone()
    assert columns == INGESTION_VIEW_COLUMNS
    assert "file_name" not in columns
    assert owner == (PLATFORM_VIEW_OWNER,)
    assert file_name_readable == (False,)


@pytest.mark.parametrize("role", [APP_TENANT, PLATFORM_READER, PLATFORM_WRITER, AUDIT_WRITER])
def test_only_tss_reads_tenant_storage(db: TestDb, role: str) -> None:
    with as_role(db, role, db.seed.tenant_a) as conn, pytest.raises(errors.InsufficientPrivilege):
        _count(conn, "tenant_storage")


def test_tss_reads_one_tenants_storage_at_a_time(db: TestDb) -> None:
    s = db.seed
    with as_role(db, TSS, s.tenant_a) as conn:
        rows = conn.execute("SELECT tenant_id, bucket FROM assay.tenant_storage").fetchall()
    assert rows == [(s.tenant_a, "bucket-a")]
    with as_role(db, TSS) as conn:
        assert _count(conn, "tenant_storage") == 0


def test_platform_writer_provisions_a_tenant(db: TestDb) -> None:
    new = uuid.uuid4()
    with as_role(db, PLATFORM_WRITER) as conn:
        conn.execute(
            "INSERT INTO assay.tenants (id, name, kind) VALUES (%s, 'New', 'business')", (new,)
        )
        # platform_writer cannot read tenant_storage, so a retry uses ON CONFLICT DO NOTHING
        # with no conflict target (a target would need SELECT on its columns).
        storage = (
            "INSERT INTO assay.tenant_storage (tenant_id, mode, bucket, kms_key_arn, role_arn, "
            "vector_bucket, vector_index, region, status) "
            "VALUES (%s, 'dedicated', 'b', 'k', 'r', 'vb', 'vi', 'region', 'active') "
            "ON CONFLICT DO NOTHING"
        )
        assert conn.execute(storage, (new,)).rowcount == 1
        assert conn.execute(storage, (new,)).rowcount == 0
        cur = conn.execute("UPDATE assay.tenants SET status = 'active' WHERE id = %s", (new,))
        assert cur.rowcount == 1
        conn.execute(
            "INSERT INTO assay.audit_log (tenant_id, category, action, outcome) "
            "VALUES (%s, 'tenant', 'provisioning_succeeded', 'success')",
            (new,),
        )


# --- Lifecycle ------------------------------------------------------------------------


def test_bootstrap_is_idempotent(db: TestDb) -> None:
    name = db.url.rsplit("/", 1)[1]
    with psycopg.connect(db.url) as conn:
        result = bootstrap(conn, name)
    assert result.created == []
    assert sorted(result.existing) == sorted(ALL_ROLES)


def test_downgrade_to_base_and_upgrade_again() -> None:
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    url = _create_database(name)
    try:
        _migrate(url)
        _migrate(url, "base", down=True)
        _migrate(url)
        engine = create_engine(_sqlalchemy_url(url))
        try:
            with engine.connect() as conn:
                assert runner.current_revision(conn) == "0001"
        finally:
            engine.dispose()
    finally:
        _drop_database(name)
