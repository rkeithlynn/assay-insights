"""Migration 0008 (build step 6): batch_files, the pipeline lookups, duplicates, the step view."""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from psycopg import errors

from assay_db.roles import APP_TENANT, PIPELINE_LOOKUP, PLATFORM_READER, PLATFORM_WRITER
from tests.unit.pgtest import (
    TestDb,
    _bootstrap_as_master,
    _conninfo,
    _create_database,
    _drop_database,
    _migrate,
    as_role,
    requires_db,
)

pytestmark = requires_db

FUNCTIONS = ("tenant_for_upload", "tenants_with_work")


def _file(
    db: TestDb, tenant: uuid.UUID, user: uuid.UUID, batch: uuid.UUID
) -> tuple[uuid.UUID, str]:
    file_id = uuid.uuid4()
    key = f"inbox/{batch}/{file_id}"
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "INSERT INTO assay.batch_files (id, tenant_id, batch_id, kind, file_name, base_name, "
            "size_bytes, sha256, s3_key, content_type, registered_by) VALUES "
            "(%s, %s, %s, 'companion', 'secret-name.json', 'secret-name', 10, %s, %s, "
            "'application/json', %s)",
            (file_id, tenant, batch, "a" * 64, key, user),
        )
    return file_id, key


def test_the_lookups_are_locked_down_security_definers(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT p.proname, p.prosecdef, p.proconfig, r.rolname, p.proacl::text "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'pipeline'"
        ).fetchall()
    assert {r[0] for r in rows} == set(FUNCTIONS)
    for name, definer, config, owner, acl in rows:
        assert definer, name
        assert config == ["search_path=pg_catalog, pg_temp"], name
        assert owner == PIPELINE_LOOKUP, name
        # EXECUTE for app_tenant only (and the owner); not PUBLIC.
        grantees = {entry.split("=")[0] for entry in acl.strip("{}").split(",")}
        assert grantees == {APP_TENANT, PIPELINE_LOOKUP}, (name, acl)


@pytest.mark.parametrize("role", [PLATFORM_READER, PLATFORM_WRITER, "audit_writer", "tss"])
def test_no_other_role_may_call_the_lookups(db: TestDb, role: str) -> None:
    with as_role(db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT pipeline.tenant_for_upload('inbox/x/y')")


def test_the_lookup_owner_reads_only_the_columns_the_lookups_need(db: TestDb) -> None:
    expected = {
        "batch_files": {"tenant_id", "s3_key", "status", "registered_at"},
        "batches": {"tenant_id", "status", "opened_at"},
        "documents": {"id", "tenant_id", "status", "queued_at"},
        "document_events": {"tenant_id", "document_id", "step"},
    }
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'assay' "
            "AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped "
            "AND has_column_privilege(%s, c.oid, a.attname, 'SELECT')",
            (PIPELINE_LOOKUP,),
        ).fetchall()
        writes = conn.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'assay' AND (has_table_privilege(%s, c.oid, 'INSERT') "
            "OR has_any_column_privilege(%s, c.oid, 'UPDATE'))",
            (PIPELINE_LOOKUP, PIPELINE_LOOKUP),
        ).fetchone()
    found: dict[str, set[str]] = {}
    for table, column in rows:
        found.setdefault(table, set()).add(column)
    assert found == expected
    assert "file_name" not in found["batch_files"]
    assert writes == (0,)


def test_tenant_for_upload_answers_with_the_tenant_only(db: TestDb) -> None:
    s = db.seed
    _, key = _file(db, s.tenant_a, s.user_a, s.batch_a)
    with as_role(db, APP_TENANT) as conn:  # no tenant set: the lookup is all it can do
        assert conn.execute("SELECT pipeline.tenant_for_upload(%s)", (key,)).fetchone() == (
            s.tenant_a,
        )
        assert conn.execute("SELECT pipeline.tenant_for_upload('inbox/nothing')").fetchone() == (
            None,
        )
        # And without a tenant it still sees no row of its own accord.
        assert conn.execute("SELECT count(*) FROM assay.batch_files").fetchone() == (0,)


