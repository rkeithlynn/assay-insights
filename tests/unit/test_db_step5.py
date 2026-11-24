"""Migration 0004 and the catalog, against a real PostgreSQL 17 (build step 5).

- `auth_events.record()`: the only function `audit_writer` may call. SECURITY DEFINER, fixed
  search_path, fixed actions; it takes the tenant from `users`, never from the event, and an
  unknown user is reported, not recorded.
- The admin views: no view in schema `platform` shows a business tenant's non-admin users.
- `platform_writer` may end the sessions of a tenant-admin or the master-user, nothing more.
- The catalog: seeded as `migrator`; a tenant's default models set as `platform_writer`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors, sql

from assay_db import catalog, handler
from assay_db.roles import (
    APP_TENANT,
    AUDIT_WRITER,
    AUTH_EVENT_RECORDER,
    MIGRATOR,
    PLATFORM_READER,
    PLATFORM_WRITER,
)
from tests.unit.pgtest import TestDb, as_role, migrator_engine, requires_db

pytestmark = requires_db

RECORD = "SELECT auth_events.record(%s, %s, %s, %s, %s)"
NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def tenant_user(db: TestDb) -> Iterator[tuple[uuid.UUID, str]]:
    """A non-admin user of tenant A, committed for the test and removed afterwards."""
    user_id = uuid.uuid4()
    sub = f"sub-user-{user_id.hex[:8]}"
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, status) "
            "VALUES (%s, %s, %s, %s, %s, 'tenant-user', 'invited')",
            (user_id, db.seed.tenant_a, sub, f"{sub}@secret.test", f"Secret {sub}"),
        )
    try:
        yield user_id, sub
    finally:
        with psycopg.connect(db.url) as conn:
            conn.execute("DELETE FROM assay.audit_log WHERE actor_user_id = %s", (user_id,))
            conn.execute("DELETE FROM assay.users WHERE id = %s", (user_id,))


def _audit_rows(db: TestDb, event_id: uuid.UUID) -> list[tuple[object, ...]]:
    with psycopg.connect(db.url) as conn:
        return conn.execute(
            "SELECT tenant_id, actor_user_id, actor_email, actor_role, category, action, outcome "
            "FROM assay.audit_log WHERE id = %s",
            (event_id,),
        ).fetchall()


# --- auth_events.record -----------------------------------------------------------------------


def test_a_sign_in_is_recorded_in_the_users_own_tenant(
    db: TestDb, tenant_user: tuple[uuid.UUID, str]
) -> None:
    user_id, sub = tenant_user
    event = uuid.uuid4()
    with as_role(db, AUDIT_WRITER) as conn:
        assert conn.execute(RECORD, (event, "tenant", sub, "sign_in", NOW)).fetchone() == (
            "recorded",
        )
        conn.commit()
    [row] = _audit_rows(db, event)
    assert row == (
        db.seed.tenant_a,
        user_id,
        f"{sub}@secret.test",
        "tenant-user",
        "auth",
        "sign_in",
        "success",
    )
    with psycopg.connect(db.url) as conn:
        state = conn.execute(
            "SELECT status, last_sign_in_at FROM assay.users WHERE id = %s", (user_id,)
        ).fetchone()
    assert state == ("active", NOW)


def test_a_repeated_event_is_recorded_once(db: TestDb, tenant_user: tuple[uuid.UUID, str]) -> None:
    _, sub = tenant_user
    event = uuid.uuid4()
    for _attempt in range(2):
        with as_role(db, AUDIT_WRITER) as conn:
            conn.execute(RECORD, (event, "tenant", sub, "password_reset", NOW))
            conn.commit()
    assert len(_audit_rows(db, event)) == 1


def test_a_platform_sign_in_lands_in_the_platform_tenant(db: TestDb) -> None:
    event = uuid.uuid4()
    with as_role(db, AUDIT_WRITER) as conn:
        conn.execute(RECORD, (event, "platform", "sub-platform", "sign_in", NOW))
        conn.commit()
    [row] = _audit_rows(db, event)
    assert row[:4] == (
        db.seed.platform,
        db.seed.user_platform,
        "master@example.test",
        "master-admin",
    )


@pytest.mark.parametrize(
    ("pool", "sub"),
    [
        ("tenant", "no-such-sub"),
        # The sub exists, but in the other pool's kind of tenant: never matched across pools.
        ("platform", "sub-a"),
        ("tenant", "sub-platform"),
    ],
)
def test_an_unknown_user_is_reported_and_nothing_is_written(
    db: TestDb, pool: str, sub: str
) -> None:
    event = uuid.uuid4()
    with as_role(db, AUDIT_WRITER) as conn:
        assert conn.execute(RECORD, (event, pool, sub, "sign_in", NOW)).fetchone() == (
            "unknown_user",
        )
        conn.commit()
    assert _audit_rows(db, event) == []


@pytest.mark.parametrize(
    ("pool", "action"),
    [("tenant", "role_changed"), ("tenant", "sign_out"), ("tenant", None), ("other", "sign_in")],
)
def test_only_the_two_actions_and_pools_are_allowed(
    db: TestDb, pool: str, action: str | None
) -> None:
    with (
        as_role(db, AUDIT_WRITER) as conn,
        pytest.raises(errors.InvalidParameterValue),
    ):
        conn.execute(RECORD, (uuid.uuid4(), pool, "sub-a", action, NOW))


def test_the_function_is_a_locked_down_security_definer(db: TestDb) -> None:
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, "
            "has_function_privilege('public', p.oid, 'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'auth_events' AND p.proname = 'record'"
        ).fetchone()
        executors = {
            role
            for role in (APP_TENANT, AUDIT_WRITER, PLATFORM_READER, PLATFORM_WRITER, MIGRATOR)
            if conn.execute(
                "SELECT has_function_privilege(%s, 'auth_events.record(uuid, text, text, text, "
                "timestamptz)', 'EXECUTE')",
                (role,),
            ).fetchone()
            == (True,)
        }
        recorder_login = conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (AUTH_EVENT_RECORDER,)
        ).fetchone()
    assert row == (True, AUTH_EVENT_RECORDER, ["search_path=pg_catalog, pg_temp"], False)
    # migrator owns the schema, not the function; only audit_writer was granted EXECUTE.
    assert executors == {AUDIT_WRITER}
    assert recorder_login == (False,)


@pytest.mark.parametrize("role", [APP_TENANT, PLATFORM_WRITER, PLATFORM_READER])
def test_no_other_role_can_record_auth_events(db: TestDb, role: str) -> None:
    with as_role(db, role, db.seed.tenant_a) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(RECORD, (uuid.uuid4(), "tenant", "sub-a", "sign_in", NOW))


def test_audit_writer_still_cannot_read_users(db: TestDb) -> None:
    with (
        as_role(db, AUDIT_WRITER, db.seed.tenant_a) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT 1 FROM assay.users")


# --- Admin views -------------------------------------------------------------------------


def test_no_platform_view_shows_a_business_tenants_non_admin_users(
    db: TestDb, tenant_user: tuple[uuid.UUID, str]
) -> None:
    """Owner condition D8: whatever a view shows, never a tenant-user's identity."""
    user_id, sub = tenant_user
    secrets_of_user = {str(user_id), sub, f"{sub}@secret.test", f"Secret {sub}"}
    with as_role(db, PLATFORM_READER) as conn:
        views = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.views WHERE table_schema = 'platform'"
            ).fetchall()
        ]
        assert {"platform_users_v", "tenant_admins_v", "tenant_summary_v"} <= set(views)
        for view in views:
            rows = conn.execute(
                sql.SQL("SELECT * FROM platform.{}").format(sql.Identifier(view))
            ).fetchall()
            seen = {str(value) for row in rows for value in row}
            assert not seen & secrets_of_user, view


