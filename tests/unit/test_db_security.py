"""Roles, row-level security and grants, against a real PostgreSQL 17 (BUILD_SPEC.md 6, 7, 7a).

The database server, the master stand-in and the session database come from
`tests/unit/pgtest.py`; see there for how to run one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from psycopg import errors, sql

from assay_db import checks, handler, roles, runner
from assay_db.models import SCHEMA, TENANT_TABLES, Base
from assay_db.roles import (
    ALL_ROLES,
    APP_TENANT,
    AUDIT_WRITER,
    AUTH_EVENT_RECORDER,
    LOGIN_ROLES,
    MIGRATOR,
    PIPELINE_LOOKUP,
    PLATFORM_READER,
    PLATFORM_VIEW_OWNER,
    PLATFORM_WRITER,
    RDS_IAM,
    TSS,
    RoleVerificationError,
)
from tests.unit.pgtest import (
    HEAD_REVISION,
    MASTER,
    TestDb,
    _admin,
    _bootstrap_as_master,
    _conninfo,
    _create_database,
    _drop_database,
    _migrate,
    as_role,
    migrator_engine,
    requires_db,
)

pytestmark = requires_db

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
    # Owns auth_events.record() (migration 0004); column-level SELECT and UPDATE on users.
    AUTH_EVENT_RECORDER: {"audit_log": {INS}},
    # Owns the two lookups in schema pipeline (migration 0008); column-level SELECT only.
    PIPELINE_LOOKUP: {},
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
    assert len(TENANT_TABLES) == 24
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
    engine = migrator_engine(db.name)
    try:
        with engine.connect() as conn:
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

    engine = migrator_engine(db.name)
    try:
        with engine.begin() as conn:
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


PLATFORM_USERS_VIEW_COLUMNS = [
    "user_id",
    "tenant_id",
    "tenant_name",
    "tenant_status",
    "cognito_sub",
    "email",
    "name",
    "role",
    "status",
    "sessions_revoked_at",
]


def test_platform_reader_sees_only_platform_users_through_the_view(db: TestDb) -> None:
    s = db.seed
    with as_role(db, PLATFORM_READER) as conn:
        rows = conn.execute(
            "SELECT user_id, tenant_id, tenant_name, tenant_status, cognito_sub, role "
            "FROM platform.platform_users_v"
        ).fetchall()
    assert rows == [
        (s.user_platform, s.platform, "Platform", "active", "sub-platform", "master-admin")
    ]


def test_platform_users_view_shape_and_owner(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        columns = [
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'platform' AND table_name = 'platform_users_v' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        owner = conn.execute(
            "SELECT viewowner FROM pg_views WHERE schemaname = 'platform' "
            "AND viewname = 'platform_users_v'"
        ).fetchone()
        readable = {
            r[0]
            for r in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = 'assay.users'::regclass "
                "AND attnum > 0 AND NOT attisdropped "
                "AND has_column_privilege(%s, 'assay.users', attname, 'SELECT')",
                (PLATFORM_VIEW_OWNER,),
            ).fetchall()
        }
        grantees = {
            r[0]
            for r in conn.execute(
                "SELECT grantee FROM information_schema.role_table_grants "
                "WHERE table_schema = 'platform' AND table_name = 'platform_users_v'"
            ).fetchall()
        }
    assert columns == PLATFORM_USERS_VIEW_COLUMNS
    assert owner == (PLATFORM_VIEW_OWNER,)
    assert readable == {
        "id",
        "tenant_id",
        "cognito_sub",
        "email",
        "name",
        "role",
        "status",
        "sessions_revoked_at",
    }
    assert grantees == {PLATFORM_VIEW_OWNER, PLATFORM_READER}


def test_view_owner_reads_users_only_through_its_column_grants(db: TestDb) -> None:
    """Owner decision D8 (build step 5): the view owner's policy on users is `true` and each
    view filters for itself. It still cannot sign in and holds column-level SELECT only.
    Whether any view shows a business tenant's non-admin users: tests/unit/test_db_step5.py.
    """
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            "SELECT rolcanlogin, has_table_privilege(rolname, 'assay.users', 'SELECT') "
            "FROM pg_roles WHERE rolname = %s",
            (PLATFORM_VIEW_OWNER,),
        ).fetchone()
    assert row == (False, False)


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


def test_failed_first_bootstrap_leaves_no_role_behind(db: TestDb) -> None:
    """The production failure: bootstrap raised after CREATE ROLE. Nothing was committed."""
    assert db.roles_after_failed_attempt == []


def test_first_bootstrap_as_master_created_every_role(db: TestDb) -> None:
    assert db.first_bootstrap.created == list(ALL_ROLES)
    assert db.first_bootstrap.existing == []


def test_rerunning_bootstrap_as_master_is_safe(db: TestDb) -> None:
    result = _bootstrap_as_master(db.name)
    assert result.created == []
    assert result.existing == list(ALL_ROLES)
    with psycopg.connect(db.url) as conn:
        roles.verify_roles(conn)


def test_bootstrap_on_a_second_database_with_existing_roles(db: TestDb) -> None:
    """The ALTER path for existing roles, as the master stand-in, on a fresh database."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        result = _bootstrap_as_master(name)
        assert result.created == []
        with psycopg.connect(_conninfo(name)) as conn:
            row = conn.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CREATE'), "
                "has_database_privilege('public', current_database(), 'CONNECT')",
                (MIGRATOR,),
            ).fetchone()
        assert row == (True, False)
    finally:
        _drop_database(name)


