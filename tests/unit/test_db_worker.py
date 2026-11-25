"""Migration 0010: the worker's attempt grant and the bound role `app_worker`.

Against a real PostgreSQL 17, signed in as each role itself (never as a superuser, except to
seed rows and read catalogs).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import errors
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from assay_db import grants
from assay_db.roles import APP_TENANT, APP_WORKER, PLATFORM_READER, PLATFORM_WRITER, TSS
from tests.unit.pgtest import TestDb, _conninfo, as_role, requires_db

pytestmark = requires_db

TTL = dt.timedelta(minutes=30)


def _document(
    db: TestDb,
    tenant: uuid.UUID,
    user: uuid.UUID,
    batch: uuid.UUID,
    *,
    status: str = "processing",
    companion: bool = True,
) -> uuid.UUID:
    """A document with a clean PDF, optionally a clean companion, and one removed file."""
    document = uuid.uuid4()
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "INSERT INTO assay.documents (id, tenant_id, batch_id, uploaded_by, file_name, status) "
            "VALUES (%s, %s, %s, %s, 'secret-name.pdf', %s)",
            (document, tenant, batch, user, status),
        )
        files = [(document, "pdf", "clean")]
        if companion:
            files.append((uuid.uuid4(), "companion", "clean"))
        files.append((uuid.uuid4(), "companion", "removed"))
        for file_id, kind, file_status in files:
            conn.execute(
                "INSERT INTO assay.batch_files (id, tenant_id, batch_id, kind, file_name, "
                "base_name, size_bytes, sha256, s3_key, content_type, status, version_id, "
                "document_id, registered_by) VALUES (%s, %s, %s, %s, 'secret-name', %s, 10, %s, "
                "%s, 'application/pdf', %s, 'v1', %s, %s)",
                (
                    file_id,
                    tenant,
                    batch,
                    kind,
                    f"secret-{file_id}",
                    "a" * 64,
                    f"inbox/{batch}/{file_id}",
                    file_status,
                    document,
                    user,
                ),
            )
    return document


def _claim(
    db: TestDb, tenant: uuid.UUID, document: uuid.UUID, *, attempt: int = 1, cap: int = 50
) -> str | None:
    """As the pipeline's claim step does: app_tenant, in the document's tenant."""
    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(_conninfo(db.name, APP_TENANT)),
        poolclass=NullPool,
    )
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
            return grants.claim(
                conn,
                tenant_id=tenant,
                document_id=document,
                attempt=attempt,
                max_per_tenant=cap,
                ttl=TTL,
            )
    finally:
        engine.dispose()


def _admin(db: TestDb, statement: str, *params: object) -> None:
    with psycopg.connect(db.url) as conn:
        conn.execute(statement, params)


@contextmanager
def worker(db: TestDb) -> Iterator[psycopg.Connection]:
    """A session signed in as app_worker, autocommit: a binding lasts for the session."""
    with psycopg.connect(_conninfo(db.name, APP_WORKER), autocommit=True) as conn:
        yield conn


def _bind(conn: psycopg.Connection, document: uuid.UUID, attempt: int, token: str) -> uuid.UUID:
    row = conn.execute("SELECT pipeline.bind_worker(%s, %s, %s)", (document, attempt, token))
    value = row.fetchone()
    assert value is not None
    return uuid.UUID(str(value[0]))


def _ids(conn: psycopg.Connection, table: str, column: str = "id") -> set[uuid.UUID]:
    return {r[0] for r in conn.execute(f"SELECT {column} FROM assay.{table}").fetchall()}


@pytest.fixture
def docs(db: TestDb) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two processing documents in tenant A and one in tenant B. Earlier tests' grants end,
    so each test starts with every worker slot free."""
    _admin(db, "UPDATE assay.worker_grants SET ended_at = now() WHERE ended_at IS NULL")
    s = db.seed
    return (
        _document(db, s.tenant_a, s.user_a, s.batch_a),
        _document(db, s.tenant_a, s.user_a, s.batch_a, companion=False),
        _document(db, s.tenant_b, s.user_b, s.batch_b),
    )


# --- The grant ---------------------------------------------------------------------------


