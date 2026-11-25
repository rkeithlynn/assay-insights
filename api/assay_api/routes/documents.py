"""Ingestion history, document detail and the PDF view link: tenant-admin and tenant-user,
own tenant (BUILD_SPEC.md sections 10 and 11).

    GET /documents                  ingestion history: from, to, status, batch_id, sort, page,
                                    format=csv
    GET /documents/{id}             detail: pipeline steps (each with its attempt), forms
                                    found, the companion file's values and its uploads
    GET /documents/{id}/pdf-url     a five-minute link to the original PDF

Every route runs as `app_tenant` in the token's tenant: another tenant's document, guessed
ID or not, is not found.

The view link (build step 6, condition f) is signed only for a document whose PDF is in
`originals/` (never `inbox/` or `quarantine/`), with a READ context, which reaches nothing
else. S3 serves it as `application/pdf` whatever was uploaded. Each link writes a `viewed`
row to `document_events` with the user, IP address and browser, in the same transaction.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi import status as http
from pydantic import BaseModel
from sqlalchemy import Connection, text

from assay_api import ingestion_read
from assay_api.requestinfo import request_info
from assay_api.routes.batches import FileProblem, StorageDep
from assay_api.security import TENANT_ADMIN, TenantMember
from assay_api.storage import view_link
from assay_db import audit, batches
from assay_db.models import DOCUMENT_STATUSES
from assay_schemas.pipeline import visible_events
from assay_tss import keys
from assay_tss.client import TssRequestError
from assay_tss.models import StorageAccess

router = APIRouter()
log = logging.getLogger(__name__)

DocumentStatus = Literal[
    "uploaded",
    "scanning",
    "processing",
    "ready",
    "needs_review",
    "quarantined",
    "on_hold",
    "failed",
    "duplicate",
    "discarded",
]
if set(get_args(DocumentStatus)) != set(DOCUMENT_STATUSES):
    raise RuntimeError("DocumentStatus must list every document status")
Sort = Literal[
    "received",
    "-received",
    "batch",
    "-batch",
    "file_name",
    "-file_name",
    "pages",
    "-pages",
    "size",
    "-size",
    "companion",
    "-companion",
    "status",
    "-status",
    "forms",
    "-forms",
    "duration",
    "-duration",
    "uploaded_by",
    "-uploaded_by",
]
if set(get_args(Sort)) != set(ingestion_read.TENANT_SORTS):
    raise RuntimeError("Sort must list every tenant sort order")

# A PDF can be viewed only once it is in originals/, and never once discarded.
NOT_VIEWABLE = frozenset({"uploaded", "scanning", "quarantined", "discarded"})


class DocumentRow(BaseModel):
    id: uuid.UUID
    received_at: dt.datetime
    batch_id: uuid.UUID
    batch_name: str | None
    file_name: str
    page_count: int | None
    size_bytes: int | None
    has_companion: bool
    status: DocumentStatus
    error_code: str | None
    forms_found: int
    completed_at: dt.datetime | None
    duration_seconds: int | None
    uploaded_by: str


class DocumentPage(BaseModel):
    documents: list[DocumentRow]
    total: int
    page: int
    page_size: int
    start: dt.datetime
    end: dt.datetime


class DocumentEventOut(BaseModel):
    step: str
    status: str
    at: dt.datetime
    error_code: str | None
    # Who opened the PDF (step `viewed` only), for tenant admins only; null for tenant users.
    viewer: str | None
    # Which run of the pipeline the event belongs to ("Process again" starts a new one).
    attempt: int


class DocumentPartOut(BaseModel):
    start_page: int
    end_page: int
    form_type: str
    method: str
    confidence: float | None


class CompanionSignerOut(BaseModel):
    name: str | None
    email: str | None
    phone: str | None
    location: str | None


class CompanionLabelOut(BaseModel):
    key: str
    value: str | float | bool


class CompanionValues(BaseModel):
    """The companion file's current values (step 9). Tenant content: tenant routes only."""

    version: int
    application_ref: str | None
    loan_number: str | None
    cosigner_name: str | None
    package_created_on: dt.date | None
    package_completed_on: dt.date | None
    loan_term_months: int | None
    loan_maturity_date: dt.date | None
    loan_interest_rate: float | None
    signers: list[CompanionSignerOut]
    labels: list[CompanionLabelOut]


class CompanionFileOut(BaseModel):
    """One companion upload of the document: the batch's, or one attached later."""

    id: uuid.UUID
    name: str
    status: str
    error_code: str | None
    errors: list[FileProblem]
    replacement: bool
    registered_at: dt.datetime
    applied_at: dt.datetime | None
    # The file the current values came from.
    current: bool


class DocumentDetail(DocumentRow):
    duplicate_of: uuid.UUID | None
    pdf_available: bool
    attempt: int
    events: list[DocumentEventOut]
    parts: list[DocumentPartOut]
    companion: CompanionValues | None
    companion_files: list[CompanionFileOut]


