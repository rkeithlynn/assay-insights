"""Attempts, companion data by version, companion replacement and quarantine notices
(build step 6, second half, stage 1 part 2).

Owner decisions of 2026-11-24 (the second-half plan) and 2026-11-25:

1. **Every event carries its attempt** (`document_events.attempt`, 1 for every existing
   row). "Process again" starts a new attempt of a document; a step asks only about its own
   attempt's events, so an earlier attempt's result never stands in for the new one's.
   `pipeline.tenants_with_work` is replaced to match: a document counts as started only if
   its current attempt has a `start` event.
2. **Old results are kept, not deleted** (decision 2: `app_tenant` has no DELETE anywhere).
   Companion data is stored by version: `signers`, `companion_loan_terms` and
   `document_labels` gain `attempt`, and `documents.companion_attempt` points at the current
   one. It moves in the transaction that writes the new rows, so a reader sees the previous
   values until then. `documents.companion_file_id` is the upload those values came from.
   `documents.content_attempt` is the same pointer for text and extraction, which later
   stages write; it is NULL until then.
3. **A companion file attached or replaced after ingestion** is a `batch_files` row with
   `purpose = 'companion_replacement'`: registered against its document, uploaded to the
   inbox and scanned like any upload. `applied_at` is when it took effect. The one-live-file
   -per-name index now covers batch uploads only, so a replacement may carry its PDF's name.
4. **Quarantine notice.** `batch_files.notice_sent_at` is when the security notice for an
   infected file was published; a redelivered scan result sends it only if it is unset.
5. **Audit categories** `security` (a file quarantined) and `document` ("Process again", a
   companion file replaced, "Mark as reviewed").

New read access, both one integer column (approved with the plan, 2026-11-24, decision 5):
- `pipeline_lookup` reads `document_events.attempt` (for `tenants_with_work`).
- `app_worker` reads `document_events.attempt` (a repeated worker step asks about its own
  attempt only). Its table-level INSERT on `document_events` already covers the new column.

No policy changes, no new role, no new table.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "pipeline_lookup"
WORKER = "app_worker"

CATEGORIES_0005 = "category IN ('auth', 'user', 'tenant', 'audit')"
CATEGORIES = "category IN ('auth', 'user', 'tenant', 'audit', 'security', 'document')"

SIGNATURE = "pipeline.tenants_with_work(timestamptz, timestamptz, timestamptz)"

TENANTS_WITH_WORK = """
CREATE OR REPLACE FUNCTION pipeline.tenants_with_work(
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
              {attempt}
       )
