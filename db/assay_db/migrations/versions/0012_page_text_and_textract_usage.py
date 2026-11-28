"""Page text by attempt, Textract usage, and what the worker may write for step 5
(build step 6, second half, stage 2).

Owner decisions of 2026-11-27 (the stage 2 plan):

1. **`pages` by attempt.** `pages.attempt` (the table is empty until this stage's code), with
   one row per page per attempt. Rows are read through `documents.content_attempt`, which the
   text step moves in the transaction that writes them; nothing is deleted on reprocessing.
2. **`documents.text_version_id`**: the S3 version of `derived/text/{doc}.json` that
   `content_attempt` stands for. Readers use that version, never "the latest": every version
   of the text object is kept (owner, 2026-11-27, option 1), and a late write by a
   superseded attempt must never be read.
3. **`ai_usage` for a service billed by the page.** Textract has no tokens and is not in the
   model catalog. A row now says which service made the call (`service`: `bedrock` for every
   existing kind of row, or `textract`), the `operation`, the service's own `model_version`,
   the `attempt` and `page_no` it was for, and `units`, `unit` and `unit_price` for what it
   was billed by. `model_id` becomes nullable: a check still requires it for Bedrock rows and
   forbids it for Textract rows, which must carry their units and price instead.
4. **`pipeline.bound_attempt()`**: the attempt of the live grant the session presented, like
   `bound_tenant()` and `bound_document()` (migration 0010), owned by `pipeline_lookup`.
5. **What `app_worker` may write** (each approved 2026-11-27):
   - INSERT on eight columns of `pages` and fifteen of `ai_usage`, each with a policy that
     admits only its bound tenant, document and attempt. It can read neither table.
   - UPDATE on `documents.content_attempt` and `documents.text_version_id`. A trigger holds
     it to its own attempt: when `app_worker` sets either column, `content_attempt` must be
     the attempt of its live grant and the version must not be empty. So it cannot point a
     document at another attempt's text, back or forward.
   - EXECUTE on `pipeline.bound_attempt()`.
6. **The master view's Text line.** `platform.ingestion_events_v` gains four integer columns
   at the end, for a `text` event only: `pages`, `pages_native`, `pages_ocr` and
   `pages_unread`. Counts, not content. A read widening for `platform_reader` (owner,
   2026-11-27, decision C: the unread page count shows in the master view too). Each is shown
   only if the stored value is a plain whole number, whatever wrote it.

No page text is stored in the database by this revision or its code: `pages` holds numbers
and the source of each page; the text is in the tenant's bucket.

No new role and no new table. `app_tenant` already has table-level SELECT, INSERT and UPDATE
on `pages`, `ai_usage` and `documents`, which cover the new columns.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-27 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose. Role names match assay_db.roles.
OWNER = "pipeline_lookup"
WORKER = "app_worker"
VIEW_OWNER = "platform_view_owner"
READER = "platform_reader"

# The same live-grant lookup as migration 0010's bound_tenant() and bound_document().
BOUND_ATTEMPT = """
CREATE FUNCTION pipeline.bound_attempt() RETURNS integer
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT g.attempt
    FROM assay.worker_grants g
    JOIN assay.documents d ON d.tenant_id = g.tenant_id AND d.id = g.document_id
    WHERE g.token_hash = encode(sha256(convert_to(
              split_part(current_setting('app.worker_grant', true), ':', 3), 'UTF8')), 'hex')
      AND g.document_id::text = split_part(current_setting('app.worker_grant', true), ':', 1)
      AND g.attempt::text = split_part(current_setting('app.worker_grant', true), ':', 2)
      AND g.ended_at IS NULL
      AND g.expires_at > now()
      AND d.attempt = g.attempt
$$
"""
FUNCTION = "pipeline.bound_attempt()"

# Runs as whoever makes the change (not SECURITY DEFINER). Only the worker is held to it:
# every other role that may update `documents` is one tenant's whole pipeline or API.
GUARD = f"""
CREATE FUNCTION assay.worker_content_pointer_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF current_user = '{WORKER}' THEN
        IF NEW.content_attempt IS DISTINCT FROM pipeline.bound_attempt()
           OR NEW.text_version_id IS NULL OR NEW.text_version_id = ''
        THEN
            RAISE EXCEPTION 'the content pointer may only be set for the bound attempt'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""
