"""SQLAlchemy 2.0 models for every table in BUILD_SPEC.md section 7.

Conventions:

- Everything lives in schema `assay`. UUID primary keys, `timestamptz` everywhere.
- Every table except `ai_models` has `tenant_id` (in `tenants` the tenant ID is `id`) and a
  forced row-level security policy; see `assay_db.security`.
- Child tables reference their parent through `(tenant_id, parent_id)`, so the database
  itself rejects a row that points at another tenant's document, part, face or user.
- Values the spec enumerates are `text` columns with a CHECK constraint, not enum types.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "assay"

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
}

# Enumerated values from the spec. The API and workers import these.
TENANT_KINDS = ("platform", "business")
TENANT_STATUSES = ("provisioning", "active", "suspended", "failed")
# assay_tss.models.IsolationMode and StorageStatus.
STORAGE_MODES = ("dedicated",)
STORAGE_STATUSES = ("active", "offboarded")
JOBS = ("ingest", "query")
USER_ROLES = ("master-admin", "master-user", "tenant-admin", "tenant-user")
USER_STATUSES = ("invited", "active", "disabled")
BATCH_STATUSES = ("open", "closed", "discarded")
DOCUMENT_STATUSES = (
    "uploaded",
    "scanning",
    "processing",
    "ready",
    "needs_review",
    "quarantined",
    "on_hold",
    "failed",
    "duplicate",
    # The document's batch was discarded, or its file removed, before it was processed
    # (owner decision, build step 6). Left out of the ingestion history unless asked for.
    "discarded",
)
# An upload in a batch (build step 6): one PDF or one companion file.
BATCH_FILE_KINDS = ("pdf", "companion")
BATCH_FILE_STATUSES = (
    "registered",  # waiting for the upload and its scan result
    "clean",  # scanned clean; size and checksum match the registration; companion valid
    "invalid",  # a companion file that scanned clean but fails validation
    "mismatch",  # stored size or checksum differs from the registration
    "infected",  # THREATS_FOUND: moved to quarantine/
    "unsupported",  # UNSUPPORTED, ACCESS_DENIED or FAILED scan: held
    "scan_timeout",  # uploaded, but no scan result arrived in time: held
    "not_uploaded",  # registered, but nothing arrived before the link expired
    "removed",  # taken out of the batch by the admin, or dropped when it closed
)
PART_METHODS = ("rule", "model")
LABEL_SOURCES = ("companion",)
TEXT_SOURCES = ("native", "ocr")
SIGNER_SOURCES = ("audit_trail", "companion")
AUDIT_CATEGORIES = ("auth", "user", "tenant", "audit")
ATTACHMENT_KINDS = (
    "paystub",
    "bank_statement",
    "tax_form",
    "id_document",
    "live_photo",
    "other",
)
FACE_KINDS = ("id_document", "live_photo")
FACE_MATCH_SCOPES = ("same_document", "other_document")
FRAUD_SCORE_KINDS = ("identity_photo", "stipulation")
REVIEW_STATES = ("open", "confirmed", "dismissed")


def one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


uuid_pk = Annotated[
    uuid.UUID,
    mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
]
uuid_col = Annotated[uuid.UUID, mapped_column(UUID(as_uuid=True))]
tstz = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]
tstz_now = Annotated[
    dt.datetime, mapped_column(DateTime(timezone=True), server_default=text("now()"))
]
json_doc = Annotated[Any, mapped_column(JSONB)]


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        str: Text(),
        int: Integer(),
        float: REAL(),
        bool: Boolean(),
        dt.date: Date(),
    }


def tenant_fk() -> Any:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.tenants.id"), nullable=False, index=True
    )


def child_of(parent: str, column: str) -> ForeignKeyConstraint:
    """`(tenant_id, column)` references `parent(tenant_id, id)`: parent and child share a tenant."""
    return ForeignKeyConstraint(
        ["tenant_id", column],
        [f"{SCHEMA}.{parent}.tenant_id", f"{SCHEMA}.{parent}.id"],
    )


# --- Platform-wide catalog -------------------------------------------------------------


class AiModel(Base):
    """The model catalog (section 7b). Platform-wide: the only table without `tenant_id`."""

    __tablename__ = "ai_models"
    __table_args__ = (
        CheckConstraint(
            "cardinality(jobs) > 0 AND jobs <@ ARRAY['ingest', 'query']::text[]", name="jobs"
        ),
        CheckConstraint("price_in >= 0 AND price_out >= 0", name="prices"),
    )

    id: Mapped[uuid_pk]
    name: Mapped[str]
    # Bedrock inference profile ID. Comes from stack config through scripts/seed.py.
    profile_id: Mapped[str] = mapped_column(unique=True)
    jobs: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Price per million tokens, in US dollars.
    price_in: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    price_out: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    status: Mapped[str]


# --- Tenants and users -----------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (one_of("kind", TENANT_KINDS), one_of("status", TENANT_STATUSES))

    # Here `id` is the tenant ID.
    id: Mapped[uuid_pk]
    name: Mapped[str]
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="provisioning")
    extraction_model: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.ai_models.id"))
    answer_model: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.ai_models.id"))
    identity_scoring_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[tstz_now]


class TenantModel(Base):
    """The catalog models available to a tenant, per job."""

    __tablename__ = "tenant_models"
    __table_args__ = (
        one_of("job", JOBS),
        UniqueConstraint("tenant_id", "model_id", "job"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.ai_models.id"))
    job: Mapped[str]
    # A master admin, who belongs to the platform tenant: a plain reference, not tenant-scoped.
    enabled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"))
    enabled_at: Mapped[tstz_now]


class TenantStorage(Base):
    """Where a tenant's data lives. Readable only by the `tss` database role."""

    __tablename__ = "tenant_storage"
    __table_args__ = (one_of("mode", STORAGE_MODES), one_of("status", STORAGE_STATUSES))

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.tenants.id"), unique=True, nullable=False
    )
    mode: Mapped[str]
    bucket: Mapped[str]
    prefix: Mapped[str | None]
    kms_key_arn: Mapped[str]
    role_arn: Mapped[str]
    vector_bucket: Mapped[str]
    vector_index: Mapped[str]
    region: Mapped[str]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    status: Mapped[str]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        one_of("role", USER_ROLES),
        one_of("status", USER_STATUSES),
        UniqueConstraint("tenant_id", "id"),
        Index("uq_users_tenant_id_email", "tenant_id", text("lower(email)"), unique=True),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    cognito_sub: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str]
    name: Mapped[str]
    role: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="invited")
    # A master admin invites a tenant's first admin, so this may cross tenants.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"))
    last_sign_in_at: Mapped[tstz | None]
    # Set when the user's MFA is reset or the user is disabled. The API refuses any token
    # from a sign-in before this time (global sign-out revokes only refresh tokens).
    sessions_revoked_at: Mapped[tstz | None]