def test_batch_files_belong_to_one_tenant(db: TestDb) -> None:
    s = db.seed
    _file(db, s.tenant_b, s.user_b, s.batch_b)
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        names = {r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM assay.batch_files")}
        assert names <= {s.tenant_a}
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("DELETE FROM assay.batch_files")


def test_one_original_per_hash_in_a_tenant_but_never_across_tenants(db: TestDb) -> None:
    """Condition (g) at the database: the index is per tenant."""
    s = db.seed
    sha = "b" * 64
    with psycopg.connect(db.url) as conn:
        for tenant, user, batch in (
            (s.tenant_a, s.user_a, s.batch_a),
            (s.tenant_b, s.user_b, s.batch_b),
        ):
            conn.execute(
                "INSERT INTO assay.documents (tenant_id, batch_id, uploaded_by, file_name, sha256, "
                "status) VALUES (%s, %s, %s, 'x.pdf', %s, 'processing')",
                (tenant, batch, user, sha),
            )
        with pytest.raises(errors.UniqueViolation):
            conn.execute(
                "INSERT INTO assay.documents (tenant_id, batch_id, uploaded_by, file_name, sha256, "
                "status) VALUES (%s, %s, %s, 'y.pdf', %s, 'processing')",
                (s.tenant_a, s.batch_a, s.user_a, sha),
            )


def test_the_master_step_view_has_no_viewer_and_no_detail(db: TestDb) -> None:
    s = db.seed
    with psycopg.connect(db.url) as conn:
        for step, detail in (
            ("scan", '{"result": "NO_THREATS_FOUND", "error": null}'),
            ("viewed", '{"viewer": "someone@example.test", "ip_address": "198.51.100.7"}'),
        ):
            conn.execute(
                "INSERT INTO assay.document_events (tenant_id, document_id, step, status, detail) "
                "VALUES (%s, %s, %s, 'x', %s)",
                (s.tenant_a, s.doc_a, step, detail),
            )
    with as_role(db, PLATFORM_READER) as conn:
        description = conn.execute("SELECT * FROM platform.ingestion_events_v LIMIT 0").description
        columns = [d.name for d in description or []]
        rows = conn.execute(
            "SELECT step FROM platform.ingestion_events_v WHERE document_id = %s", (s.doc_a,)
        ).fetchall()
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM assay.document_events")
    assert columns == [
        "document_id",
        "tenant_id",
        "step",
        "status",
        "at",
        "error_code",
        "error_type",
    ]
    assert ("viewed",) not in rows and ("scan",) in rows


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("AccessDenied", "AccessDenied"),
        ("States.Timeout", "States.Timeout"),
        ("Lambda.TooManyRequestsException", "Lambda.TooManyRequestsException"),
        # Anything that is not a plain name is never shown, whatever wrote it.
        ("Access Denied: s3://bucket/originals/x.pdf", None),
        ("x\nJane Smith", None),
        ("A" * 65, None),
        ("", None),
        (None, None),
    ],
)
def test_the_master_step_view_shows_only_a_plain_error_type(
    db: TestDb, stored: str | None, shown: str | None
) -> None:
    """Migration 0009: one more value from `detail`, and only if it is a name."""
    s = db.seed
    with psycopg.connect(db.url) as conn:
        conn.execute("DELETE FROM assay.document_events WHERE step = 'failed'")
        conn.execute(
            "INSERT INTO assay.document_events (tenant_id, document_id, step, status, detail) "
            "VALUES (%s, %s, 'failed', 'failed', %s)",
            (
                s.tenant_a,
                s.doc_a,
                json.dumps({"error": "fingerprint_failed", "error_type": stored}),
            ),
        )
    with as_role(db, PLATFORM_READER) as conn:
        row = conn.execute(
            "SELECT error_code, error_type FROM platform.ingestion_events_v "
            "WHERE document_id = %s AND step = 'failed'",
            (s.doc_a,),
        ).fetchone()
    assert row == ("fingerprint_failed", shown)


# Everything that says who may read what, except the view's own column list.
PRIVILEGES = {
    "view": "SELECT pg_get_userbyid(c.relowner), c.relacl::text, c.reloptions::text "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'platform' AND c.relname = 'ingestion_events_v'",
    "policies": "SELECT schemaname, tablename, policyname, roles::text, cmd, qual, with_check "
    "FROM pg_policies ORDER BY 1, 2, 3",
    "tables": "SELECT grantee, table_schema, table_name, privilege_type FROM "
    "information_schema.role_table_grants ORDER BY 1, 2, 3, 4",
    "columns": "SELECT grantee, table_schema, table_name, column_name, privilege_type FROM "
    "information_schema.column_privileges ORDER BY 1, 2, 3, 4, 5",
    "members": "SELECT roleid::regrole::text, member::regrole::text, inherit_option, "
    "set_option FROM pg_auth_members ORDER BY 1, 2",
}


def _privileges(name: str) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(_conninfo(name)) as conn:
        return {key: conn.execute(query).fetchall() for key, query in PRIVILEGES.items()}


def test_migration_0009_adds_one_column_and_changes_no_owner_grant_or_policy(db: TestDb) -> None:
    """Up and down: the view keeps its owner, options and grants; the only new column
    privileges are on `error_type` of that view."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name, "0008")
        before = _privileges(name)
        assert before["view"][0][0] == "platform_view_owner"
        _migrate(name, "0009")
        after = _privileges(name)
        assert {k: v for k, v in after.items() if k != "columns"} == {
            k: v for k, v in before.items() if k != "columns"
        }
        added = set(after["columns"]) - set(before["columns"])
        assert set(before["columns"]) <= set(after["columns"])
        assert added and all(
            row[1:4] == ("platform", "ingestion_events_v", "error_type")
            and row[0] in {"platform_reader", "platform_view_owner"}
            for row in added
        ), added
        _migrate(name, "0008", down=True)
        assert _privileges(name) == before
    finally:
        _drop_database(name)


def test_discarded_is_a_document_status(db: TestDb) -> None:
    s = db.seed
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "INSERT INTO assay.documents (tenant_id, batch_id, uploaded_by, file_name, status) "
            "VALUES (%s, %s, %s, 'z.pdf', 'discarded')",
            (s.tenant_a, s.batch_a, s.user_a),
        )
