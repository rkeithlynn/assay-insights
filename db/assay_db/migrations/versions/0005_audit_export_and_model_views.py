"""Audit exports and the model views for admin routes (build step 5, second half).

1. `audit_log.category` gains `audit`: every CSV export of Audit history writes its own row
   (`audit` / `exported`) with the actor, the filters and the row count.
2. Two views for `platform_reader`, owned by `platform_view_owner` with column-level SELECT
   only, so `platform_reader` still has no direct access to any table but `tenants` and
   `audit_log`:
   - `platform.ai_models_v`: the catalog (no tenant data; `ai_models` has no tenant_id);
   - `platform.tenant_models_v`: which catalog models each tenant may use, per job.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
CATEGORIES_0001 = "category IN ('auth', 'user', 'tenant')"
CATEGORIES = "category IN ('auth', 'user', 'tenant', 'audit')"

AI_MODELS_VIEW = """
CREATE VIEW platform.ai_models_v WITH (security_barrier = true) AS
SELECT id AS model_id, name, profile_id, jobs, price_in, price_out, status
FROM assay.ai_models
"""

TENANT_MODELS_VIEW = """
CREATE VIEW platform.tenant_models_v WITH (security_barrier = true) AS
SELECT tm.tenant_id, tm.model_id, tm.job
FROM assay.tenant_models tm
"""


def _platform_view(name: str, definition: str) -> None:
    op.execute(definition)
    op.execute(f"REVOKE ALL ON platform.{name} FROM PUBLIC")
    op.execute(f"GRANT SELECT ON platform.{name} TO platform_reader")
    # Last: the grant above carries over to the new owner.
    op.execute(f"ALTER VIEW platform.{name} OWNER TO platform_view_owner")


def upgrade() -> None:
    op.execute("ALTER TABLE assay.audit_log DROP CONSTRAINT ck_audit_log_category")
    op.execute(
        f"ALTER TABLE assay.audit_log ADD CONSTRAINT ck_audit_log_category CHECK ({CATEGORIES})"
    )

    op.execute(
        "GRANT SELECT (id, name, profile_id, jobs, price_in, price_out, status) "
        "ON assay.ai_models TO platform_view_owner"
    )
    op.execute(
        "GRANT SELECT (tenant_id, model_id, job) ON assay.tenant_models TO platform_view_owner"
    )
    op.execute(
        "CREATE POLICY platform_view_owner_all ON assay.tenant_models AS PERMISSIVE FOR SELECT "
        "TO platform_view_owner USING (true)"
    )
    _platform_view("ai_models_v", AI_MODELS_VIEW)
    _platform_view("tenant_models_v", TENANT_MODELS_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.tenant_models_v")
    op.execute("DROP VIEW IF EXISTS platform.ai_models_v")
    op.execute("DROP POLICY IF EXISTS platform_view_owner_all ON assay.tenant_models")
    op.execute(
        "REVOKE SELECT (tenant_id, model_id, job) ON assay.tenant_models FROM platform_view_owner"
    )
    op.execute(
        "REVOKE SELECT (id, name, profile_id, jobs, price_in, price_out, status) "
        "ON assay.ai_models FROM platform_view_owner"
    )
    # audit_log is append-only: existing `audit` rows are kept, so the old check is restored
    # NOT VALID (it applies to new rows only).
    op.execute("ALTER TABLE assay.audit_log DROP CONSTRAINT ck_audit_log_category")
    op.execute(
        "ALTER TABLE assay.audit_log ADD CONSTRAINT ck_audit_log_category "
        f"CHECK ({CATEGORIES_0001}) NOT VALID"
    )
