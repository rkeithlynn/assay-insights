"""Migration 0012 (build step 6, second half, stage 2): what the worker may write for step 5,
tried as each role against a real PostgreSQL. Never only as a superuser.

- `app_worker`, bound to one attempt of one document, may insert that attempt's `pages` and
  `ai_usage` rows and nothing else's, and can read neither table.
- It may set the document's content pointer for its bound attempt only, and can update no
  other column of `documents` beyond the six it already had.
- No master role sees `pages` or `ai_usage`; the master step view gains a text event's page
  counts and nothing from the text.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from assay_db.roles import APP_TENANT, APP_WORKER, PLATFORM_READER, PLATFORM_WRITER
from tests.unit.pgtest import TestDb, _conninfo, as_role, requires_db
from tests.unit.test_db_worker import _admin, _bind, _claim, _document, worker

pytestmark = requires_db


@pytest.fixture
def docs(db: TestDb) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two processing documents in tenant A and one in tenant B, as in test_db_worker.
    Earlier tests' grants end, so each test starts with every worker slot free."""
    _admin(db, "UPDATE assay.worker_grants SET ended_at = now() WHERE ended_at IS NULL")
    s = db.seed
    return (
        _document(db, s.tenant_a, s.user_a, s.batch_a),
        _document(db, s.tenant_a, s.user_a, s.batch_a, companion=False),
        _document(db, s.tenant_b, s.user_b, s.batch_b),
    )


ROW_SECURITY = "row-level security"
PAGE = (
    "INSERT INTO assay.pages (tenant_id, document_id, attempt, page_no, text_source, "
    "quality_score, ocr_confidence, char_count) VALUES (%s, %s, %s, %s, 'native', 0.97, NULL, 12)"
)
USAGE = (
    "INSERT INTO assay.ai_usage (tenant_id, job, service, operation, model_version, "
    "document_id, attempt, page_no, input_tokens, output_tokens, units, unit, unit_price, "
    "cost, request_id) VALUES (%s, 'ingest', 'textract', 'DetectDocumentText', '1.0', %s, %s, "
    "%s, 0, 0, 1, 'page', 0.0015, 0.0015, 'req-1')"
)
# Every column of `documents` the worker may update: six since migration 0010, two since 0012.
WORKER_UPDATES = {
    "status",
    "error_code",
    "s3_key",
    "size_bytes",
    "page_count",
    "completed_at",
    "content_attempt",
    "text_version_id",
}


def bound(db: TestDb, conn: psycopg.Connection, document: uuid.UUID, tenant: uuid.UUID) -> None:
    token = _claim(db, tenant, document)
    assert token
    _bind(conn, document, 1, token)


def count(db: TestDb, table: str, document: uuid.UUID) -> int:
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM assay.{table} WHERE document_id = %s",  # nosec B608
            (document,),
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- pages and ai_usage: insert-only, for the bound attempt -------------------------------------


