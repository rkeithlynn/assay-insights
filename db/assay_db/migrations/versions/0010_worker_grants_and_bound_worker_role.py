"""The worker's attempt grant and its bound database role (build step 6, second half, part 1).

Owner decisions of 2026-11-24 (conditions A and B of the second-half plan): the worker task
that opens tenants' PDFs must be tied to one tenant, and to one document, by something it
cannot choose for itself.

1. `documents.attempt` (1 for every existing row): which run of the pipeline is current.
2. `worker_grants`: one row per run of the worker task, written by the pipeline's `claim`
   step (as `app_tenant`) just before the task starts. It holds the SHA-256 of a random
   token, never the token. A grant is live while it has not ended, has not expired and names
   the document's current attempt. Forced row-level security; `app_tenant` gets SELECT,
   INSERT and UPDATE, as on every other tenant table.
3. Four SECURITY DEFINER functions in schema `pipeline`, owned by `pipeline_lookup`, each
   with a fixed search_path:
   - `worker_grant(document, attempt, token_hash)`, for the Tenant Storage Service (`tss`):
     the tenant of a live grant for a document that is `processing`, and the inbox keys of
     that document's clean files (IDs only). Nothing for anything else. This is how
     `/resolve-worker` learns the tenant: from this row, never from the caller.
   - `bind_worker(document, attempt, token)`, for `app_worker`: checks the grant and ties the
     session to it, or raises.
   - `bound_tenant()` and `bound_document()`: the tenant and document of the live grant the
     session presented, or NULL. `app_worker`'s policies read these. The grant is checked
     again on every use, so a session whose grant has ended sees nothing. A session cannot
     name a tenant: the only thing it can present is a token, and only a token whose hash is
     in a live grant binds it.
4. Database role `app_worker` (created by the bootstrap): the worker task signs in as it
   instead of `app_tenant`. Column-level grants on the three tables part 1's worker uses,
   and policies that confine it to the bound tenant and document:
   - `documents`: read IDs, status, attempt, hash, key, size, pages and error code (no file
     name); update status, error code, key, size, pages and completion time.
   - `batch_files`: read IDs, kind, key, version, hash, size and status (no file name).
   - `document_events`: insert; read step, status and time (not `detail`, which names who
     opened a PDF).
   It has nothing on `users`, the chat tables or `audit_log`.

New read access (each for the owner's approval, all approved 2026-11-24):
- `pipeline_lookup` reads `documents.attempt`, `batch_files.document_id` and six columns of
  `worker_grants`, with a SELECT policy on `worker_grants`.
- `tss` gets USAGE on schema `pipeline` and EXECUTE on `worker_grant` (until now it read only
  `tenant_storage`).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-25 05:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "pipeline_lookup"
WORKER = "app_worker"
TSS = "tss"

# The live grant the session presented in `app.worker_grant` (`<document>:<attempt>:<token>`).
# An unset setting gives NULL throughout, and so no row.
BOUND = """
    FROM assay.worker_grants g
    JOIN assay.documents d ON d.tenant_id = g.tenant_id AND d.id = g.document_id
    WHERE g.token_hash = encode(sha256(convert_to(
              split_part(current_setting('app.worker_grant', true), ':', 3), 'UTF8')), 'hex')
      AND g.document_id::text = split_part(current_setting('app.worker_grant', true), ':', 1)
      AND g.attempt::text = split_part(current_setting('app.worker_grant', true), ':', 2)
      AND g.ended_at IS NULL
      AND g.expires_at > now()
      AND d.attempt = g.attempt
"""

BOUND_TENANT = f"""
CREATE FUNCTION pipeline.bound_tenant() RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT g.tenant_id {BOUND}
$$
"""

BOUND_DOCUMENT = f"""
CREATE FUNCTION pipeline.bound_document() RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT g.document_id {BOUND}
$$
"""

# Session-level, so it lasts for the connection; the worker commits right after it.
BIND_WORKER = """
CREATE FUNCTION pipeline.bind_worker(p_document uuid, p_attempt integer, p_token text)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant uuid;
BEGIN
    IF p_document IS NULL OR p_attempt IS NULL OR p_token IS NULL OR position(':' in p_token) > 0
    THEN
        RAISE EXCEPTION 'worker grant refused' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config(
        'app.worker_grant', p_document::text || ':' || p_attempt::text || ':' || p_token, false
    );
    v_tenant := pipeline.bound_tenant();
    IF v_tenant IS NULL THEN
        PERFORM set_config('app.worker_grant', '', false);
        RAISE EXCEPTION 'worker grant refused' USING ERRCODE = '42501';
    END IF;
    RETURN v_tenant;