class PdfLink(BaseModel):
    url: str
    expires_at: dt.datetime


def _not_found() -> HTTPException:
    return HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="document_not_found")


def _row(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "received_at": r.received_at,
        "batch_id": r.batch_id,
        "batch_name": r.batch_name,
        "file_name": r.file_name,
        "page_count": r.page_count,
        "size_bytes": r.size_bytes,
        "has_companion": r.has_companion,
        "status": r.status,
        "error_code": r.error_code,
        "forms_found": int(r.forms_found),
        "completed_at": r.completed_at,
        "duration_seconds": ingestion_read.duration_seconds(r),
        "uploaded_by": r.uploaded_by,
    }


@router.get(
    "/documents",
    response_model=DocumentPage,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def list_documents(
    caller: TenantMember,
    request: Request,
    start: Annotated[dt.datetime | None, Query(alias="from")] = None,
    end: Annotated[dt.datetime | None, Query(alias="to")] = None,
    status: Annotated[list[DocumentStatus] | None, Query()] = None,
    batch_id: uuid.UUID | None = None,
    sort: Sort = "-received",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=ingestion_read.MAX_PAGE_SIZE)
    ] = ingestion_read.PAGE_SIZE,
    format: Literal["json", "csv"] = "json",
) -> Any:
    try:
        start_at, end_at = ingestion_read.resolve_range(start, end, dt.datetime.now(dt.UTC))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    filters = ingestion_read.Filters(
        start=start_at, end=end_at, statuses=tuple(status or ()), batch_id=batch_id
    )
    if format == "csv":
        _, rows = ingestion_read.tenant_page(
            caller.conn, filters, sort=sort, number=1, size=ingestion_read.CSV_MAX_ROWS
        )
        # Every export is audited (owner decision, 2026-11-24), in the request's transaction as
        # `app_tenant` (INSERT only), which commits before the file is sent: no file leaves
        # without its row.
        account = caller.account
        target_type, target_label = ingestion_read.TENANT_EXPORT_TARGET
        audit.write(
            caller.conn,
            tenant_id=account.tenant_id,
            actor=audit.AuditActor(role=account.role, user_id=account.user_id, email=account.email),
            category=ingestion_read.EXPORT_CATEGORY,
            action=ingestion_read.EXPORT_ACTION,
            target_type=target_type,
            target_id=None,
            target_label=target_label,
            detail={
                "filters": filters.as_detail(),
                "sort": sort,
                "rows": len(rows),
                "limit": ingestion_read.CSV_MAX_ROWS,
            },
            request=request_info(request),
        )
        name = f"ingestion-{start_at:%Y%m%d}-{end_at:%Y%m%d}.csv"
        return Response(
            content=ingestion_read.tenant_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Row-Count": str(len(rows)),
                "Cache-Control": "no-store",
            },
        )
    total, rows = ingestion_read.tenant_page(
        caller.conn, filters, sort=sort, number=page, size=page_size
    )
    return DocumentPage(
        documents=[DocumentRow(**_row(r)) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        start=start_at,
        end=end_at,
    )


def _document(caller: Any, document_id: uuid.UUID) -> Any:
    if keys.is_reserved(document_id):
        raise _not_found()
    row = caller.conn.execute(
        text(
            ingestion_read.TENANT_SELECT
            + ", d.duplicate_of, d.s3_key, d.attempt, d.companion_attempt, "
            "d.companion_file_id, d.application_ref, d.loan_number, d.cosigner_name, "
            "d.package_created_on, d.package_completed_on "
            + ingestion_read.TENANT_FROM
            + "WHERE d.id = :id"
        ),
        {"id": document_id},
    ).one_or_none()
    if row is None:
        raise _not_found()
    return row


def _pdf_available(row: Any) -> bool:
    return (
        row.status not in NOT_VIEWABLE
        and row.s3_key is not None
        and row.s3_key.startswith(keys.ORIGINALS_PREFIX)
    )


def _companion(conn: Connection, row: Any) -> CompanionValues | None:
    """The values of the companion data version the document points at, and no other."""
    if row.companion_attempt is None:
        return None
    keys_ = {"id": row.id, "attempt": row.companion_attempt}
    signers = conn.execute(
        text(
            "SELECT name, email, phone, location FROM assay.signers WHERE document_id = :id "
            "AND source = 'companion' AND attempt = :attempt ORDER BY name NULLS LAST, email"
        ),
        keys_,
    ).all()
    terms = conn.execute(
        text(
            "SELECT term_months, maturity_date, rate FROM assay.companion_loan_terms "
            "WHERE document_id = :id AND attempt = :attempt"
        ),
        keys_,
    ).one_or_none()
    labels = conn.execute(
        text(
            "SELECT key, value FROM assay.document_labels WHERE document_id = :id "
            "AND attempt = :attempt ORDER BY key"
        ),
        keys_,
    ).all()
    return CompanionValues(
        version=row.companion_attempt,
        application_ref=row.application_ref,
        loan_number=row.loan_number,
        cosigner_name=row.cosigner_name,
        package_created_on=row.package_created_on,
        package_completed_on=row.package_completed_on,
        loan_term_months=terms.term_months if terms else None,
        loan_maturity_date=terms.maturity_date if terms else None,
        loan_interest_rate=float(terms.rate) if terms and terms.rate is not None else None,
        signers=[
            CompanionSignerOut(name=s.name, email=s.email, phone=s.phone, location=s.location)
            for s in signers
        ],
        labels=[CompanionLabelOut(key=label.key, value=label.value) for label in labels],
    )


def _companion_files(conn: Connection, row: Any) -> list[CompanionFileOut]:
    files = conn.execute(
        text(
            "SELECT id, file_name, status, error_code, error_detail, purpose, registered_at, "
            "applied_at FROM assay.batch_files WHERE document_id = :id AND kind = 'companion' "
            "AND status <> 'removed' ORDER BY registered_at DESC, id"
        ),
        {"id": row.id},
    ).all()
    return [
        CompanionFileOut(
            id=f.id,
            name=f.file_name,
            status=f.status,
            error_code=f.error_code,
            errors=[FileProblem(**e) for e in (f.error_detail or [])],
            replacement=f.purpose == batches.REPLACEMENT,
            registered_at=f.registered_at,
            applied_at=f.applied_at,
            current=f.id == row.companion_file_id,
        )
        for f in files
    ]


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: uuid.UUID, caller: TenantMember) -> DocumentDetail:
    row = _document(caller, document_id)
    # A step that is finished shows its result, not also the mark that it had started.
    events = visible_events(
        caller.conn.execute(
            text(
                "SELECT step, status, at, attempt, detail ->> 'error' AS error_code, "
                "detail ->> 'viewer' AS viewer FROM assay.document_events "
                "WHERE document_id = :id ORDER BY at, id"
            ),
            {"id": document_id},
        ).all()
    )
    parts = caller.conn.execute(
        text(
            "SELECT start_page, end_page, form_type, method, confidence "
            "FROM assay.document_parts WHERE document_id = :id ORDER BY start_page, end_page"
        ),
        {"id": document_id},
    ).all()
    # Who opened a PDF is for tenant admins only (owner decision, 2026-11-24).
    show_viewer = caller.account.role == TENANT_ADMIN
    return DocumentDetail(
        **_row(row),
        duplicate_of=row.duplicate_of,
        pdf_available=_pdf_available(row),
        attempt=row.attempt,
        companion=_companion(caller.conn, row),
        companion_files=_companion_files(caller.conn, row),
        events=[
            DocumentEventOut(
                step=e.step,
                status=e.status,
                at=e.at,
                error_code=e.error_code,
                viewer=e.viewer if show_viewer and e.step == "viewed" else None,
                attempt=e.attempt,
            )
            for e in events
        ],
        parts=[
            DocumentPartOut(
                start_page=p.start_page,
                end_page=p.end_page,
                form_type=p.form_type,
                method=p.method,
                confidence=p.confidence,
            )
            for p in parts
        ],
    )