def test_the_worker_writes_its_own_attempts_pages_and_usage(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        conn.execute(PAGE, (s.tenant_a, a1, 1, 1))
        conn.execute(PAGE, (s.tenant_a, a1, 1, 2))
        conn.execute(USAGE, (s.tenant_a, a1, 1, 2))
        # A repeat is dropped, with no conflict target (naming one needs SELECT).
        conn.execute(PAGE + " ON CONFLICT DO NOTHING", (s.tenant_a, a1, 1, 2))
    assert count(db, "pages", a1) == 2
    assert count(db, "ai_usage", a1) == 1


@pytest.mark.parametrize("statement", [PAGE, USAGE], ids=["pages", "ai_usage"])
def test_the_worker_writes_no_row_for_anything_but_its_bound_attempt(
    db: TestDb, docs: tuple[uuid.UUID, ...], statement: str
) -> None:
    """Another document of its tenant, another tenant's document, another tenant's ID on its
    own document, and another attempt of its own document: each refused by row security."""
    a1, a2, b1 = docs
    s = db.seed
    table = "pages" if statement is PAGE else "ai_usage"
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        for name, values in (
            ("a sibling document", (s.tenant_a, a2, 1, 1)),
            ("another tenant's document", (s.tenant_b, b1, 1, 1)),
            ("another tenant's ID", (s.tenant_b, a1, 1, 1)),
            ("a later attempt", (s.tenant_a, a1, 2, 1)),
            ("an attempt that cannot be", (s.tenant_a, a1, 7, 1)),
        ):
            with pytest.raises(errors.InsufficientPrivilege, match=ROW_SECURITY):
                conn.execute(statement, values)
            assert name
    for document in (a1, a2, b1):
        assert count(db, table, document) == 0


@pytest.mark.parametrize("statement", [PAGE, USAGE], ids=["pages", "ai_usage"])
def test_a_session_with_no_live_grant_writes_nothing(
    db: TestDb, docs: tuple[uuid.UUID, ...], statement: str
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        # Never bound.
        with pytest.raises(errors.InsufficientPrivilege, match=ROW_SECURITY):
            conn.execute(statement, (s.tenant_a, a1, 1, 1))
        bound(db, conn, a1, s.tenant_a)
        _admin(db, "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s", a1)
        # Bound, and the grant ended while it was connected.
        with pytest.raises(errors.InsufficientPrivilege, match=ROW_SECURITY):
            conn.execute(statement, (s.tenant_a, a1, 1, 1))


def test_a_superseded_attempt_writes_nothing(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    """ "Process again" moved the document to attempt 2: attempt 1's session is bound to
    nothing, whatever it still holds."""
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        _admin(db, "UPDATE assay.documents SET attempt = 2 WHERE id = %s", a1)
        assert conn.execute("SELECT pipeline.bound_attempt()").fetchone() == (None,)
        for statement in (PAGE, USAGE):
            with pytest.raises(errors.InsufficientPrivilege, match=ROW_SECURITY):
                conn.execute(statement, (s.tenant_a, a1, 1, 1))
        changed = conn.execute(
            "UPDATE assay.documents SET content_attempt = 1, text_version_id = 'v' "
            "WHERE id = %s RETURNING id",
            (a1,),
        ).fetchall()
        assert changed == []


@pytest.mark.parametrize("table", ["pages", "ai_usage"])
def test_the_worker_can_read_change_or_remove_no_row_of_either_table(
    db: TestDb, docs: tuple[uuid.UUID, ...], table: str
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        conn.execute(PAGE if table == "pages" else USAGE, (s.tenant_a, a1, 1, 1))
        for statement in (
            f"SELECT * FROM assay.{table}",
            f"SELECT count(*) FROM assay.{table}",
            f"UPDATE assay.{table} SET attempt = 1",
            f"DELETE FROM assay.{table}",
            f"TRUNCATE assay.{table}",
        ):
            with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
                conn.execute(statement)


def test_the_worker_cannot_write_a_model_call_or_a_chat_call(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    """Its columns are a Textract call's. A row for a catalog model or a chat needs columns
    it was not given."""
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        for column in ("model_id", "chat_audit_id", "id"):
            with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
                conn.execute(
                    f"INSERT INTO assay.ai_usage (tenant_id, job, document_id, input_tokens, "  # nosec B608
                    f"output_tokens, cost, {column}) VALUES (%s, 'ingest', %s, 1, 1, 0, %s)",
                    (s.tenant_a, a1, uuid.uuid4()),
                )
        with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
            conn.execute(
                "INSERT INTO assay.pages (tenant_id, document_id, attempt, page_no, text_source, "
                "search_tsv) VALUES (%s, %s, 1, 1, 'native', to_tsvector('secret'))",
                (s.tenant_a, a1),
            )


# --- The content pointer -------------------------------------------------------------------------


def pointer(db: TestDb, document: uuid.UUID) -> tuple[Any, ...]:
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            "SELECT content_attempt, text_version_id FROM assay.documents WHERE id = %s",
            (document,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_the_worker_sets_the_content_pointer_for_its_bound_attempt_only(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        for attempt, version in ((2, "v"), (0, "v"), (7, "v"), (None, "v"), (1, ""), (1, None)):
            with pytest.raises(errors.InsufficientPrivilege, match="bound attempt"):
                conn.execute(
                    "UPDATE assay.documents SET content_attempt = %s, text_version_id = %s "
                    "WHERE id = %s",
                    (attempt, version, a1),
                )
        # Naming only one of the two columns does not get round it.
        with pytest.raises(errors.InsufficientPrivilege, match="bound attempt"):
            conn.execute("UPDATE assay.documents SET content_attempt = 2 WHERE id = %s", (a1,))
        with pytest.raises(errors.InsufficientPrivilege, match="bound attempt"):
            conn.execute("UPDATE assay.documents SET text_version_id = 'x' WHERE id = %s", (a1,))
        assert pointer(db, a1) == (None, None)
        conn.execute(
            "UPDATE assay.documents SET content_attempt = 1, text_version_id = 'version-1' "
            "WHERE id = %s",
            (a1,),
        )
    assert pointer(db, a1) == (1, "version-1")


def test_a_second_attempt_cannot_point_back_at_the_first(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    """The document holds attempt 1's text and is being processed again. Attempt 2's worker
    may leave the pointer alone, or move it to 2; it cannot rewrite attempt 1's."""
    a1, _a2, _b1 = docs
    s = db.seed
    _admin(
        db,
        "UPDATE assay.documents SET attempt = 2, content_attempt = 1, text_version_id = 'old' "
        "WHERE id = %s",
        a1,
    )
    token = _claim(db, s.tenant_a, a1, attempt=2)
    assert token
    with worker(db) as conn:
        _bind(conn, a1, 2, token)
        # An update that does not name the pointer is not the guard's business.
        conn.execute("UPDATE assay.documents SET page_count = 4 WHERE id = %s", (a1,))
        assert pointer(db, a1) == (1, "old")
        with pytest.raises(errors.InsufficientPrivilege, match="bound attempt"):
            conn.execute(
                "UPDATE assay.documents SET content_attempt = 1, text_version_id = 'forged' "
                "WHERE id = %s",
                (a1,),
            )
        conn.execute(
            "UPDATE assay.documents SET content_attempt = 2, text_version_id = 'new' WHERE id = %s",
            (a1,),
        )
    assert pointer(db, a1) == (2, "new")


def test_the_worker_cannot_move_another_documents_pointer(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, a2, b1 = docs
    s = db.seed
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        for other in (a2, b1):
            changed = conn.execute(
                "UPDATE assay.documents SET content_attempt = 1, text_version_id = 'v' "
                "WHERE id = %s RETURNING id",
                (other,),
            ).fetchall()
            assert changed == []  # row security shows it no such row
            assert pointer(db, other) == (None, None)


def test_the_worker_can_update_no_other_column_of_documents(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    """Every column of the table, from the catalog, so a new column is covered when it is
    added: each is either one of the eight or refused."""
    a1, _a2, _b1 = docs
    s = db.seed
    with psycopg.connect(db.url) as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'assay' AND table_name = 'documents' ORDER BY 1"
            ).fetchall()
        ]
    assert set(columns) > WORKER_UPDATES
    refused = []
    with worker(db) as conn:
        bound(db, conn, a1, s.tenant_a)
        for column in columns:
            if column in WORKER_UPDATES:
                continue
            with pytest.raises(errors.InsufficientPrivilege, match="permission denied"):
                conn.execute(
                    f"UPDATE assay.documents SET {column} = {column} WHERE id = %s",  # nosec B608
                    (a1,),
                )
            refused.append(column)
    assert {"file_name", "tenant_id", "attempt", "companion_attempt", "batch_id"} <= set(refused)
    assert len(refused) == len(columns) - len(WORKER_UPDATES)


def test_the_guard_holds_only_the_worker(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    """`app_tenant` is one tenant's whole pipeline: it is not held to a worker's attempt."""
    a1, _a2, _b1 = docs
    s = db.seed
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        conn.execute(
            "UPDATE assay.documents SET content_attempt = 3, text_version_id = NULL WHERE id = %s",
            (a1,),
        )
        conn.commit()
    assert pointer(db, a1) == (3, None)


def test_the_bound_attempt_is_the_live_grants_and_nothing_without_one(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with worker(db) as conn:
        assert conn.execute("SELECT pipeline.bound_attempt()").fetchone() == (None,)
        bound(db, conn, a1, s.tenant_a)
        assert conn.execute("SELECT pipeline.bound_attempt()").fetchone() == (1,)
    for role in (APP_TENANT, PLATFORM_READER, PLATFORM_WRITER, "tss", "audit_writer"):
        with as_role(db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT pipeline.bound_attempt()")


# --- ai_usage: one shape for a model call, another for a page -------------------------------


def test_a_usage_row_is_a_model_call_or_a_textract_call_never_a_mix(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with psycopg.connect(db.url) as conn:
        model = conn.execute(
            "INSERT INTO assay.ai_models (name, profile_id, jobs, price_in, price_out, status) "
            "VALUES ('M', %s, '{ingest}', 1, 1, 'active') RETURNING id",
            (f"us.test.{uuid.uuid4().hex}",),
        ).fetchone()
    assert model is not None
    base = (
        "INSERT INTO assay.ai_usage (tenant_id, job, document_id, input_tokens, output_tokens, cost"
    )
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        # A model call, as before this migration: `service` defaults to bedrock.
        conn.execute(
            base + ", model_id) VALUES (%s, 'ingest', %s, 5, 7, 0.01, %s)",
            (
                s.tenant_a,
                a1,
                model[0],
            ),
        )
        conn.execute(USAGE, (s.tenant_a, a1, 1, 1))
        conn.commit()
        # The tenant setting is the transaction's: each new one sets it again.
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(s.tenant_a),))
        for name, statement, values in (
            (
                "a model call with no model",
                base + ") VALUES (%s, 'ingest', %s, 5, 7, 0.01)",
                (s.tenant_a, a1),
            ),
            (
                "a Textract call that names a model",
                base + ", service, operation, units, unit, unit_price, model_id) VALUES "
                "(%s, 'ingest', %s, 0, 0, 0.0015, 'textract', 'DetectDocumentText', 1, 'page', "
                "0.0015, %s)",
                (s.tenant_a, a1, model[0]),
            ),
            (
                "a Textract call with nothing it was billed by",
                base + ", service, operation) VALUES (%s, 'ingest', %s, 0, 0, 0.0015, "
                "'textract', 'DetectDocumentText')",
                (s.tenant_a, a1),
            ),
            (
                "a service that is not one",
                base + ", service, model_id) VALUES (%s, 'ingest', %s, 0, 0, 0, 'other', %s)",
                (s.tenant_a, a1, model[0]),
            ),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(statement, values)
            conn.rollback()
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(s.tenant_a),))
            assert name
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT service, model_id IS NULL, units, unit, cost::text FROM assay.ai_usage "
            "WHERE document_id = %s ORDER BY service",
            (a1,),
        ).fetchall()
    assert rows == [
        ("bedrock", False, None, None, "0.010000"),
        ("textract", True, 1, "page", "0.001500"),
    ]


def test_a_page_is_stored_once_per_attempt(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    a1, _a2, _b1 = docs
    s = db.seed
    with as_role(db, APP_TENANT, s.tenant_a) as conn:
        conn.execute(PAGE, (s.tenant_a, a1, 1, 1))
        conn.execute(PAGE, (s.tenant_a, a1, 2, 1))  # the same page, read again: both are kept
        conn.commit()
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(s.tenant_a),))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(PAGE, (s.tenant_a, a1, 2, 1))
    assert count(db, "pages", a1) == 2


# --- Master roles ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [PLATFORM_READER, PLATFORM_WRITER, "audit_writer", "tss"])
@pytest.mark.parametrize("table", ["pages", "ai_usage"])
def test_no_master_or_service_role_reads_pages_or_usage(db: TestDb, role: str, table: str) -> None:
    with as_role(db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(f"SELECT * FROM assay.{table}")  # nosec B608


def test_no_platform_view_reads_pages_or_usage(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT viewname, definition FROM pg_views WHERE schemaname = 'platform'"
        ).fetchall()
        owner = conn.execute(
            "SELECT table_name, privilege_type FROM information_schema.column_privileges "
            "WHERE grantee = 'platform_view_owner' AND table_name IN ('pages', 'ai_usage')"
        ).fetchall()
    assert rows
    for name, definition in rows:
        assert "assay.pages" not in definition and "ai_usage" not in definition, name
    assert owner == []


def test_the_master_step_view_shows_a_text_events_counts_and_nothing_else_of_it(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    s = db.seed
    document = docs[2]
    events = (
        # A text event as the worker writes it.
        (
            "text",
            '{"pages": 12, "pages_native": 9, "pages_ocr": 2, "pages_unread": 1, '
            '"threshold": 0.9, "version_id": "abc123"}',
        ),
        # Another step's `pages` is not a text line's count.
        ("inspect", '{"pages": 12, "companion": false}'),
        # Whatever wrote these, none is a plain whole number.
        (
            "text",
            '{"pages": "twelve pages of secret text", "pages_native": -1, '
            '"pages_ocr": 1.5, "pages_unread": "99999999"}',
        ),
    )
    with psycopg.connect(db.url) as conn:
        for step, detail in events:
            conn.execute(
                "INSERT INTO assay.document_events (tenant_id, document_id, step, status, detail) "
                "VALUES (%s, %s, %s, 'continue', %s)",
                (s.tenant_b, document, step, detail),
            )
    with as_role(db, PLATFORM_READER) as conn:
        rows = conn.execute(
            "SELECT step, pages, pages_native, pages_ocr, pages_unread "
            "FROM platform.ingestion_events_v WHERE document_id = %s ORDER BY at",
            (document,),
        ).fetchall()
        everything = conn.execute(
            "SELECT * FROM platform.ingestion_events_v WHERE document_id = %s", (document,)
        ).fetchall()
    assert rows == [
        ("text", 12, 9, 2, 1),
        ("inspect", None, None, None, None),
        ("text", None, None, None, None),
    ]
    shown = " ".join(str(value) for row in everything for value in row)
    assert "abc123" not in shown and "secret" not in shown and "0.9" not in shown


# --- The migration itself -------------------------------------------------------------------------


def test_migration_0012_goes_down_and_up_again_cleanly() -> None:
    from tests.unit.pgtest import _bootstrap_as_master, _create_database, _drop_database, _migrate

    def state(name: str) -> dict[str, list[tuple[object, ...]]]:
        queries = {
            "functions": "SELECT n.nspname, p.proname FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname IN ('pipeline', 'assay') ORDER BY 1, 2",
            "triggers": "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1",
            "policies": "SELECT tablename, policyname, cmd FROM pg_policies "
            "WHERE schemaname = 'assay' ORDER BY 1, 2",
            "columns": "SELECT grantee, table_name, column_name, privilege_type "
            "FROM information_schema.column_privileges WHERE table_schema IN ('assay', "
            "'platform') AND grantee IN ('app_worker', 'pipeline_lookup', 'platform_reader', "
            "'platform_view_owner') ORDER BY 1, 2, 3, 4",
            "shape": "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'assay' AND table_name IN ('pages', 'ai_usage', 'documents') "
            "ORDER BY 1, 2",
            "constraints": "SELECT conname FROM pg_constraint c JOIN pg_namespace n "
            "ON n.oid = c.connamespace WHERE n.nspname = 'assay' ORDER BY 1",
            "view": "SELECT column_name FROM information_schema.columns WHERE table_schema = "
            "'platform' AND table_name = 'ingestion_events_v' ORDER BY ordinal_position",
            "view_owner": "SELECT viewowner FROM pg_views WHERE schemaname = 'platform' "
            "AND viewname = 'ingestion_events_v'",
        }
        with psycopg.connect(_conninfo(name)) as conn:
            return {key: conn.execute(query).fetchall() for key, query in queries.items()}

    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name, "0011")
        before = state(name)
        assert ("pipeline", "bound_attempt") not in before["functions"]
        assert before["triggers"] == []

        _migrate(name, "0012")
        after = state(name)
        assert ("pipeline", "bound_attempt") in after["functions"]
        assert after["triggers"] == [("worker_content_pointer_guard",)]
        assert set(after["policies"]) - set(before["policies"]) == {
            ("ai_usage", "app_worker_bound_insert", "INSERT"),
            ("pages", "app_worker_bound_insert", "INSERT"),
        }
        gained = set(after["columns"]) - set(before["columns"])
        # The view's owner holds its own view's columns, as it does every column of it.
        assert {row[0] for row in gained} == {APP_WORKER, PLATFORM_READER, "platform_view_owner"}
        assert {row[1] for row in gained if row[0] == "platform_view_owner"} == {
            "ingestion_events_v"
        }
        assert {row[1:] for row in gained if row[0] == PLATFORM_READER} == {
            ("ingestion_events_v", column, "SELECT")
            for column in ("pages", "pages_native", "pages_ocr", "pages_unread")
        }
        assert after["view_owner"] == before["view_owner"] == [("platform_view_owner",)]
        assert [row[0] for row in after["view"]][-4:] == [
            "pages",
            "pages_native",
            "pages_ocr",
            "pages_unread",
        ]

        _migrate(name, "0011", down=True)
        assert state(name) == before
        _migrate(name, "0012")
        assert state(name) == after
    finally:
        _drop_database(name)