END
$$
"""

# For the TSS: a live grant, for the document's current attempt, while it is processing.
WORKER_GRANT = """
CREATE FUNCTION pipeline.worker_grant(p_document uuid, p_attempt integer, p_token_hash text)
RETURNS TABLE (tenant_id uuid, inbox_keys text[])
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT g.tenant_id,
           ARRAY(
               SELECT f.s3_key FROM assay.batch_files f
                WHERE f.tenant_id = g.tenant_id AND f.document_id = g.document_id
                  AND f.status = 'clean'
                ORDER BY f.s3_key
           )
      FROM assay.worker_grants g
      JOIN assay.documents d ON d.tenant_id = g.tenant_id AND d.id = g.document_id
     WHERE g.document_id = p_document
       AND g.attempt = p_attempt
       AND g.token_hash = p_token_hash
       AND g.ended_at IS NULL
       AND g.expires_at > now()
       AND d.attempt = g.attempt
       AND d.status = 'processing'
$$
"""

# function signature -> the one role that may execute it.
FUNCTIONS = {
    "pipeline.bound_tenant()": WORKER,
    "pipeline.bound_document()": WORKER,
    "pipeline.bind_worker(uuid, integer, text)": WORKER,
    "pipeline.worker_grant(uuid, integer, text)": TSS,
}

# What the four functions read beyond pipeline_lookup's grants of 0008.
LOOKUP_COLUMNS = {
    "documents": "attempt",
    "batch_files": "document_id",
    "worker_grants": "tenant_id, document_id, attempt, token_hash, expires_at, ended_at",
}

# app_worker: the columns part 1's worker reads and writes, and nothing else. No file name.
WORKER_SELECT = {
    "documents": (
        "id, tenant_id, batch_id, status, attempt, sha256, s3_key, size_bytes, page_count, "
        "error_code"
    ),
    "batch_files": "id, tenant_id, document_id, kind, s3_key, version_id, sha256, size_bytes, status",
    "document_events": "tenant_id, document_id, step, status, at",
}
WORKER_UPDATE = {
    "documents": "status, error_code, s3_key, size_bytes, page_count, completed_at",
}
BOUND_TENANT_SQL = "tenant_id = (SELECT pipeline.bound_tenant())"
# table -> (the column holding the document's ID, the commands its policies cover)
WORKER_POLICIES = {
    "documents": ("id", ("SELECT", "UPDATE")),
    "batch_files": ("document_id", ("SELECT",)),
    "document_events": ("document_id", ("SELECT", "INSERT")),
}


def _bound(column: str) -> str:
    return f"{BOUND_TENANT_SQL} AND {column} = (SELECT pipeline.bound_document())"


def upgrade() -> None:
    # 1. documents.attempt.
    op.add_column(
        "documents",
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="assay",
    )
    op.create_check_constraint(
        op.f("ck_documents_attempt"), "documents", "attempt >= 1", schema="assay"
    )

    # 2. worker_grants.
    op.create_table(
        "worker_grants",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_worker_grants_attempt")),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_worker_grants_token_hash")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["assay.documents.tenant_id", "assay.documents.id"],
            name=op.f("fk_worker_grants_tenant_id_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["assay.tenants.id"], name=op.f("fk_worker_grants_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_grants")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_worker_grants_token_hash")),
        schema="assay",
    )
    op.create_index(
        op.f("ix_worker_grants_tenant_id"),
        "worker_grants",
        ["tenant_id"],
        unique=False,
        schema="assay",
    )
    op.create_index(
        "ix_worker_grants_tenant_id_document_id",
        "worker_grants",
        ["tenant_id", "document_id"],
        unique=False,
        schema="assay",
    )
    op.execute("ALTER TABLE assay.worker_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assay.worker_grants FORCE ROW LEVEL SECURITY")
    own = "tenant_id = assay.current_tenant_id()"
    op.execute(
        "CREATE POLICY app_tenant_own_tenant ON assay.worker_grants AS PERMISSIVE FOR ALL "
        f"TO app_tenant USING ({own}) WITH CHECK ({own})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON assay.worker_grants TO app_tenant")

    # 3. The four functions, and what their owner reads.
    for table, columns in LOOKUP_COLUMNS.items():
        op.execute(f"GRANT SELECT ({columns}) ON assay.{table} TO {OWNER}")
    op.execute(
        "CREATE POLICY pipeline_lookup_read ON assay.worker_grants AS PERMISSIVE FOR SELECT "
        f"TO {OWNER} USING (true)"
    )
    op.execute(f"GRANT USAGE ON SCHEMA pipeline TO {WORKER}, {TSS}")
    op.execute(f"GRANT USAGE ON SCHEMA assay TO {WORKER}")
    for statement in (BOUND_TENANT, BOUND_DOCUMENT, BIND_WORKER, WORKER_GRANT):
        op.execute(statement)
    for function, caller in FUNCTIONS.items():
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {caller}")
        op.execute(f"ALTER FUNCTION {function} OWNER TO {OWNER}")

    # 4. app_worker: column-level grants, and policies bound to the grant's tenant and document.
    for table, columns in WORKER_SELECT.items():
        op.execute(f"GRANT SELECT ({columns}) ON assay.{table} TO {WORKER}")
    for table, columns in WORKER_UPDATE.items():
        op.execute(f"GRANT UPDATE ({columns}) ON assay.{table} TO {WORKER}")
    op.execute(f"GRANT INSERT ON assay.document_events TO {WORKER}")
    for table, (column, commands) in WORKER_POLICIES.items():
        bound = _bound(column)
        for command in commands:
            using = "" if command == "INSERT" else f" USING ({bound})"
            check = "" if command == "SELECT" else f" WITH CHECK ({bound})"
            op.execute(
                f"CREATE POLICY app_worker_bound_{command.lower()} ON assay.{table} "
                f"AS PERMISSIVE FOR {command} TO {WORKER}{using}{check}"
            )


def downgrade() -> None:
    for table, (_column, commands) in WORKER_POLICIES.items():
        for command in commands:
            op.execute(f"DROP POLICY IF EXISTS app_worker_bound_{command.lower()} ON assay.{table}")
    op.execute(f"REVOKE INSERT ON assay.document_events FROM {WORKER}")
    for table, columns in WORKER_UPDATE.items():
        op.execute(f"REVOKE UPDATE ({columns}) ON assay.{table} FROM {WORKER}")  # nosec B608
    for table, columns in WORKER_SELECT.items():
        op.execute(f"REVOKE SELECT ({columns}) ON assay.{table} FROM {WORKER}")  # noqa: S608  # nosec B608

    # The functions belong to pipeline_lookup; migrator holds SET, not INHERIT, on that role.
    op.execute(f"SET ROLE {OWNER}")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    op.execute("RESET ROLE")
    op.execute(f"REVOKE USAGE ON SCHEMA assay FROM {WORKER}")
    op.execute(f"REVOKE USAGE ON SCHEMA pipeline FROM {WORKER}, {TSS}")
    op.execute("DROP POLICY IF EXISTS pipeline_lookup_read ON assay.worker_grants")
    for table, columns in LOOKUP_COLUMNS.items():
        op.execute(f"REVOKE SELECT ({columns}) ON assay.{table} FROM {OWNER}")  # noqa: S608  # nosec B608

    op.drop_index(
        "ix_worker_grants_tenant_id_document_id", table_name="worker_grants", schema="assay"
    )
    op.drop_index(op.f("ix_worker_grants_tenant_id"), table_name="worker_grants", schema="assay")
    op.drop_table("worker_grants", schema="assay")
    op.drop_constraint(op.f("ck_documents_attempt"), "documents", schema="assay", type_="check")
    op.drop_column("documents", "attempt", schema="assay")