$$
"""

# The three tables that hold companion data, and their one-row-per-document constraints.
COMPANION_TABLES = ("signers", "companion_loan_terms", "document_labels")
UNIQUE = {
    "companion_loan_terms": (
        "uq_companion_loan_terms_tenant_id_document_id",
        ["tenant_id", "document_id"],
        "uq_companion_loan_terms_tenant_id_document_id_attempt",
        ["tenant_id", "document_id", "attempt"],
    ),
    "document_labels": (
        "uq_document_labels_tenant_id_document_id_key",
        ["tenant_id", "document_id", "key"],
        "uq_document_labels_tenant_id_document_id_attempt_key",
        ["tenant_id", "document_id", "attempt", "key"],
    ),
}


def _replace_function(attempt: str) -> None:
    # `migrator` holds SET, not INHERIT, on the owner: it switches to replace the function.
    # Owner, grants and search_path stay as migration 0008 made them.
    op.execute(f"SET ROLE {OWNER}")
    op.execute(TENANTS_WITH_WORK.format(attempt=attempt))
    op.execute("RESET ROLE")


def _attempt(table: str) -> None:
    op.add_column(
        table,
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="assay",
    )
    op.create_check_constraint(op.f(f"ck_{table}_attempt"), table, "attempt >= 1", schema="assay")


def upgrade() -> None:
    # 1. Every event carries its attempt.
    _attempt("document_events")
    op.execute(f"GRANT SELECT (attempt) ON assay.document_events TO {OWNER}, {WORKER}")
    _replace_function("AND e.attempt = d.attempt")

    # 2. Companion data by version, and the pointers.
    for table in COMPANION_TABLES:
        _attempt(table)
    for table, (old, _old_columns, new, new_columns) in UNIQUE.items():
        op.drop_constraint(op.f(old), table, schema="assay", type_="unique")
        op.create_unique_constraint(op.f(new), table, new_columns, schema="assay")
    for column in ("companion_attempt", "content_attempt"):
        op.add_column("documents", sa.Column(column, sa.Integer(), nullable=True), schema="assay")
        op.create_check_constraint(
            op.f(f"ck_documents_{column}"), "documents", f"{column} >= 1", schema="assay"
        )
    op.add_column(
        "documents", sa.Column("companion_file_id", sa.UUID(), nullable=True), schema="assay"
    )
    # No backfill: nothing wrote companion rows before this revision's code, and `migrator`
    # sees no rows anyway (forced row-level security).

    # 3. Companion replacement files, and 4. the notice mark.
    op.add_column(
        "batch_files",
        sa.Column("purpose", sa.Text(), server_default="upload", nullable=False),
        schema="assay",
    )
    op.create_check_constraint(
        op.f("ck_batch_files_purpose"),
        "batch_files",
        "purpose IN ('upload', 'companion_replacement')",
        schema="assay",
    )
    op.add_column(
        "batch_files",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        schema="assay",
    )
    op.add_column(
        "batch_files",
        sa.Column("notice_sent_at", sa.DateTime(timezone=True), nullable=True),
        schema="assay",
    )
    op.drop_index("uq_batch_files_live_name", table_name="batch_files", schema="assay")
    op.create_index(
        "uq_batch_files_live_name",
        "batch_files",
        ["tenant_id", "batch_id", "kind", "base_name"],
        unique=True,
        schema="assay",
        postgresql_where=sa.text("status <> 'removed' AND purpose = 'upload'"),
    )

    # 5. Audit categories.
    op.execute("ALTER TABLE assay.audit_log DROP CONSTRAINT ck_audit_log_category")
    op.execute(
        f"ALTER TABLE assay.audit_log ADD CONSTRAINT ck_audit_log_category CHECK ({CATEGORIES})"
    )


def downgrade() -> None:
    # `migrator` sees no rows (forced row-level security) and audit_log is append-only, so
    # nothing is deleted here. The old category check is restored NOT VALID (new rows only).
    # If replacement files or more than one version of a document's companion data exist,
    # the older unique rules below cannot be restored and the database refuses the
    # downgrade: that data has no place in the older schema.
    op.execute("ALTER TABLE assay.audit_log DROP CONSTRAINT ck_audit_log_category")
    op.execute(
        "ALTER TABLE assay.audit_log ADD CONSTRAINT ck_audit_log_category "
        f"CHECK ({CATEGORIES_0005}) NOT VALID"
    )

    op.drop_index("uq_batch_files_live_name", table_name="batch_files", schema="assay")
    op.create_index(
        "uq_batch_files_live_name",
        "batch_files",
        ["tenant_id", "batch_id", "kind", "base_name"],
        unique=True,
        schema="assay",
        postgresql_where=sa.text("status <> 'removed'"),
    )
    op.drop_column("batch_files", "notice_sent_at", schema="assay")
    op.drop_column("batch_files", "applied_at", schema="assay")
    op.drop_constraint(op.f("ck_batch_files_purpose"), "batch_files", schema="assay")
    op.drop_column("batch_files", "purpose", schema="assay")

    op.drop_column("documents", "companion_file_id", schema="assay")
    for column in ("content_attempt", "companion_attempt"):
        op.drop_constraint(op.f(f"ck_documents_{column}"), "documents", schema="assay")
        op.drop_column("documents", column, schema="assay")
    for table, (old, old_columns, new, _new_columns) in UNIQUE.items():
        op.drop_constraint(op.f(new), table, schema="assay", type_="unique")
        op.create_unique_constraint(op.f(old), table, old_columns, schema="assay")
    for table in COMPANION_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_attempt"), table, schema="assay")
        op.drop_column(table, "attempt", schema="assay")

    _replace_function("")
    op.execute(
        f"REVOKE SELECT (attempt) ON assay.document_events FROM {OWNER}, {WORKER}"  # noqa: S608  # nosec B608
    )
    op.drop_constraint(op.f("ck_document_events_attempt"), "document_events", schema="assay")
    op.drop_column("document_events", "attempt", schema="assay")