@pytest.mark.parametrize(
    ("tamper", "undo", "expected"),
    [
        (
            "ALTER ROLE app_tenant BYPASSRLS",
            "ALTER ROLE app_tenant NOBYPASSRLS",
            "app_tenant: has BYPASSRLS",
        ),
        (
            "ALTER ROLE platform_writer CREATEROLE",
            "ALTER ROLE platform_writer NOCREATEROLE",
            "platform_writer: has CREATEROLE",
        ),
        (
            "ALTER ROLE tss REPLICATION",
            "ALTER ROLE tss NOREPLICATION",
            "tss: has REPLICATION",
        ),
        (
            "GRANT pg_read_all_data TO platform_reader",
            "REVOKE pg_read_all_data FROM platform_reader",
            "platform_reader: unexpected membership of pg_read_all_data",
        ),
        (
            "ALTER ROLE migrator NOLOGIN",
            "ALTER ROLE migrator LOGIN",
            None,  # the bootstrap may and does fix LOGIN itself
        ),
    ],
)
def test_bootstrap_verifies_instead_of_forcing(
    db: TestDb, tamper: str, undo: str, expected: str | None
) -> None:
    """Attributes the master user cannot change stop the bootstrap, and it rolls back."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    with _admin() as conn:
        conn.execute(tamper)
    try:
        if expected is None:
            _bootstrap_as_master(name)
            granted = True
        else:
            with pytest.raises(RoleVerificationError, match=expected):
                _bootstrap_as_master(name)
            granted = False
        # On failure the run's database-level grants were rolled back with everything else.
        with psycopg.connect(_conninfo(name)) as conn:
            row = conn.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CREATE')", (MIGRATOR,)
            ).fetchone()
        assert row == (granted,)
    finally:
        with _admin() as conn:
            conn.execute(undo)
        _drop_database(name)
    with psycopg.connect(db.url) as conn:
        roles.verify_roles(conn)


def test_bootstrap_stops_at_a_role_made_superuser(db: TestDb) -> None:
    """The master user may not even alter a superuser role, so the bootstrap stops there."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    with _admin() as conn:
        conn.execute("ALTER ROLE tss SUPERUSER")
    try:
        with pytest.raises((RoleVerificationError, errors.InsufficientPrivilege)):
            _bootstrap_as_master(name)
    finally:
        with _admin() as conn:
            conn.execute("ALTER ROLE tss NOSUPERUSER")
        _drop_database(name)
    with psycopg.connect(db.url) as conn:
        roles.verify_roles(conn)


