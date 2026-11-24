"""Accounts and auth events (build step 5).

1. `users.sessions_revoked_at`. Resetting a user's MFA or disabling them sets it, and the
   API's account check refuses any token issued by a sign-in before it. Global sign-out
   revokes refresh tokens only; this ends the access tokens the API verifies locally.
2. `auth_events.record(...)`: the one thing `audit_writer` may call besides its INSERT on
   `audit_log`. The Cognito trigger queues a sign-in or password reset; the consumer Lambda
   (as `audit_writer`) passes the pool, the user's `sub`, a fixed action and the time. The
   function, owned by the NOLOGIN role `auth_event_recorder`, finds the user's tenant, ID,
   email and role in `users` (it never takes a tenant from the event), writes the audit row
   and, for a sign-in, sets `last_sign_in_at` and moves `invited` to `active`.
   SECURITY DEFINER with a fixed search_path and a fixed allow-list of actions. An unknown
   user is reported as `unknown_user`, so the consumer fails the message and it reaches the
   dead-letter queue instead of being dropped.
3. Admin views, owned by `platform_view_owner`, which keeps column-level SELECT only:
   `platform.tenant_summary_v` (user and document counts per tenant) and
   `platform.tenant_admins_v` (each business tenant's admins: name, email, status). The
   owner's policy on `users` becomes `true`, and every view filters in its own WHERE
   clause; no view exposes a business tenant's non-admin users. `platform_users_v` gains
   `sessions_revoked_at` for the account check.
4. `platform_writer` may set `sessions_revoked_at` on a tenant-admin or the master-user
   (the master admin's MFA reset), and read only `id` and `role` of those rows to do it.
   It gets no write on `ai_models`: the catalog is seeded as `migrator` through the
   migration Lambda.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.

RECORD_FUNCTION = """
CREATE FUNCTION auth_events.record(
    p_event_id uuid, p_pool text, p_sub text, p_action text, p_at timestamptz
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_user_id uuid;
    v_tenant_id uuid;
    v_email text;
    v_role text;
BEGIN
    IF p_action IS NULL OR p_action NOT IN ('sign_in', 'password_reset') THEN
        RAISE EXCEPTION 'auth event action not allowed' USING ERRCODE = '22023';
    END IF;
    IF p_pool IS NULL OR p_pool NOT IN ('tenant', 'platform') THEN
        RAISE EXCEPTION 'auth event pool not allowed' USING ERRCODE = '22023';
    END IF;
    IF p_event_id IS NULL OR p_sub IS NULL OR p_at IS NULL THEN
        RAISE EXCEPTION 'auth event incomplete' USING ERRCODE = '22023';
    END IF;

    SELECT u.id, u.tenant_id, u.email, u.role
      INTO v_user_id, v_tenant_id, v_email, v_role
      FROM assay.users u
      JOIN assay.tenants t ON t.id = u.tenant_id
     WHERE u.cognito_sub = p_sub
       AND t.kind = CASE p_pool WHEN 'platform' THEN 'platform' ELSE 'business' END;
    IF NOT FOUND THEN
        RETURN 'unknown_user';
    END IF;

    -- The event ID is the audit row's ID, so a message delivered twice is recorded once.
    INSERT INTO assay.audit_log (
        id, tenant_id, at, actor_user_id, actor_email, actor_role, category, action,
        target_type, target_id, target_label, detail, outcome
    ) VALUES (
        p_event_id, v_tenant_id, p_at, v_user_id, v_email, v_role, 'auth', p_action,
        'user', v_user_id::text, v_email, pg_catalog.jsonb_build_object('pool', p_pool),
        'success'
    ) ON CONFLICT DO NOTHING;

    IF p_action = 'sign_in' THEN
        UPDATE assay.users
           SET last_sign_in_at = GREATEST(COALESCE(last_sign_in_at, p_at), p_at),
               status = CASE WHEN status = 'invited' THEN 'active' ELSE status END
         WHERE id = v_user_id;
    END IF;
    RETURN 'recorded';
END
$$
"""

TENANT_SUMMARY_VIEW = """
CREATE VIEW platform.tenant_summary_v WITH (security_barrier = true) AS
SELECT
    t.id AS tenant_id,
    (SELECT count(*) FROM assay.users u WHERE u.tenant_id = t.id) AS user_count,
    (SELECT count(*) FROM assay.documents d WHERE d.tenant_id = t.id) AS document_count
FROM assay.tenants t
"""

TENANT_ADMINS_VIEW = """
CREATE VIEW platform.tenant_admins_v WITH (security_barrier = true) AS
SELECT
    u.id AS user_id,
    u.tenant_id,
    u.cognito_sub,
    u.email,
    u.name,
    u.status
FROM assay.users u
JOIN assay.tenants t ON t.id = u.tenant_id
WHERE t.kind = 'business' AND u.role = 'tenant-admin'
"""

PLATFORM_USERS_VIEW = """
CREATE VIEW platform.platform_users_v WITH (security_barrier = true) AS
SELECT
    u.id AS user_id,
    u.tenant_id,
    t.name AS tenant_name,
    t.status AS tenant_status,
    u.cognito_sub,
    u.email,
    u.name,
    u.role,
    u.status,
    u.sessions_revoked_at
FROM assay.users u
JOIN assay.tenants t ON t.id = u.tenant_id
WHERE t.kind = 'platform'
"""

# As 0002 created it, for the downgrade.
PLATFORM_USERS_VIEW_0002 = """
CREATE VIEW platform.platform_users_v WITH (security_barrier = true) AS
SELECT
    u.id AS user_id,
    u.tenant_id,
    t.name AS tenant_name,
    t.status AS tenant_status,
    u.cognito_sub,
    u.email,
    u.name,
    u.role,
    u.status
FROM assay.users u
JOIN assay.tenants t ON t.id = u.tenant_id
WHERE t.kind = 'platform'
"""
PLATFORM_USERS_POLICY_0002 = """
CREATE POLICY platform_view_owner_platform_users ON assay.users
AS PERMISSIVE FOR SELECT TO platform_view_owner
USING (tenant_id IN (SELECT t.id FROM assay.tenants t WHERE t.kind = 'platform'))
"""

# The rows platform_writer may touch in users: a tenant's admins and the master user.
MFA_RESET_TARGETS = "role IN ('tenant-admin', 'master-user')"


def _platform_view(name: str, definition: str) -> None:
    op.execute(definition)
    op.execute(f"REVOKE ALL ON platform.{name} FROM PUBLIC")
    op.execute(f"GRANT SELECT ON platform.{name} TO platform_reader")
    # Last: the grant above carries over to the new owner.
    op.execute(f"ALTER VIEW platform.{name} OWNER TO platform_view_owner")


def upgrade() -> None:
    # 1. Sessions revoked.
    op.add_column(
        "users",
        sa.Column("sessions_revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema="assay",
    )

    # 2. Auth events.
    op.execute("CREATE SCHEMA auth_events")
    op.execute("REVOKE ALL ON SCHEMA auth_events FROM PUBLIC")
    op.execute("GRANT USAGE, CREATE ON SCHEMA auth_events TO auth_event_recorder")
    op.execute("GRANT USAGE ON SCHEMA auth_events TO audit_writer")
    op.execute("GRANT USAGE ON SCHEMA assay TO auth_event_recorder")
    op.execute(
        "GRANT SELECT (id, tenant_id, cognito_sub, email, role, status, last_sign_in_at) "
        "ON assay.users TO auth_event_recorder"
    )
    op.execute("GRANT UPDATE (last_sign_in_at, status) ON assay.users TO auth_event_recorder")
    op.execute("GRANT SELECT (id, kind) ON assay.tenants TO auth_event_recorder")
    op.execute("GRANT INSERT ON assay.audit_log TO auth_event_recorder")
    for name, table, command, using, check in (
        ("auth_event_recorder_read", "users", "SELECT", "true", None),
        ("auth_event_recorder_sign_in", "users", "UPDATE", "true", "true"),
        ("auth_event_recorder_read", "tenants", "SELECT", "true", None),
        ("auth_event_recorder_insert", "audit_log", "INSERT", None, "true"),
    ):
        statement = (
            f"CREATE POLICY {name} ON assay.{table} AS PERMISSIVE FOR {command} "
            "TO auth_event_recorder"
        )
        if using is not None:
            statement += f" USING ({using})"
        if check is not None:
            statement += f" WITH CHECK ({check})"
        op.execute(statement)
    op.execute(RECORD_FUNCTION)
    op.execute(
        "REVOKE ALL ON FUNCTION auth_events.record(uuid, text, text, text, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION auth_events.record(uuid, text, text, text, timestamptz) "
        "TO audit_writer"
    )
    op.execute(
        "ALTER FUNCTION auth_events.record(uuid, text, text, text, timestamptz) "
        "OWNER TO auth_event_recorder"
    )

    # 3. Admin views. The owner may now see every users row; each view filters for itself.
    op.execute("DROP POLICY platform_view_owner_platform_users ON assay.users")
    op.execute(
        "CREATE POLICY platform_view_owner_all ON assay.users AS PERMISSIVE FOR SELECT "
        "TO platform_view_owner USING (true)"
    )
    op.execute("GRANT SELECT (sessions_revoked_at) ON assay.users TO platform_view_owner")
    op.execute("DROP VIEW platform.platform_users_v")
    _platform_view("platform_users_v", PLATFORM_USERS_VIEW)
    _platform_view("tenant_summary_v", TENANT_SUMMARY_VIEW)
    _platform_view("tenant_admins_v", TENANT_ADMINS_VIEW)

    # 4. platform_writer: end the sessions of a tenant admin or the master user.
    op.execute("GRANT SELECT (id, role) ON assay.users TO platform_writer")
    op.execute("GRANT UPDATE (sessions_revoked_at) ON assay.users TO platform_writer")
    op.execute(
        "CREATE POLICY platform_writer_mfa_reset_read ON assay.users AS PERMISSIVE FOR SELECT "
        f"TO platform_writer USING ({MFA_RESET_TARGETS})"
    )
    op.execute(
        "CREATE POLICY platform_writer_mfa_reset ON assay.users AS PERMISSIVE FOR UPDATE "
        f"TO platform_writer USING ({MFA_RESET_TARGETS}) WITH CHECK ({MFA_RESET_TARGETS})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS platform_writer_mfa_reset ON assay.users")
    op.execute("DROP POLICY IF EXISTS platform_writer_mfa_reset_read ON assay.users")
    op.execute("REVOKE UPDATE (sessions_revoked_at) ON assay.users FROM platform_writer")
    op.execute("REVOKE SELECT (id, role) ON assay.users FROM platform_writer")

    op.execute("DROP VIEW IF EXISTS platform.tenant_admins_v")
    op.execute("DROP VIEW IF EXISTS platform.tenant_summary_v")
    op.execute("DROP VIEW IF EXISTS platform.platform_users_v")
    op.execute("REVOKE SELECT (sessions_revoked_at) ON assay.users FROM platform_view_owner")
    op.execute("DROP POLICY IF EXISTS platform_view_owner_all ON assay.users")
    op.execute(PLATFORM_USERS_POLICY_0002)
    _platform_view("platform_users_v", PLATFORM_USERS_VIEW_0002)

    op.execute("DROP SCHEMA IF EXISTS auth_events CASCADE")
    for name, table in (
        ("auth_event_recorder_insert", "audit_log"),
        ("auth_event_recorder_read", "tenants"),
        ("auth_event_recorder_sign_in", "users"),
        ("auth_event_recorder_read", "users"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {name} ON assay.{table}")
    op.execute("REVOKE INSERT ON assay.audit_log FROM auth_event_recorder")
    op.execute("REVOKE SELECT (id, kind) ON assay.tenants FROM auth_event_recorder")
    op.execute("REVOKE UPDATE (last_sign_in_at, status) ON assay.users FROM auth_event_recorder")
    op.execute(
        "REVOKE SELECT (id, tenant_id, cognito_sub, email, role, status, last_sign_in_at) "
        "ON assay.users FROM auth_event_recorder"
    )
    op.execute("REVOKE USAGE ON SCHEMA assay FROM auth_event_recorder")

    op.drop_column("users", "sessions_revoked_at", schema="assay")
