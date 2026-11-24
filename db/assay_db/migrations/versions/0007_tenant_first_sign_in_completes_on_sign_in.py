"""Auth events: in the tenant pool, only a completed sign-in makes a user Active.

Owner decision (2026-11-24). Revision 0006 gave `temporary_password_accepted` the effect of a
sign-in on `users` (`last_sign_in_at`, and `invited` to `active`). In the tenant pool that
event comes first and a `sign_in` follows once the new password is set, so a user who entered
the temporary password and abandoned the new-password form was shown Active, was offered a
password reset Cognito refuses, and was not offered a resent invitation. Now:

- tenant pool: `temporary_password_accepted` is recorded in `audit_log` and changes nothing in
  `users`; only `sign_in` sets `last_sign_in_at` and moves `invited` to `active`;
- platform pool: unchanged. Its first sign-in sends only `temporary_password_accepted` (the
  trigger does not run again after MFA setup in managed login), so that event still has the
  sign-in's effect there.

Only the function's body changes, and the function already receives the pool. `CREATE OR
REPLACE` keeps its owner (`auth_event_recorder`), its fixed search_path, its SECURITY DEFINER
and its single EXECUTE grant (`audit_writer`); no role, grant or policy changes. As in 0006,
`migrator` switches to the owner for the one statement (it holds SET, not INHERIT, on it).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "auth_event_recorder"

_RECORD_FUNCTION = """
CREATE OR REPLACE FUNCTION auth_events.record(
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
    IF p_action IS NULL OR p_action NOT IN (
        'sign_in', 'temporary_password_accepted', 'password_reset'
    ) THEN
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

    IF {completes_sign_in} THEN
        UPDATE assay.users
           SET last_sign_in_at = GREATEST(COALESCE(last_sign_in_at, p_at), p_at),
               status = CASE WHEN status = 'invited' THEN 'active' ELSE status END
         WHERE id = v_user_id;
    END IF;
    RETURN 'recorded';
END
$$
"""

# A completed sign-in: `sign_in` in either pool; `temporary_password_accepted` in the platform
# pool only, where it is the one event a first sign-in sends.
RECORD_FUNCTION = _RECORD_FUNCTION.format(
    completes_sign_in=(
        "p_action = 'sign_in' OR (p_action = 'temporary_password_accepted' AND p_pool = 'platform')"
    )
)
# Revision 0006's function, for the downgrade.
RECORD_FUNCTION_0006 = _RECORD_FUNCTION.format(
    completes_sign_in="p_action IN ('sign_in', 'temporary_password_accepted')"
)


def _replace(definition: str) -> None:
    op.execute(f"SET ROLE {OWNER}")
    op.execute(definition)
    op.execute("RESET ROLE")


def upgrade() -> None:
    _replace(RECORD_FUNCTION)


def downgrade() -> None:
    _replace(RECORD_FUNCTION_0006)