def test_downgrade_to_base_and_upgrade_again(db: TestDb) -> None:
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name)
        _migrate(name, "base", down=True)
        _migrate(name)
        engine = migrator_engine(name)
        try:
            with engine.connect() as conn:
                assert runner.current_revision(conn) == HEAD_REVISION
        finally:
            engine.dispose()
    finally:
        _drop_database(name)


# --- Read-only verification (the Lambda's `verify` command) ----------------------------


def test_verify_rls_passes_on_the_migrated_schema(db: TestDb) -> None:
    with psycopg.connect(_conninfo(db.name, MIGRATOR)) as conn:
        report = checks.verify_rls(conn)
    assert report.tenant_tables == sorted(TENANT_TABLES)
    assert report.platform_tables == ["ai_models"]


@pytest.mark.parametrize(
    ("tamper", "undo", "expected"),
    [
        (
            "ALTER TABLE assay.documents NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE assay.documents FORCE ROW LEVEL SECURITY",
            "documents: row-level security not forced",
        ),
        (
            "ALTER TABLE assay.pages DISABLE ROW LEVEL SECURITY",
            "ALTER TABLE assay.pages ENABLE ROW LEVEL SECURITY",
            "pages: row-level security not enabled",
        ),
        (
            "CREATE TABLE assay.rogue (id int)",
            "DROP TABLE assay.rogue",
            "rogue: table not in the models",
        ),
    ],
)
def test_verify_rls_catches_a_broken_table(
    db: TestDb, tamper: str, undo: str, expected: str
) -> None:
    with psycopg.connect(db.url, autocommit=True) as admin:
        admin.execute(tamper)
        try:
            with (
                psycopg.connect(_conninfo(db.name, MIGRATOR)) as conn,
                pytest.raises(checks.RlsVerificationError, match=expected),
            ):
                checks.verify_rls(conn)
        finally:
            admin.execute(undo)


@pytest.fixture
def lambda_env(db: TestDb, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs the Lambda handler against the test server instead of Aurora."""
    for key, value in {
        "DB_HOST": "unused",
        "DB_PORT": "5432",
        "DB_NAME": db.name,
        "AWS_REGION": "us-east-1",
        "DB_CA_BUNDLE": "unused",
        "DB_MASTER_SECRET_ARN": "unused",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(handler, "iam_engine", lambda _target, _user: migrator_engine(db.name))
    monkeypatch.setattr(
        handler,
        "master_connection",
        lambda _target, _arn: psycopg.connect(_conninfo(db.name, MASTER)),
    )


@pytest.mark.usefixtures("lambda_env")
def test_handler_verify_command() -> None:
    result = handler.handler({"command": "verify"}, None)
    assert result["rls_verified"] is True
    assert result["roles_verified"] is True
    assert result["rls_tenant_tables"] == sorted(TENANT_TABLES)
    assert result["revision_after"] == HEAD_REVISION
    assert "roles_created" not in result  # verify never runs the bootstrap


@pytest.mark.usefixtures("lambda_env")
def test_handler_upgrade_is_idempotent_and_verifies() -> None:
    result = handler.handler({"command": "upgrade"}, None)
    assert result["roles_created"] == []
    assert result["roles_existing"] == list(ALL_ROLES)
    assert result["rls_verified"] is True
    assert result["partitions_created"] == []
    assert result["revision_before"] == result["revision_after"] == HEAD_REVISION


@pytest.mark.usefixtures("lambda_env")
def test_handler_upgrade_rolls_back_when_rls_is_broken(db: TestDb) -> None:
    with psycopg.connect(db.url, autocommit=True) as admin:
        admin.execute("ALTER TABLE assay.documents NO FORCE ROW LEVEL SECURITY")
        try:
            with pytest.raises(checks.RlsVerificationError, match="documents"):
                handler.handler({"command": "upgrade"}, None)
        finally:
            admin.execute("ALTER TABLE assay.documents FORCE ROW LEVEL SECURITY")


def test_handler_rejects_unknown_commands() -> None:
    with pytest.raises(ValueError, match="unknown command"):
        handler.handler({"command": "drop-everything"}, None)