# --- Batches and documents -------------------------------------------------------------


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        one_of("status", BATCH_STATUSES),
        UniqueConstraint("tenant_id", "id"),
        child_of("users", "opened_by"),
        # Section 10: one open batch per user.
        Index(
            "uq_batches_one_open_per_user",
            "tenant_id",
            "opened_by",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    opened_by: Mapped[uuid_col]
    opened_at: Mapped[tstz_now]
    closed_at: Mapped[tstz | None]


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        one_of("status", DOCUMENT_STATUSES),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        UniqueConstraint("tenant_id", "id"),
        child_of("batches", "batch_id"),
        child_of("users", "uploaded_by"),
        child_of("documents", "duplicate_of"),
        Index("ix_documents_tenant_id_received_at", "tenant_id", text("received_at DESC")),
        Index("ix_documents_tenant_id_batch_id", "tenant_id", "batch_id"),
        Index("ix_documents_tenant_id_sha256", "tenant_id", "sha256"),
        # The master view's history across tenants, newest first.
        Index("ix_documents_received_at", text("received_at DESC")),
        # One original per hash within a tenant. Duplicates point at it (duplicate_of), and a
        # failed original no longer counts. Backs up the fingerprint step's advisory lock.
        Index(
            "uq_documents_tenant_id_sha256_original",
            "tenant_id",
            "sha256",
            unique=True,
            postgresql_where=text(
                "sha256 IS NOT NULL AND duplicate_of IS NULL AND status <> 'failed'"
            ),
        ),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    batch_id: Mapped[uuid_col]
    uploaded_by: Mapped[uuid_col]
    file_name: Mapped[str]
    sha256: Mapped[str | None]
    s3_key: Mapped[str | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None]
    has_companion: Mapped[bool] = mapped_column(server_default=text("false"))
    application_ref: Mapped[str | None]
    loan_number: Mapped[str | None]
    cosigner_name: Mapped[str | None]
    package_created_on: Mapped[dt.date | None]
    package_completed_on: Mapped[dt.date | None]
    status: Mapped[str] = mapped_column(server_default="uploaded")
    error_code: Mapped[str | None]
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_at: Mapped[tstz_now]
    completed_at: Mapped[tstz | None]
    pipeline_version: Mapped[str | None]
    # When the document became eligible and was sent to the ingest queue (build step 6). The
    # sweeper queues it again if no execution started within a while.
    queued_at: Mapped[tstz | None]


class BatchFile(Base):
    """One upload in a batch (build step 6): a PDF or a companion file.

    `id` is the upload's opaque file ID, the last part of its inbox key; a PDF's document has
    the same ID. `file_name` is kept here only, never in an S3 key. `size_bytes` and `sha256`
    are what the browser registered; the presigned link signs both, and the scan step checks
    the stored object against them. `version_id` is the object version that scanned clean:
    every later read, hash and copy uses it.
    """

    __tablename__ = "batch_files"
    __table_args__ = (
        one_of("kind", BATCH_FILE_KINDS),
        one_of("status", BATCH_FILE_STATUSES),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("size_bytes > 0", name="size_bytes"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("s3_key"),
        child_of("batches", "batch_id"),
        child_of("documents", "document_id"),
        child_of("users", "registered_by"),
        Index("ix_batch_files_tenant_id_batch_id", "tenant_id", "batch_id"),
        # Pairing is by base name, so a batch holds one live file per kind and base name.
        Index(
            "uq_batch_files_live_name",
            "tenant_id",
            "batch_id",
            "kind",
            "base_name",
            unique=True,
            postgresql_where=text("status <> 'removed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    batch_id: Mapped[uuid_col]
    kind: Mapped[str]
    file_name: Mapped[str]
    # The name without its extension, lower-cased: how a companion finds its PDF.
    base_name: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str]
    s3_key: Mapped[str]
    content_type: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="registered")
    error_code: Mapped[str | None]
    # For an invalid companion: [{code, key}], naming keys only, never values.
    error_detail: Mapped[json_doc | None]
    # GuardDuty's scanResultStatus, as received.
    scan_result: Mapped[str | None]
    version_id: Mapped[str | None]
    scanned_at: Mapped[tstz | None]
    # The PDF's document. For a companion file, set when the batch closes and pairs it.
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    registered_by: Mapped[uuid_col]
    registered_at: Mapped[tstz_now]


class DocumentEvent(Base):
    __tablename__ = "document_events"
    __table_args__ = (
        child_of("documents", "document_id"),
        Index("ix_document_events_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    step: Mapped[str]
    status: Mapped[str]
    at: Mapped[tstz_now]
    detail: Mapped[json_doc | None]


class DocumentPart(Base):
    __tablename__ = "document_parts"
    __table_args__ = (
        one_of("method", PART_METHODS),
        CheckConstraint("start_page >= 1 AND end_page >= start_page", name="pages"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        UniqueConstraint("tenant_id", "id"),
        child_of("documents", "document_id"),
        Index("ix_document_parts_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    start_page: Mapped[int]
    end_page: Mapped[int]
    form_type: Mapped[str]
    method: Mapped[str]
    confidence: Mapped[float | None]


class DocumentLabel(Base):
    """Custom companion-file keys (section 8). Values keep their JSON type."""

    __tablename__ = "document_labels"
    __table_args__ = (
        one_of("source", LABEL_SOURCES),
        child_of("documents", "document_id"),
        UniqueConstraint("tenant_id", "document_id", "key"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    key: Mapped[str]
    value: Mapped[json_doc]
    source: Mapped[str] = mapped_column(server_default="companion")


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        one_of("text_source", TEXT_SOURCES),
        CheckConstraint("page_no >= 1", name="page_no"),
        child_of("documents", "document_id"),
        UniqueConstraint("tenant_id", "document_id", "page_no"),
        Index("ix_pages_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    page_no: Mapped[int]
    text_source: Mapped[str]
    quality_score: Mapped[float | None]
    ocr_confidence: Mapped[float | None]
    char_count: Mapped[int | None]
    search_tsv: Mapped[Any | None] = mapped_column(TSVECTOR)


class SignatureCheck(Base):
    __tablename__ = "signature_check"
    __table_args__ = (
        child_of("documents", "document_id"),
        Index("ix_signature_check_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    intact: Mapped[bool | None]
    envelope_id: Mapped[str | None]
    checked_at: Mapped[tstz_now]


class Signer(Base):
    __tablename__ = "signers"
    __table_args__ = (
        one_of("source", SIGNER_SOURCES),
        UniqueConstraint("tenant_id", "id"),
        child_of("documents", "document_id"),
        Index("ix_signers_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    name: Mapped[str | None]
    email: Mapped[str | None]
    phone: Mapped[str | None]
    location: Mapped[str | None]
    signed_at: Mapped[tstz | None]
    source: Mapped[str]


class CompanionLoanTerms(Base):
    __tablename__ = "companion_loan_terms"
    __table_args__ = (
        child_of("documents", "document_id"),
        UniqueConstraint("tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    term_months: Mapped[int | None]
    maturity_date: Mapped[dt.date | None]
    # Annual percent, for example 6.125.
    rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))


class SigningEvent(Base):
    __tablename__ = "signing_events"
    __table_args__ = (
        child_of("documents", "document_id"),
        child_of("signers", "signer_id"),
        Index("ix_signing_events_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    signer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event: Mapped[str]
    occurred_at: Mapped[tstz | None]
    ip_address: Mapped[str | None] = mapped_column(INET)
    auth_method: Mapped[str | None]


class ExtractedField(Base):
    __tablename__ = "extracted_fields"
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        child_of("document_parts", "part_id"),
        Index("ix_extracted_fields_tenant_id_part_id", "tenant_id", "part_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    part_id: Mapped[uuid_col]
    field: Mapped[str]
    value: Mapped[json_doc | None]
    page_no: Mapped[int | None]
    confidence: Mapped[float | None]
    schema_version: Mapped[str]
    model_version: Mapped[str | None]


# --- AI questions ----------------------------------------------------------------------


class ChatConversation(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        child_of("users", "user_id"),
        Index("ix_chat_conversations_tenant_id_user_id", "tenant_id", "user_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid_col]
    title: Mapped[str | None]
    created_at: Mapped[tstz_now]


class ChatAudit(Base):
    __tablename__ = "chat_audit"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        child_of("chat_conversations", "conversation_id"),
        child_of("users", "user_id"),
        Index("ix_chat_audit_tenant_id_conversation_id", "tenant_id", "conversation_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    conversation_id: Mapped[uuid_col]
    user_id: Mapped[uuid_col]
    asked_at: Mapped[tstz_now]
    retrieved_doc_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'")
    )
    log_s3_key: Mapped[str | None]
    model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.ai_models.id"))


class AiUsage(Base):
    """One row per model call. Cost is frozen at call time."""

    __tablename__ = "ai_usage"
    __table_args__ = (
        one_of("job", JOBS),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens"),
        child_of("documents", "document_id"),
        child_of("chat_audit", "chat_audit_id"),
        Index("ix_ai_usage_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    job: Mapped[str]
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.ai_models.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chat_audit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    request_id: Mapped[str | None]


# --- Audit trail (section 7a) ----------------------------------------------------------


class AuditLog(Base):
    """Append-only audit trail, partitioned by month on `at`.

    `category` is not in section 7's column list; it is section 7a's Category column, which
    `GET /admin/audit` filters on.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", "at"),
        one_of("category", AUDIT_CATEGORIES),
        Index("ix_audit_log_at", text("at DESC")),
        Index("ix_audit_log_tenant_id_at", "tenant_id", text("at DESC")),
        {"postgresql_partition_by": "RANGE (at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.tenants.id"))
    at: Mapped[tstz_now]
    # Plain values, not foreign keys: an audit insert must never fail on a reference.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_email: Mapped[str | None]
    actor_role: Mapped[str | None]
    category: Mapped[str]
    action: Mapped[str]
    target_type: Mapped[str | None]
    target_id: Mapped[str | None]
    target_label: Mapped[str | None]
    # Values before and after. Never passwords, tokens or codes.
    detail: Mapped[json_doc | None]
    outcome: Mapped[str]
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None]


# --- Stipulations and fraud scoring (section 8a) ---------------------------------------


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        one_of("kind", ATTACHMENT_KINDS),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        UniqueConstraint("tenant_id", "id"),
        child_of("documents", "document_id"),
        Index("ix_attachments_tenant_id_document_id", "tenant_id", "document_id"),
        Index("ix_attachments_tenant_id_sha256", "tenant_id", "sha256"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    kind: Mapped[str] = mapped_column(server_default="other")
    file_name: Mapped[str]
    s3_key: Mapped[str | None]
    sha256: Mapped[str | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None]
    page_count: Mapped[int | None]
    status: Mapped[str]


class Face(Base):
    __tablename__ = "faces"
    __table_args__ = (
        one_of("kind", FACE_KINDS),
        UniqueConstraint("tenant_id", "id"),
        child_of("documents", "document_id"),
        child_of("attachments", "attachment_id"),
        Index("ix_faces_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    page_no: Mapped[int | None]
    kind: Mapped[str]
    rekognition_face_id: Mapped[str | None]
    s3_key: Mapped[str]
    image_sha256: Mapped[str]


class FaceMatch(Base):
    __tablename__ = "face_matches"
    __table_args__ = (
        one_of("scope", FACE_MATCH_SCOPES),
        CheckConstraint("similarity BETWEEN 0 AND 100", name="similarity"),
        child_of("faces", "face_id"),
        child_of("faces", "matched_face_id"),
        Index("ix_face_matches_tenant_id_face_id", "tenant_id", "face_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    face_id: Mapped[uuid_col]
    matched_face_id: Mapped[uuid_col]
    similarity: Mapped[float]
    scope: Mapped[str]


class FraudScore(Base):
    __tablename__ = "fraud_scores"
    __table_args__ = (
        one_of("kind", FRAUD_SCORE_KINDS),
        one_of("review_state", REVIEW_STATES),
        CheckConstraint("score BETWEEN 0 AND 100", name="score"),
        child_of("documents", "document_id"),
        child_of("attachments", "attachment_id"),
        child_of("users", "reviewed_by"),
        Index("ix_fraud_scores_tenant_id_score", "tenant_id", "score"),
        Index("ix_fraud_scores_tenant_id_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[uuid_pk]
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    document_id: Mapped[uuid_col]
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str]
    score: Mapped[int]
    # JSON list of {code, text, points, page or attachment}.
    reasons: Mapped[json_doc] = mapped_column(server_default=text("'[]'::jsonb"))
    rules_version: Mapped[str]
    model_version: Mapped[str | None]
    scored_at: Mapped[tstz_now]
    review_state: Mapped[str] = mapped_column(server_default="open")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    review_note: Mapped[str | None]


# Every table except `ai_models` carries the tenant and has forced RLS.
TENANT_TABLES: tuple[str, ...] = tuple(
    t.name for t in Base.metadata.sorted_tables if t.name != AiModel.__tablename__
)