def test_tenant_admins_view_and_counts(db: TestDb, tenant_user: tuple[uuid.UUID, str]) -> None:
    s = db.seed
    with as_role(db, PLATFORM_READER) as conn:
        admins = conn.execute(
            "SELECT user_id, tenant_id, email FROM platform.tenant_admins_v ORDER BY email"
        ).fetchall()
        counts = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(
                "SELECT tenant_id, user_count, document_count FROM platform.tenant_summary_v"
            ).fetchall()
        )
    assert admins == [
        (s.user_a, s.tenant_a, "admin-a@example.test"),
        (s.user_b, s.tenant_b, "admin-b@example.test"),
    ]
    assert counts[s.tenant_a] == (2, 1)  # the admin and the fixture's tenant user
    assert counts[s.tenant_b] == (1, 1)
    assert counts[s.platform] == (1, 0)


# --- platform_writer: MFA resets ---------------------------------------------------------


def test_platform_writer_ends_sessions_of_tenant_admins_only(
    db: TestDb, tenant_user: tuple[uuid.UUID, str]
) -> None:
    user_id, _ = tenant_user
    s = db.seed
    update = "UPDATE assay.users SET sessions_revoked_at = now() WHERE id = %s"
    with as_role(db, PLATFORM_WRITER) as conn:
        assert conn.execute(update, (s.user_a,)).rowcount == 1
        # A tenant-user and the master admin are not its to touch.
        assert conn.execute(update, (user_id,)).rowcount == 0
        assert conn.execute(update, (s.user_platform,)).rowcount == 0
    with as_role(db, PLATFORM_WRITER) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE assay.users SET role = 'tenant-user' WHERE id = %s", (s.user_a,))
    with as_role(db, PLATFORM_WRITER) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT email FROM assay.users")