GUARD_FUNCTION = "assay.worker_content_pointer_guard()"
GUARD_TRIGGER = "worker_content_pointer_guard"

# app_worker: insert-only, by column, and only for its bound tenant, document and attempt.
WORKER_INSERT = {
    "pages": (
        "tenant_id, document_id, attempt, page_no, text_source, quality_score, "
        "ocr_confidence, char_count"
    ),
    "ai_usage": (
        "tenant_id, job, service, operation, model_version, document_id, attempt, page_no, "
        "input_tokens, output_tokens, units, unit, unit_price, cost, request_id"
    ),
}
WORKER_UPDATE = "content_attempt, text_version_id"
BOUND = (
    "tenant_id = (SELECT pipeline.bound_tenant()) "
    "AND document_id = (SELECT pipeline.bound_document()) "
    "AND attempt = (SELECT pipeline.bound_attempt())"
)
POLICY = "app_worker_bound_insert"

SERVICES = "service IN ('bedrock', 'textract')"
# A Bedrock row names its catalog model. A Textract row names none, and says what it was
# billed by instead. Each value is required outright: a check passes on NULL, so
# `units >= 1` alone would admit a row with no units.
SERVICE_SHAPE = (
    "(service = 'bedrock' AND model_id IS NOT NULL) OR "
    "(service = 'textract' AND model_id IS NULL AND operation IS NOT NULL "
    "AND units IS NOT NULL AND units >= 1 AND unit IS NOT NULL AND unit = 'page' "
    "AND unit_price IS NOT NULL AND unit_price >= 0)"
)
USAGE_FOR = "(attempt IS NULL OR attempt >= 1) AND (page_no IS NULL OR page_no >= 1)"

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


def _count(key: str) -> str:
    return (
        f"CASE WHEN e.step = 'text' AND e.detail ->> '{key}' ~ '^[0-9]{{1,7}}$' "
        f"THEN (e.detail ->> '{key}')::integer END AS {key}"
    )


COUNTS = ("pages", "pages_native", "pages_ocr", "pages_unread")
# The view of 0009 with the four counts added at the end. Fixed text of this module.
VIEW_0012 = VIEW_0009.replace(
    "END AS error_type\n",
    "END AS error_type,\n    " + ",\n    ".join(_count(key) for key in COUNTS) + "\n",
)
if VIEW_0012 == VIEW_0009:
    raise RuntimeError("the view of 0009 no longer ends as this revision expects")


def _replace_view(view: str) -> None:
    """As migration 0009 did it: the same owner, the same one SELECT grant, nothing for
    PUBLIC. `migrator` holds SET, not INHERIT, on the owner, so it switches only to drop."""
    op.execute(f"SET ROLE {VIEW_OWNER}")
    op.execute("DROP VIEW platform.ingestion_events_v")
    op.execute("RESET ROLE")
    op.execute(view)
    op.execute("REVOKE ALL ON platform.ingestion_events_v FROM PUBLIC")
    op.execute(f"GRANT SELECT ON platform.ingestion_events_v TO {READER}")
    op.execute(f"ALTER VIEW platform.ingestion_events_v OWNER TO {VIEW_OWNER}")


