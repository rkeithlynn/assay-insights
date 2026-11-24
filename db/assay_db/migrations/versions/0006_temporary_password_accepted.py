"""Auth events: the temporary password accepted, apart from the sign-in (build step 5).

A first sign-in on the tenant pool runs Cognito's post-authentication trigger twice: once
when the temporary password is accepted, before the new-password challenge, and once when
the new password is set and tokens are issued. Both were recorded as `sign_in`. The trigger
now sends the first as `temporary_password_accepted` (it sees the user still in
`FORCE_CHANGE_PASSWORD`), and `auth_events.record()` accepts that action.

The new action has the same effect on `users` as a sign-in (`last_sign_in_at`, and `invited`
to `active`), as before this revision: on the platform pool it is the only event a first
sign-in sends, because the trigger does not run again after MFA setup in managed login.

Only the function's body changes. `CREATE OR REPLACE` keeps its owner
(`auth_event_recorder`), its fixed search_path, its SECURITY DEFINER and its single
EXECUTE grant (`audit_writer`); no role, grant or policy changes. `migrator` may not replace
a function it does not own (it is a member of `auth_event_recorder` with SET only, not
INHERIT), so it switches to that role for the one statement.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
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
    IF p_action IS NULL OR p_action NOT IN ({actions}) THEN
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

    IF p_action IN ({sign_in_actions}) THEN
        UPDATE assay.users
           SET last_sign_in_at = GREATEST(COALESCE(last_sign_in_at, p_at), p_at),
               status = CASE WHEN status = 'invited' THEN 'active' ELSE status END
         WHERE id = v_user_id;
    END IF;
    RETURN 'recorded';
END
$$
"""

RECORD_FUNCTION = _RECORD_FUNCTION.format(
    actions="'sign_in', 'temporary_password_accepted', 'password_reset'",
    sign_in_actions="'sign_in', 'temporary_password_accepted'",
)
# Revision 0004's function, for the downgrade.
RECORD_FUNCTION_0004 = _RECORD_FUNCTION.format(
    actions="'sign_in', 'password_reset'", sign_in_actions="'sign_in'"
)


def _replace(definition: str) -> None:
    op.execute(f"SET ROLE {OWNER}")
    op.execute(definition)
    op.execute("RESET ROLE")


def upgrade() -> None:
    _replace(RECORD_FUNCTION)


def downgrade() -> None:
    _replace(RECORD_FUNCTION_0004)
