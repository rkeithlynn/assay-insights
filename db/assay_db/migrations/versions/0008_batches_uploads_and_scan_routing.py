"""Batches and uploads, scan routing, duplicates and the master view's ordering (build step 6).

1. `batch_files`: one row per upload (a PDF or a companion file), keyed by the opaque file
   ID that is the last part of its inbox key (`inbox/{batch_id}/{file_id}`). The file name
   lives here and in `documents` only, never in a key. The registered size and SHA-256 are
   signed into the upload link; `version_id` is the object version that scanned clean, which
   every later read, hash and copy uses. Forced row-level security, and `app_tenant` gets
   SELECT, INSERT and UPDATE (no DELETE), as on every other tenant table.
2. `documents`: status `discarded` (owner decision: a discarded batch's documents, or a
   removed file's, are kept and left out of the default history views); `queued_at`, when an
   eligible document was sent to the ingest queue; a partial unique index that allows one
   original per hash in a tenant (duplicates and failed documents excepted), backing the
   fingerprint step's advisory lock; and an index on `received_at` for the history across
   tenants.
3. Schema `pipeline`, with two SECURITY DEFINER functions owned by the new NOLOGIN role
   `pipeline_lookup`, which `app_tenant` may execute. Both answer only with tenant IDs, and
   read no file name and no `tenant_storage`:
   - `tenant_for_upload(key)`: the tenant of the registered upload with this inbox key, or
     NULL. The scan Lambda calls it before it knows a tenant (owner decision 3); it then
     checks that the event's bucket is that tenant's and works as `app_tenant` in that
     tenant only.
   - `tenants_with_work(...)`: the tenants with a batch open too long, a registered file
     with no scan result, or an eligible document not yet started. The sweeper calls it, then
     works in each tenant as `app_tenant`.
   Each has a fixed search_path; `pipeline_lookup` has column-level SELECT on exactly the
   columns they read, and a SELECT policy on those four tables.
4. `platform.ingestion_events_v`, for the master view's step panel (section 11: "each
   pipeline step, its time and any error"): document, tenant, step, status, time and the
   event's error code, nothing else from `detail`. Views of a PDF (`viewed`, which name the
   viewer) are left out. Owned by `platform_view_owner`, like every admin view.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "pipeline_lookup"

DOCUMENT_STATUSES_0001 = (
    "status IN ('uploaded', 'scanning', 'processing', 'ready', 'needs_review', 'quarantined', "
    "'on_hold', 'failed', 'duplicate')"
)
DOCUMENT_STATUSES = (
    "status IN ('uploaded', 'scanning', 'processing', 'ready', 'needs_review', 'quarantined', "
    "'on_hold', 'failed', 'duplicate', 'discarded')"
)

TENANT_FOR_UPLOAD = """
CREATE FUNCTION pipeline.tenant_for_upload(p_key text) RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT f.tenant_id FROM assay.batch_files f WHERE f.s3_key = p_key
$$
"""

TENANTS_WITH_WORK = """
CREATE FUNCTION pipeline.tenants_with_work(
    p_open_before timestamptz, p_registered_before timestamptz, p_queued_before timestamptz
) RETURNS SETOF uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT b.tenant_id FROM assay.batches b
     WHERE b.status = 'open' AND b.opened_at < p_open_before
    UNION
    SELECT f.tenant_id FROM assay.batch_files f
     WHERE f.status = 'registered' AND f.registered_at < p_registered_before
    UNION
    SELECT d.tenant_id FROM assay.documents d
     WHERE d.status = 'processing'
       AND (d.queued_at IS NULL OR d.queued_at < p_queued_before)
       AND NOT EXISTS (
           SELECT 1 FROM assay.document_events e
            WHERE e.tenant_id = d.tenant_id AND e.document_id = d.id AND e.step = 'start'
       )
$$
"""

INGESTION_EVENTS_VIEW = """
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

FUNCTIONS = (
    "pipeline.tenant_for_upload(text)",
    "pipeline.tenants_with_work(timestamptz, timestamptz, timestamptz)",
)

# The columns the two functions read, and nothing else.
LOOKUP_COLUMNS = {
    "batch_files": "tenant_id, s3_key, status, registered_at",
    "batches": "tenant_id, status, opened_at",
    "documents": "id, tenant_id, status, queued_at",
    "document_events": "tenant_id, document_id, step",
}