def upgrade() -> None:
    # 1. pages by attempt.
    op.add_column(
        "pages",
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="assay",
    )
    op.create_check_constraint(op.f("ck_pages_attempt"), "pages", "attempt >= 1", schema="assay")
    op.drop_constraint(
        op.f("uq_pages_tenant_id_document_id_page_no"), "pages", schema="assay", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_pages_tenant_id_document_id_attempt_page_no"),
        "pages",
        ["tenant_id", "document_id", "attempt", "page_no"],
        schema="assay",
    )

    # 2. The text object's version.
    op.add_column(
        "documents", sa.Column("text_version_id", sa.Text(), nullable=True), schema="assay"
    )

    # 3. ai_usage for a service billed by the page.
    op.add_column(
        "ai_usage",
        sa.Column("service", sa.Text(), server_default="bedrock", nullable=False),
        schema="assay",
    )
    for name, kind in (
        ("operation", sa.Text()),
        ("model_version", sa.Text()),
        ("attempt", sa.Integer()),
        ("page_no", sa.Integer()),
        ("units", sa.Integer()),
        ("unit", sa.Text()),
        ("unit_price", sa.Numeric(precision=12, scale=6)),
    ):
        op.add_column("ai_usage", sa.Column(name, kind, nullable=True), schema="assay")
    op.alter_column("ai_usage", "model_id", existing_type=sa.UUID(), nullable=True, schema="assay")
    op.create_check_constraint(op.f("ck_ai_usage_service"), "ai_usage", SERVICES, schema="assay")
    op.create_check_constraint(
        op.f("ck_ai_usage_service_shape"), "ai_usage", SERVICE_SHAPE, schema="assay"
    )
    op.create_check_constraint(op.f("ck_ai_usage_usage_for"), "ai_usage", USAGE_FOR, schema="assay")

    # 4. The bound attempt.
    op.execute(BOUND_ATTEMPT)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {WORKER}")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {OWNER}")

    # 5. What app_worker may write, and the guard on the content pointer.
    for table, columns in WORKER_INSERT.items():
        op.execute(f"GRANT INSERT ({columns}) ON assay.{table} TO {WORKER}")
        op.execute(
            f"CREATE POLICY {POLICY} ON assay.{table} AS PERMISSIVE FOR INSERT TO {WORKER} "
            f"WITH CHECK ({BOUND})"
        )
    op.execute(f"GRANT UPDATE ({WORKER_UPDATE}) ON assay.documents TO {WORKER}")
    op.execute(GUARD)
    op.execute(f"REVOKE ALL ON FUNCTION {GUARD_FUNCTION} FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER {GUARD_TRIGGER} BEFORE UPDATE OF {WORKER_UPDATE} ON assay.documents "
        f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}"
    )

    # 6. The master view's Text line.
    _replace_view(VIEW_0012)


def downgrade() -> None:
    # `migrator` sees no rows (forced row-level security), so nothing is deleted here. If a
    # document has pages for more than one attempt, or a Textract usage row exists, the older
    # rules below cannot be restored and the database refuses the downgrade: that data has no
    # place in the older schema.
    _replace_view(VIEW_0009)

    op.execute(f"DROP TRIGGER IF EXISTS {GUARD_TRIGGER} ON assay.documents")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}")
    op.execute(f"REVOKE UPDATE ({WORKER_UPDATE}) ON assay.documents FROM {WORKER}")  # nosec B608
    for table, columns in WORKER_INSERT.items():
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON assay.{table}")
        op.execute(f"REVOKE INSERT ({columns}) ON assay.{table} FROM {WORKER}")  # nosec B608

    # The function belongs to pipeline_lookup; migrator holds SET, not INHERIT, on that role.
    op.execute(f"SET ROLE {OWNER}")
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
    op.execute("RESET ROLE")

    for name in ("ck_ai_usage_usage_for", "ck_ai_usage_service_shape", "ck_ai_usage_service"):
        op.drop_constraint(op.f(name), "ai_usage", schema="assay", type_="check")
    op.alter_column("ai_usage", "model_id", existing_type=sa.UUID(), nullable=False, schema="assay")
    for name in (
        "unit_price",
        "unit",
        "units",
        "page_no",
        "attempt",
        "model_version",
        "operation",
        "service",
    ):
        op.drop_column("ai_usage", name, schema="assay")

    op.drop_column("documents", "text_version_id", schema="assay")

    op.drop_constraint(
        op.f("uq_pages_tenant_id_document_id_attempt_page_no"),
        "pages",
        schema="assay",
        type_="unique",
    )
    op.create_unique_constraint(
        op.f("uq_pages_tenant_id_document_id_page_no"),
        "pages",
        ["tenant_id", "document_id", "page_no"],
        schema="assay",
    )
    op.drop_constraint(op.f("ck_pages_attempt"), "pages", schema="assay", type_="check")
    op.drop_column("pages", "attempt", schema="assay")
