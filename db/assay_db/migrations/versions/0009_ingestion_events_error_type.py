"""The master view's step panel shows a failed step's error type (build step 6).

Owner decision (2026-11-24, after the first browser upload failed with a generic code). The
pipeline's `failed` step now writes `{step}_failed` as the document's code (all a tenant user
sees) and keeps the error's name in its event's `detail.error_type`: an exception type, an AWS
error code or a `States.*` name, never a message or stack trace. Master admins see it in
`platform.ingestion_events_v`, which gains one column, `error_type`, at the end.

A read widening for `platform_reader`: one more value from `document_events.detail`, which
`platform_view_owner` could already read. The view shows it only when it matches the pattern
the pipeline checks (`^[A-Za-z0-9_.]{1,64}$`), so a value that is not a plain name is never
shown, whatever wrote it. No grant, policy or role changes: the view is dropped and created
again exactly as 0008 created it (as `migrator`, then given to `platform_view_owner`), with
the same SELECT grant to `platform_reader` and nothing for PUBLIC. `migrator` switches to the
owner only to drop it (it holds SET, not INHERIT, on that role).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "platform_view_owner"
READER = "platform_reader"

VIEW_0008 = """
CREATE VIEW platform.ingestion_events_v WITH (security_barrier = true) AS
SELECT
    e.document_id,
    e.tenant_id,
    e.step,
    e.status,
    e.at,
    e.detail ->> 'error' AS error_code
FROM assay.document_events e
WHERE e.step <> 'viewed'
"""

VIEW_0009 = """
CREATE VIEW platform.ingestion_events_v WITH (security_barrier = true) AS
SELECT
    e.document_id,
    e.tenant_id,
    e.step,
    e.status,
    e.at,
    e.detail ->> 'error' AS error_code,
    CASE
        WHEN e.detail ->> 'error_type' ~ '^[A-Za-z0-9_.]{1,64}$'
        THEN e.detail ->> 'error_type'
    END AS error_type
FROM assay.document_events e
WHERE e.step <> 'viewed'
"""


def _replace(view: str) -> None:
    op.execute(f"SET ROLE {OWNER}")
    op.execute("DROP VIEW platform.ingestion_events_v")
    op.execute("RESET ROLE")
    op.execute(view)
    op.execute("REVOKE ALL ON platform.ingestion_events_v FROM PUBLIC")
    op.execute(f"GRANT SELECT ON platform.ingestion_events_v TO {READER}")
    # Last: the grant above carries over to the new owner.
    op.execute(f"ALTER VIEW platform.ingestion_events_v OWNER TO {OWNER}")


def upgrade() -> None:
    _replace(VIEW_0009)


def downgrade() -> None:
    _replace(VIEW_0008)