def upgrade() -> None:
    # 2. documents (first: batch_files references documents).
    op.execute("ALTER TABLE assay.documents DROP CONSTRAINT ck_documents_status")
    op.execute(
        f"ALTER TABLE assay.documents ADD CONSTRAINT ck_documents_status CHECK ({DOCUMENT_STATUSES})"
    )
    op.add_column(
        "documents",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        schema="assay",
    )
    op.create_index(
        "ix_documents_received_at",
        "documents",
        [sa.literal_column("received_at DESC")],
        unique=False,
        schema="assay",
    )
    op.create_index(
        "uq_documents_tenant_id_sha256_original",
        "documents",
        ["tenant_id", "sha256"],
        unique=True,
        schema="assay",
        postgresql_where=sa.text(
            "sha256 IS NOT NULL AND duplicate_of IS NULL AND status <> 'failed'"
        ),
    )

    # 1. batch_files.
    op.create_table(
        "batch_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("base_name", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="registered", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("scan_result", sa.Text(), nullable=True),
        sa.Column("version_id", sa.Text(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("registered_by", sa.UUID(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('pdf', 'companion')", name=op.f("ck_batch_files_kind")),
        sa.CheckConstraint(
            "status IN ('registered', 'clean', 'invalid', 'mismatch', 'infected', 'unsupported', 'scan_timeout', 'not_uploaded', 'removed')",
            name=op.f("ck_batch_files_status"),
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_batch_files_sha256")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_batch_files_size_bytes")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["assay.batches.tenant_id", "assay.batches.id"],
            name=op.f("fk_batch_files_tenant_id_batch_id_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["assay.documents.tenant_id", "assay.documents.id"],
            name=op.f("fk_batch_files_tenant_id_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "registered_by"],
            ["assay.users.tenant_id", "assay.users.id"],
            name=op.f("fk_batch_files_tenant_id_registered_by_users"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["assay.tenants.id"], name=op.f("fk_batch_files_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batch_files")),
        sa.UniqueConstraint("s3_key", name=op.f("uq_batch_files_s3_key")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_batch_files_tenant_id_id")),
        schema="assay",
    )
    op.create_index(
        op.f("ix_batch_files_tenant_id"), "batch_files", ["tenant_id"], unique=False, schema="assay"
    )
    op.create_index(
        "ix_batch_files_tenant_id_batch_id",
        "batch_files",
        ["tenant_id", "batch_id"],
        unique=False,
        schema="assay",
    )
    op.create_index(
        "uq_batch_files_live_name",
        "batch_files",
        ["tenant_id", "batch_id", "kind", "base_name"],
        unique=True,
        schema="assay",
        postgresql_where=sa.text("status <> 'removed'"),
    )
    op.execute("ALTER TABLE assay.batch_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assay.batch_files FORCE ROW LEVEL SECURITY")
    own = "tenant_id = assay.current_tenant_id()"
    op.execute(
        "CREATE POLICY app_tenant_own_tenant ON assay.batch_files AS PERMISSIVE FOR ALL "
        f"TO app_tenant USING ({own}) WITH CHECK ({own})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON assay.batch_files TO app_tenant")

    # 3. The pipeline's two lookups.
    op.execute("CREATE SCHEMA pipeline")
    op.execute("REVOKE ALL ON SCHEMA pipeline FROM PUBLIC")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA pipeline TO {OWNER}")
    op.execute("GRANT USAGE ON SCHEMA pipeline TO app_tenant")
    op.execute(f"GRANT USAGE ON SCHEMA assay TO {OWNER}")
    for table, columns in LOOKUP_COLUMNS.items():
        op.execute(f"GRANT SELECT ({columns}) ON assay.{table} TO {OWNER}")
        op.execute(
            f"CREATE POLICY pipeline_lookup_read ON assay.{table} AS PERMISSIVE FOR SELECT "
            f"TO {OWNER} USING (true)"
        )
    op.execute(TENANT_FOR_UPLOAD)
    op.execute(TENANTS_WITH_WORK)
    for function in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO app_tenant")
        op.execute(f"ALTER FUNCTION {function} OWNER TO {OWNER}")

    # 4. The master view's step panel.
    op.execute(
        "GRANT SELECT (tenant_id, document_id, step, status, at, detail) "
        "ON assay.document_events TO platform_view_owner"
    )
    op.execute(
        "CREATE POLICY platform_view_owner_all ON assay.document_events AS PERMISSIVE "
        "FOR SELECT TO platform_view_owner USING (true)"
    )
    op.execute(INGESTION_EVENTS_VIEW)
    op.execute("REVOKE ALL ON platform.ingestion_events_v FROM PUBLIC")
    op.execute("GRANT SELECT ON platform.ingestion_events_v TO platform_reader")
    # Last: the grant above carries over to the new owner.
    op.execute("ALTER VIEW platform.ingestion_events_v OWNER TO platform_view_owner")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS platform.ingestion_events_v")
    op.execute("DROP POLICY IF EXISTS platform_view_owner_all ON assay.document_events")
    op.execute(
        "REVOKE SELECT (tenant_id, document_id, step, status, at, detail) "
        "ON assay.document_events FROM platform_view_owner"
    )
    op.execute("DROP SCHEMA IF EXISTS pipeline CASCADE")
    for table, columns in LOOKUP_COLUMNS.items():
        op.execute(f"DROP POLICY IF EXISTS pipeline_lookup_read ON assay.{table}")
        op.execute(f"REVOKE SELECT ({columns}) ON assay.{table} FROM {OWNER}")  # noqa: S608  # nosec B608
    op.execute(f"REVOKE USAGE ON SCHEMA assay FROM {OWNER}")

    op.drop_index("uq_batch_files_live_name", table_name="batch_files", schema="assay")
    op.drop_index("ix_batch_files_tenant_id_batch_id", table_name="batch_files", schema="assay")
    op.drop_index(op.f("ix_batch_files_tenant_id"), table_name="batch_files", schema="assay")
    op.drop_table("batch_files", schema="assay")

    op.drop_index("uq_documents_tenant_id_sha256_original", table_name="documents", schema="assay")
    op.drop_index("ix_documents_received_at", table_name="documents", schema="assay")
    op.drop_column("documents", "queued_at", schema="assay")
    # migrator sees no rows (forced row-level security), so discarded documents cannot be
    # changed here: the old check is restored NOT VALID and applies to new rows only.
    op.execute("ALTER TABLE assay.documents DROP CONSTRAINT ck_documents_status")
    op.execute(
        "ALTER TABLE assay.documents ADD CONSTRAINT ck_documents_status "
        f"CHECK ({DOCUMENT_STATUSES_0001}) NOT VALID"
    )