def test_platform_writer_cannot_change_the_catalog(db: TestDb) -> None:
    with as_role(db, PLATFORM_WRITER) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO assay.ai_models (name, profile_id, jobs, price_in, price_out, status) "
            "VALUES ('x', 'us.x', ARRAY['query'], 0, 0, 'active')"
        )


# --- Catalog and default models ----------------------------------------------------------

MODELS = [
    {
        "name": "Model One",
        "profile_id": "us.example.one-v1:0",
        "jobs": ["ingest", "query"],
        "price_in": "1",
        "price_out": "5",
    },
    {
        "name": "Model Two",
        "profile_id": "us.example.two-v1",
        "jobs": ["query"],
        "price_in": "3",
        "price_out": "15",
    },
]


@pytest.fixture(scope="module")
def seeded_catalog(db: TestDb) -> None:
    engine = migrator_engine(db.name)
    try:
        with engine.begin() as conn:
            catalog.seed_models(conn, catalog.parse_models(MODELS))
    finally:
        engine.dispose()


def test_seed_models_upserts_by_profile(db: TestDb, seeded_catalog: None) -> None:
    engine = migrator_engine(db.name)
    try:
        with engine.begin() as conn:
            changed = [{**MODELS[0], "price_in": "2"}]
            result = catalog.seed_models(conn, catalog.parse_models(changed))
    finally:
        engine.dispose()
    assert result == {"inserted": [], "updated": ["us.example.one-v1:0"]}
    with psycopg.connect(db.url) as conn:
        price = conn.execute(
            "SELECT price_in FROM assay.ai_models WHERE profile_id = 'us.example.one-v1:0'"
        ).fetchone()
    assert price is not None and float(price[0]) == 2.0


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**MODELS[0], "price_in": ""}, "price_in is not set"),
        ({**MODELS[0], "profile_id": "anthropic.claude-direct"}, "us. inference profile"),
        ({**MODELS[0], "profile_id": "global.anthropic.claude"}, "us. inference profile"),
        ({**MODELS[0], "jobs": ["chat"]}, "jobs"),
        ({**MODELS[0], "price_out": "-1"}, "zero or more"),
    ],
)
def test_catalog_entries_are_checked(entry: dict[str, object], message: str) -> None:
    with pytest.raises(catalog.CatalogError, match=message):
        catalog.parse_models([entry])


def test_seed_models_through_the_migration_lambda_validates_first() -> None:
    """A bad entry is refused before the Lambda connects to anything."""
    with pytest.raises(catalog.CatalogError):
        handler.handler({"command": "seed_models", "models": [{"name": "x"}]}, None)


def test_a_tenant_gets_the_default_models_once(db: TestDb, seeded_catalog: None) -> None:
    s = db.seed
    defaults = catalog.DefaultModels(ingest="us.example.one-v1:0", query="us.example.two-v1")
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "UPDATE assay.tenants SET extraction_model = NULL, answer_model = NULL WHERE id = %s",
            (s.tenant_b,),
        )
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from tests.unit.pgtest import _conninfo

    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(_conninfo(db.name, PLATFORM_WRITER)),
        poolclass=NullPool,
    )
    try:
        with engine.begin() as conn:
            assert catalog.ensure_default_models(conn, tenant_id=s.tenant_b, defaults=defaults)
        with engine.begin() as conn:
            assert not catalog.ensure_default_models(conn, tenant_id=s.tenant_b, defaults=defaults)
    finally:
        engine.dispose()
    with psycopg.connect(db.url) as conn:
        selected = conn.execute(
            "SELECT i.profile_id, q.profile_id FROM assay.tenants t "
            "JOIN assay.ai_models i ON i.id = t.extraction_model "
            "JOIN assay.ai_models q ON q.id = t.answer_model WHERE t.id = %s",
            (s.tenant_b,),
        ).fetchone()
        available = conn.execute(
            "SELECT tm.job, m.profile_id FROM assay.tenant_models tm "
            "JOIN assay.ai_models m ON m.id = tm.model_id WHERE tm.tenant_id = %s ORDER BY tm.job",
            (s.tenant_b,),
        ).fetchall()
        audits = conn.execute(
            "SELECT count(*) FROM assay.audit_log WHERE tenant_id = %s "
            "AND action = 'models_available_changed'",
            (s.tenant_b,),
        ).fetchone()
    assert selected == ("us.example.one-v1:0", "us.example.two-v1")
    assert available == [("ingest", "us.example.one-v1:0"), ("query", "us.example.two-v1")]
    assert audits == (1,)


def test_a_default_must_be_a_us_inference_profile() -> None:
    with pytest.raises(catalog.CatalogError):
        catalog.DefaultModels(ingest="anthropic.direct", query="us.example.two-v1")