def test_a_grant_keeps_only_the_tokens_hash(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    token = _claim(db, db.seed.tenant_a, docs[0])
    assert token and ":" not in token
    with psycopg.connect(db.url) as conn:
        rows = conn.execute(
            "SELECT * FROM assay.worker_grants WHERE document_id = %s", (docs[0],)
        ).fetchall()
    assert len(rows) == 1
    assert token not in " ".join(str(value) for value in rows[0])
    assert grants.token_hash(token) in rows[0]


def test_the_cap_counts_live_grants_in_the_tenant_only(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, a2, b1 = docs
    s = db.seed
    assert _claim(db, s.tenant_a, a1, cap=1) is not None
    assert _claim(db, s.tenant_a, a2, cap=1) is None  # the tenant's one slot is taken
    assert _claim(db, s.tenant_b, b1, cap=1) is not None  # another tenant's slots are its own
    _admin(db, "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s", a1)
    assert _claim(db, s.tenant_a, a2, cap=1) is not None  # an ended grant frees its slot
    _admin(
        db,
        "UPDATE assay.worker_grants SET expires_at = now() - interval '1 second' "
        "WHERE document_id = %s",
        a2,
    )
    assert _claim(db, s.tenant_a, a1, cap=1) is not None  # so does an expired one


def test_a_repeated_claim_leaves_one_live_grant_and_kills_the_first_token(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    first = _claim(db, db.seed.tenant_a, docs[0])
    second = _claim(db, db.seed.tenant_a, docs[0])
    assert first and second and first != second
    with worker(db) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            _bind(conn, docs[0], 1, first)
        assert _bind(conn, docs[0], 1, second) == db.seed.tenant_a


# --- The TSS's lookup ----------------------------------------------------------------------


def _worker_grant(
    db: TestDb, document: uuid.UUID, attempt: int, token: str
) -> list[tuple[uuid.UUID, list[str]]]:
    with as_role(db, TSS) as conn:
        return conn.execute(
            "SELECT tenant_id, inbox_keys FROM pipeline.worker_grant(%s, %s, %s)",
            (document, attempt, grants.token_hash(token)),
        ).fetchall()


def test_the_tss_learns_the_tenant_and_the_documents_clean_inbox_keys(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1 = docs[0]
    token = _claim(db, db.seed.tenant_a, a1)
    assert token
    rows = _worker_grant(db, a1, 1, token)
    assert len(rows) == 1
    tenant, keys = rows[0]
    assert tenant == db.seed.tenant_a
    # The PDF and the clean companion; never the removed file, never a file name.
    assert len(keys) == 2 and f"inbox/{db.seed.batch_a}/{a1}" in keys
    assert all(key.startswith(f"inbox/{db.seed.batch_a}/") for key in keys)


def test_the_tss_gets_nothing_for_a_grant_that_is_not_live(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, a2, b1 = docs
    s = db.seed
    token = _claim(db, s.tenant_a, a1)
    other = _claim(db, s.tenant_b, b1)
    assert token and other
    assert _worker_grant(db, a1, 1, token)
    assert _worker_grant(db, a1, 1, token + "x") == []  # wrong token
    assert _worker_grant(db, a1, 2, token) == []  # wrong attempt
    assert _worker_grant(db, a2, 1, token) == []  # another document of the same tenant
    assert _worker_grant(db, b1, 1, token) == []  # another tenant's document
    assert _worker_grant(db, a1, 1, other) == []  # another tenant's token

    # The document moved on to a later attempt: the grant names an attempt that is not current.
    _admin(db, "UPDATE assay.documents SET attempt = 2 WHERE id = %s", a1)
    assert _worker_grant(db, a1, 1, token) == []
    _admin(db, "UPDATE assay.documents SET attempt = 1 WHERE id = %s", a1)
    assert _worker_grant(db, a1, 1, token)

    # The document is no longer processing.
    _admin(db, "UPDATE assay.documents SET status = 'on_hold' WHERE id = %s", a1)
    assert _worker_grant(db, a1, 1, token) == []
    _admin(db, "UPDATE assay.documents SET status = 'processing' WHERE id = %s", a1)

    _admin(
        db,
        "UPDATE assay.worker_grants SET expires_at = now() - interval '1 second' "
        "WHERE document_id = %s",
        a1,
    )
    assert _worker_grant(db, a1, 1, token) == []  # expired
    _admin(db, "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s", b1)
    assert _worker_grant(db, b1, 1, other) == []  # ended


def test_the_tss_reads_no_grant_table_and_no_one_else_calls_its_lookup(db: TestDb) -> None:
    with as_role(db, TSS) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM assay.worker_grants")
    with as_role(db, TSS) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT pipeline.bind_worker(%s, 1, 'x')", (uuid.uuid4(),))
    for role in (APP_TENANT, APP_WORKER, PLATFORM_READER, PLATFORM_WRITER, "audit_writer"):
        with as_role(db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM pipeline.worker_grant(%s, 1, %s)", (uuid.uuid4(), "a" * 64))


@pytest.mark.parametrize(
    "role", [APP_TENANT, TSS, PLATFORM_READER, PLATFORM_WRITER, "audit_writer"]
)
def test_only_app_worker_may_bind(db: TestDb, role: str) -> None:
    for call in ("pipeline.bound_tenant()", "pipeline.bound_document()"):
        with as_role(db, role) as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f"SELECT {call}")


# --- app_worker: unbound -------------------------------------------------------------------


def test_an_unbound_worker_session_sees_no_rows(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    with worker(db) as conn:
        assert conn.execute("SELECT pipeline.bound_tenant()").fetchone() == (None,)
        assert conn.execute("SELECT pipeline.bound_document()").fetchone() == (None,)
        for table, column in (
            ("documents", "id"),
            ("batch_files", "id"),
            ("document_events", "document_id"),
        ):
            assert _ids(conn, table, column) == set(), table


def test_a_worker_cannot_name_its_tenant(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    """app_tenant's setting means nothing to app_worker, and a made-up grant binds nothing."""
    s = db.seed
    with worker(db) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(s.tenant_a),))
        assert _ids(conn, "documents") == set()
        for forged in (
            f"{docs[0]}:1:guess",
            f"{docs[0]}:1:",
            f"{docs[0]}",
            "",
            "not-a-uuid:x:y",
            f"{s.tenant_a}:1:{'a' * 64}",
        ):
            conn.execute("SELECT set_config('app.worker_grant', %s, false)", (forged,))
            assert conn.execute("SELECT pipeline.bound_tenant()").fetchone() == (None,), forged
            assert _ids(conn, "documents") == set(), forged


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM assay.users",
        "SELECT count(*) FROM assay.tenants",
        "SELECT count(*) FROM assay.audit_log",
        "SELECT count(*) FROM assay.chat_conversations",
        "SELECT count(*) FROM assay.chat_audit",
        "SELECT count(*) FROM assay.batches",
        "SELECT count(*) FROM assay.tenant_storage",
        "SELECT count(*) FROM assay.worker_grants",
        "SELECT file_name FROM assay.documents",
        "SELECT file_name FROM assay.batch_files",
        "SELECT detail FROM assay.document_events",
        "SELECT * FROM assay.documents",
        "INSERT INTO assay.audit_log (tenant_id, category, action, outcome) "
        "VALUES (gen_random_uuid(), 'tenant', 'created', 'success')",
        "UPDATE assay.documents SET file_name = 'x'",
        "UPDATE assay.documents SET attempt = 2",
        "UPDATE assay.documents SET sha256 = NULL",
        "UPDATE assay.batch_files SET status = 'clean'",
        "DELETE FROM assay.documents",
        "DELETE FROM assay.document_events",
        "SELECT pipeline.tenant_for_upload('inbox/x/y')",
    ],
)
def test_a_worker_bound_or_not_has_no_other_table(
    db: TestDb, docs: tuple[uuid.UUID, ...], statement: str
) -> None:
    token = _claim(db, db.seed.tenant_a, docs[0])
    assert token
    for bind in (False, True):
        with worker(db) as conn:
            if bind:
                _bind(conn, docs[0], 1, token)
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


# --- app_worker: bound ----------------------------------------------------------------------


def test_a_bound_session_sees_its_document_and_nothing_else(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1, a2, b1 = docs
    s = db.seed
    for document in (a1, a2, b1):
        _admin(
            db,
            "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
            "SELECT tenant_id, id, 'start', 'started' FROM assay.documents WHERE id = %s",
            document,
        )
    token = _claim(db, s.tenant_a, a1)
    assert token
    with worker(db) as conn:
        assert _bind(conn, a1, 1, token) == s.tenant_a
        assert conn.execute("SELECT pipeline.bound_document()").fetchone() == (a1,)
        # Not the tenant's other document, and nothing of the other tenant.
        assert _ids(conn, "documents") == {a1}
        assert _ids(conn, "batch_files", "document_id") == {a1}
        assert _ids(conn, "document_events", "document_id") == {a1}

        # Its own document: an event and the columns it may change.
        conn.execute(
            "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
            "VALUES (%s, %s, 'inspect', 'continue')",
            (s.tenant_a, a1),
        )
        changed = conn.execute(
            "UPDATE assay.documents SET page_count = 3, size_bytes = 10 WHERE id = %s RETURNING id",
            (a1,),
        ).fetchall()
        assert changed == [(a1,)]

        # Any other document: no event, no change, whatever tenant is written in the row.
        for tenant, document in ((s.tenant_a, a2), (s.tenant_b, b1), (s.tenant_b, a1)):
            with pytest.raises((errors.InsufficientPrivilege, errors.ForeignKeyViolation)):
                conn.execute(
                    "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
                    "VALUES (%s, %s, 'inspect', 'continue')",
                    (tenant, document),
                )
            assert conn.execute(
                "UPDATE assay.documents SET page_count = 9 WHERE id = %s RETURNING id",
                (document,),
            ).fetchall() == ([(a1,)] if document == a1 else [])
    with psycopg.connect(db.url) as conn:
        pages: dict[uuid.UUID, int | None] = dict(
            conn.execute(
                "SELECT id, page_count FROM assay.documents WHERE id = ANY(%s)", ([a1, a2, b1],)
            ).fetchall()
        )
    assert pages[a2] is None and pages[b1] is None


def test_a_wrong_token_or_attempt_does_not_bind(db: TestDb, docs: tuple[uuid.UUID, ...]) -> None:
    a1, a2, b1 = docs
    token = _claim(db, db.seed.tenant_a, a1)
    assert token
    with worker(db) as conn:
        for document, attempt, presented in (
            (a1, 1, token + "x"),
            (a1, 2, token),
            (a2, 1, token),
            (b1, 1, token),
            (a1, 1, f"{token}:{token}"),
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                _bind(conn, document, attempt, presented)
            assert conn.execute("SELECT pipeline.bound_tenant()").fetchone() == (None,)
            assert _ids(conn, "documents") == set()


def test_a_session_whose_grant_ended_expired_or_was_superseded_sees_nothing(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    a1 = docs[0]
    for stop, restore in (
        (
            "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s",
            None,
        ),
        (
            "UPDATE assay.worker_grants SET expires_at = now() - interval '1 second' "
            "WHERE document_id = %s AND ended_at IS NULL",
            None,
        ),
        (
            "UPDATE assay.documents SET attempt = 2 WHERE id = %s",
            "UPDATE assay.documents SET attempt = 1 WHERE id = %s",
        ),
    ):
        token = _claim(db, db.seed.tenant_a, a1)
        assert token
        with worker(db) as conn:
            _bind(conn, a1, 1, token)
            assert _ids(conn, "documents") == {a1}
            _admin(db, stop, a1)
            # The same session, still holding its token: nothing, and no write lands.
            assert conn.execute("SELECT pipeline.bound_tenant()").fetchone() == (None,)
            assert _ids(conn, "documents") == set()
            assert _ids(conn, "batch_files") == set()
            assert (
                conn.execute(
                    "UPDATE assay.documents SET page_count = 7 WHERE id = %s RETURNING id", (a1,)
                ).fetchall()
                == []
            )
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
                    "VALUES (%s, %s, 'inspect', 'continue')",
                    (db.seed.tenant_a, a1),
                )
        if restore:
            _admin(db, restore, a1)


def test_a_binding_does_not_outlive_its_session_or_reach_another(
    db: TestDb, docs: tuple[uuid.UUID, ...]
) -> None:
    token = _claim(db, db.seed.tenant_a, docs[0])
    assert token
    with worker(db) as first, worker(db) as second:
        _bind(first, docs[0], 1, token)
        assert _ids(first, "documents") == {docs[0]}
        assert _ids(second, "documents") == set()
    with worker(db) as later:
        assert _ids(later, "documents") == set()


def test_app_worker_column_grants_are_exactly_these(db: TestDb) -> None:
    expected = {
        ("documents", "SELECT"): {
            "id",
            "tenant_id",
            "batch_id",
            "status",
            "attempt",
            "sha256",
            "s3_key",
            "size_bytes",
            "page_count",
            "error_code",
        },
        ("documents", "UPDATE"): {
            "status",
            "error_code",
            "s3_key",
            "size_bytes",
            "page_count",
            "completed_at",
        },
        ("batch_files", "SELECT"): {
            "id",
            "tenant_id",
            "document_id",
            "kind",
            "s3_key",
            "version_id",
            "sha256",
            "size_bytes",
            "status",
        },
        # `attempt` since migration 0011: a repeated step asks about its own attempt only.
        ("document_events", "SELECT"): {
            "tenant_id",
            "document_id",
            "step",
            "status",
            "at",
            "attempt",
        },
    }
    found: dict[tuple[str, str], set[str]] = {}
    with psycopg.connect(db.url) as conn:
        for privilege in ("SELECT", "UPDATE"):
            rows = conn.execute(
                "SELECT c.relname, a.attname FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname IN ('assay', 'platform', 'pipeline', 'auth_events') "
                "AND c.relkind IN ('r', 'p', 'v') AND a.attnum > 0 AND NOT a.attisdropped "
                "AND has_column_privilege(%s, c.oid, a.attname, %s)",
                (APP_WORKER, privilege),
            ).fetchall()
            for table, column in rows:
                found.setdefault((table, privilege), set()).add(column)
    # document_events has table-level INSERT, which the privilege matrix test covers.
    assert found == expected


def test_migration_0010_goes_down_and_up_again_cleanly() -> None:
    """Down removes the table, the column, the four functions and every app_worker grant and
    policy, and leaves 0009's grants as they were; up restores them."""
    from tests.unit.pgtest import _bootstrap_as_master, _create_database, _drop_database, _migrate

    def state(name: str) -> dict[str, list[tuple[object, ...]]]:
        queries = {
            "functions": "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname = 'pipeline' ORDER BY 1",
            "policies": "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname = 'assay' ORDER BY 1, 2",
            "columns": "SELECT grantee, table_name, column_name, privilege_type "
            "FROM information_schema.column_privileges WHERE table_schema = 'assay' "
            "AND grantee IN ('app_worker', 'pipeline_lookup', 'tss') ORDER BY 1, 2, 3, 4",
            "tables": "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.table_privileges WHERE table_schema = 'assay' "
            "AND grantee IN ('app_worker', 'tss') ORDER BY 1, 2, 3",
            "schemas": "SELECT nspname, has_schema_privilege('app_worker', nspname, 'USAGE'), "
            "has_schema_privilege('tss', nspname, 'USAGE') FROM pg_namespace "
            "WHERE nspname IN ('assay', 'pipeline') ORDER BY 1",
            "attempt": "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
            "'assay' AND table_name = 'documents' AND column_name = 'attempt'",
            "grants_table": "SELECT count(*) FROM information_schema.tables WHERE table_schema = "
            "'assay' AND table_name = 'worker_grants'",
        }
        with psycopg.connect(_conninfo(name)) as conn:
            return {key: conn.execute(query).fetchall() for key, query in queries.items()}

    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name, "0009")
        before = state(name)
        assert before["attempt"] == [(0,)] and before["grants_table"] == [(0,)]
        assert not [row for row in before["columns"] if row[0] == "app_worker"]
        assert before["schemas"] == [("assay", False, True), ("pipeline", False, False)]

        _migrate(name, "0010")
        after = state(name)
        assert after["attempt"] == [(1,)] and after["grants_table"] == [(1,)]
        assert [row[0] for row in after["functions"]] == [
            "bind_worker",
            "bound_document",
            "bound_tenant",
            "tenant_for_upload",
            "tenants_with_work",
            "worker_grant",
        ]
        assert after["schemas"] == [("assay", True, True), ("pipeline", True, True)]
        assert after["tables"] == [
            ("app_worker", "document_events", "INSERT"),
            ("tss", "tenant_storage", "SELECT"),
        ]

        _migrate(name, "0009", down=True)
        assert state(name) == before
        _migrate(name, "0010")
        assert state(name) == after
    finally:
        _drop_database(name)
