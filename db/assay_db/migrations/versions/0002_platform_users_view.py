"""Platform users view, for GET /me on platform-pool tokens.

`platform_reader` has no privilege on `assay.users`. Admin routes read the platform's own
users through `platform.platform_users_v`, which is owned by `platform_view_owner` and
shows only users of the platform tenant: never a business tenant's users.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-23 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose; later revisions add their own grants and policies.
GRANT_USER_COLUMNS = (
    "GRANT SELECT (id, tenant_id, cognito_sub, email, name, role, status) "
    "ON assay.users TO platform_view_owner"
)
REVOKE_USER_COLUMNS = (
    "REVOKE SELECT (id, tenant_id, cognito_sub, email, name, role, status) "
    "ON assay.users FROM platform_view_owner"
)
# 0001 granted (id, name); the view also needs the kind and the status.
GRANT_TENANT_COLUMNS = "GRANT SELECT (kind, status) ON assay.tenants TO platform_view_owner"
REVOKE_TENANT_COLUMNS = "REVOKE SELECT (kind, status) ON assay.tenants FROM platform_view_owner"

# The policy itself, not only the view, limits the owner to the platform tenant's users.
PLATFORM_USERS_POLICY = """
CREATE POLICY platform_view_owner_platform_users ON assay.users
AS PERMISSIVE FOR SELECT TO platform_view_owner
USING (tenant_id IN (SELECT t.id FROM assay.tenants t WHERE t.kind = 'platform'))
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
    u.status
FROM assay.users u
JOIN assay.tenants t ON t.id = u.tenant_id
WHERE t.kind = 'platform'
"""


def upgrade() -> None:
    op.execute(GRANT_USER_COLUMNS)
    op.execute(GRANT_TENANT_COLUMNS)
    op.execute(PLATFORM_USERS_POLICY)
    op.execute(PLATFORM_USERS_VIEW)
    op.execute("REVOKE ALL ON platform.platform_users_v FROM PUBLIC")
    op.execute("GRANT SELECT ON platform.platform_users_v TO platform_reader")
    # Last: the grant above carries over to the new owner.
    op.execute("ALTER VIEW platform.platform_users_v OWNER TO platform_view_owner")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.platform_users_v")
    op.execute("DROP POLICY IF EXISTS platform_view_owner_platform_users ON assay.users")
    op.execute(REVOKE_TENANT_COLUMNS)
    op.execute(REVOKE_USER_COLUMNS)