@router.get("/documents/{document_id}/pdf-url", response_model=PdfLink)
def pdf_url(
    document_id: uuid.UUID, caller: TenantMember, store: StorageDep, request: Request
) -> PdfLink:
    row = _document(caller, document_id)
    if not _pdf_available(row):
        raise HTTPException(status_code=http.HTTP_409_CONFLICT, detail="pdf_not_available")
    tenant_id = caller.account.tenant_id
    try:
        ctx = store.context(tenant_id, StorageAccess.READ, f"view-{document_id.hex[:16]}")
    except TssRequestError:
        log.exception("no read context", extra={"tenant_id": str(tenant_id)})
        raise HTTPException(
            status_code=http.HTTP_502_BAD_GATEWAY, detail="storage_unavailable"
        ) from None
    # The key comes from the TSS context's builder, never from the stored string.
    key = ctx.original_key(document_id)
    if key != row.s3_key:
        raise HTTPException(status_code=http.HTTP_409_CONFLICT, detail="pdf_not_available")
    link = view_link(ctx, key, file_name=row.file_name)
    info = request_info(request)
    account = caller.account
    batches.document_event(
        caller.conn,
        tenant_id=tenant_id,
        document_id=document_id,
        step="viewed",
        status="pdf_url",
        detail={
            "viewer": account.email,
            "user_id": str(account.user_id),
            "ip_address": info.ip_address,
            "user_agent": info.user_agent,
            "expires_at": link.expires_at.isoformat(),
        },
    )
    return PdfLink(url=link.url, expires_at=link.expires_at)
