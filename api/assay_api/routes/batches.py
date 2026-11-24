"""Batches and uploads: tenant-admin, own tenant (BUILD_SPEC.md sections 8 and 10).

    POST   /batches                        open a batch (one open batch per user)
    GET    /batches                        the tenant's batches (also for the history filter)
    GET    /batches/{id}                   a batch with its files and why it cannot close yet
    POST   /batches/{id}/files             register files; a presigned upload link for each
    DELETE /batches/{id}/files/{file_id}   take a file out of an open batch
    POST   /batches/{id}/close             close it; eligible PDFs are queued
    DELETE /batches/{id}                   discard it

Every route runs as `app_tenant` in the token's tenant, so a batch or file of another tenant
is simply not found. Only the admin who opened a batch may change it. Every change takes
the batch's row lock first, so a close, a scan result and the sweeper never interleave.

Upload integrity (build step 6, condition a): each file is registered with its size and
SHA-256, both checked against the limits here, and both are signed into its upload link, so
S3 accepts only the registered content. Each file gets an opaque ID, its inbox key is built
through the TSS context from that ID, and its name stays in the database. The API never
reads an uploaded object: the scan step does, with pipeline credentials (condition c).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi import status as http
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from assay_api.security import Caller, TenantAdmin, TenantMember
from assay_api.storage import IngestQueue, Storage, upload_link
from assay_db import batches
from assay_schemas.pipeline import IngestJob
from assay_tss import keys
from assay_tss.client import TssRequestError
from assay_tss.models import StorageAccess

router = APIRouter()
log = logging.getLogger(__name__)

BatchStatus = Literal["open", "closed", "discarded"]
FileKind = Literal["pdf", "companion"]


def storage(request: Request) -> Storage:
    value: Storage | None = request.app.state.storage
    if value is None:
        raise HTTPException(status_code=http.HTTP_503_SERVICE_UNAVAILABLE, detail="no_storage")
    return value


def ingest_queue(request: Request) -> IngestQueue:
    value: IngestQueue | None = request.app.state.ingest
    if value is None:
        raise HTTPException(status_code=http.HTTP_503_SERVICE_UNAVAILABLE, detail="no_queue")
    return value


StorageDep = Annotated[Storage, Depends(storage)]
QueueDep = Annotated[IngestQueue, Depends(ingest_queue)]


# --- Models -----------------------------------------------------------------------------


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=batches.MAX_BATCH_NAME)


class FileProblem(BaseModel):
    code: str
    key: str | None = None


class FileOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: FileKind
    size_bytes: int
    status: str
    error_code: str | None
    # For an invalid companion file: what is wrong, by key. Never a value from the file.
    errors: list[FileProblem]
    # A PDF with a companion file of the same base name, or a companion with its PDF.
    matched: bool
    document_id: uuid.UUID | None
    registered_at: dt.datetime
    scanned_at: dt.datetime | None


class BatchProblem(BaseModel):
    file_id: uuid.UUID | None
    code: str


class BatchSummary(BaseModel):
    id: uuid.UUID
    name: str | None
    status: BatchStatus
    opened_by: str
    mine: bool
    opened_at: dt.datetime
    closed_at: dt.datetime | None
    pdf_count: int


class BatchList(BaseModel):
    batches: list[BatchSummary]


class BatchOut(BatchSummary):
    files: list[FileOut]
    # Why the batch cannot close yet (an open batch only).
    problems: list[BatchProblem]
    # When an open batch closes by itself (section 10: after 24 hours).
    closes_by: dt.datetime | None


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=batches.MAX_FILE_NAME)
    size_bytes: int = Field(gt=0, le=batches.PDF_MAX_BYTES)
    # Hex SHA-256 of the file's content, computed by the browser. Signed into the link.
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("sha256")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()


class FilesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[FileIn] = Field(min_length=1, max_length=batches.MAX_FILES_PER_REQUEST)


class UploadOut(BaseModel):
    url: str
    method: Literal["PUT"] = "PUT"
    # Send exactly these headers with the body; the link is signed for them.
    headers: dict[str, str]
    expires_at: dt.datetime


class RegisteredFile(BaseModel):
    id: uuid.UUID
    name: str
    kind: FileKind
    upload: UploadOut


class RegisteredFiles(BaseModel):
    files: list[RegisteredFile]


# --- Helpers ----------------------------------------------------------------------------


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code)


def _batch_error(exc: batches.BatchError) -> HTTPException:
    if exc.code in {"batch_not_found", "file_not_found"}:
        return _error(http.HTTP_404_NOT_FOUND, exc.code)
    if exc.code in {"invalid_file_name", "unsupported_file_type", "file_too_large", "empty_file"}:
        return _error(422, exc.code)
    return _error(http.HTTP_409_CONFLICT, exc.code)


def _owned_open_batch(caller: Caller, batch_id: uuid.UUID) -> Any:
    """The caller's own open batch, locked."""
    batch = batches.lock_batch(caller.conn, batch_id)
    if batch is None:
        raise _error(http.HTTP_404_NOT_FOUND, "batch_not_found")
    if batch.opened_by != caller.account.user_id:
        raise _error(http.HTTP_403_FORBIDDEN, "not_batch_owner")
    if batch.status != "open":
        raise _error(http.HTTP_409_CONFLICT, "batch_not_open")
    return batch


def _summary_query(where: str) -> str:
    # Fixed fragments only; every value is a bound parameter.
    return (
        "SELECT b.id, b.name, b.status, b.opened_by, u.name AS opened_by_name, b.opened_at, "  # noqa: S608
        "b.closed_at, (SELECT count(*) FROM assay.batch_files f WHERE f.batch_id = b.id "
        "AND f.kind = 'pdf' AND f.status <> 'removed') AS pdf_count "
        "FROM assay.batches b JOIN assay.users u ON u.tenant_id = b.tenant_id "
        f"AND u.id = b.opened_by WHERE {where}"  # nosec B608
    )


def _summary(row: Any, user_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "opened_by": row.opened_by_name,
        "mine": row.opened_by == user_id,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "pdf_count": int(row.pdf_count),
    }


def _batch_out(conn: Connection, batch_id: uuid.UUID, user_id: uuid.UUID) -> BatchOut:
    row = conn.execute(text(_summary_query("b.id = :id")), {"id": batch_id}).one_or_none()
    if row is None:
        raise _error(http.HTTP_404_NOT_FOUND, "batch_not_found")
    files = conn.execute(
        text(
            "SELECT id, file_name, kind, base_name, size_bytes, status, error_code, "
            "error_detail, document_id, registered_at, scanned_at FROM assay.batch_files "
            "WHERE batch_id = :id AND status <> 'removed' ORDER BY registered_at, file_name"
        ),
        {"id": batch_id},
    ).all()
    names = {(f.kind, f.base_name) for f in files}
    problems = (
        [
            BatchProblem(file_id=p.file_id, code=p.code)
            for p in batches.close_problems(conn, batch_id)
        ]
        if row.status == "open"
        else []
    )
    return BatchOut(
        **_summary(row, user_id),
        files=[
            FileOut(
                id=f.id,
                name=f.file_name,
                kind=f.kind,
                size_bytes=f.size_bytes,
                status=f.status,
                error_code=f.error_code,
                errors=[FileProblem(**e) for e in (f.error_detail or [])],
                matched=(("companion" if f.kind == "pdf" else "pdf", f.base_name) in names),
                document_id=f.document_id,
                registered_at=f.registered_at,
                scanned_at=f.scanned_at,
            )
            for f in files
        ],
        problems=problems,
        closes_by=row.opened_at + batches.OPEN_LIMIT if row.status == "open" else None,
    )


def enqueue(queue: IngestQueue, tenant_id: uuid.UUID, document_ids: Sequence[uuid.UUID]) -> None:
    """Runs after the response, so after the commit. A failure is logged; the sweeper queues
    the documents again (assay_db.batches.REQUEUE_AFTER)."""
    if not document_ids:
        return
    jobs = [IngestJob(tenant_id=tenant_id, document_id=d) for d in document_ids]
    try:
        queue.send(jobs)
    except Exception:
        log.exception(
            "ingest jobs not queued; the sweeper will queue them again",
            extra={"tenant_id": str(tenant_id), "documents": len(jobs)},
        )


def new_file_id() -> uuid.UUID:
    """A fresh opaque ID, never the isolation tests' reserved document ID."""
    while True:
        candidate = uuid.uuid4()
        if not keys.is_reserved(candidate):
            return candidate


# --- Routes -----------------------------------------------------------------------------


@router.post("/batches", response_model=BatchOut, status_code=http.HTTP_201_CREATED)
def open_batch(body: BatchIn, caller: TenantAdmin) -> BatchOut:
    name = " ".join((body.name or "").split()) or None
    try:
        batch_id = batches.open_batch(
            caller.conn,
            tenant_id=caller.account.tenant_id,
            user_id=caller.account.user_id,
            name=name,
        )
    except batches.BatchError as exc:
        raise _batch_error(exc) from None
    except IntegrityError:
        raise _error(http.HTTP_409_CONFLICT, "batch_already_open") from None
    return _batch_out(caller.conn, batch_id, caller.account.user_id)


@router.get("/batches", response_model=BatchList)
def list_batches(
    caller: TenantMember,
    status: BatchStatus | None = None,
    mine: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> BatchList:
    clauses, params = ["true"], {"limit": limit, "user": caller.account.user_id}
    if status is not None:
        clauses.append("b.status = :status")
        params["status"] = status
    if mine:
        clauses.append("b.opened_by = :user")
    rows = caller.conn.execute(
        text(_summary_query(" AND ".join(clauses)) + " ORDER BY b.opened_at DESC LIMIT :limit"),
        params,
    ).all()
    return BatchList(batches=[BatchSummary(**_summary(r, caller.account.user_id)) for r in rows])


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: uuid.UUID, caller: TenantAdmin) -> BatchOut:
    return _batch_out(caller.conn, batch_id, caller.account.user_id)


@router.post(
    "/batches/{batch_id}/files", response_model=RegisteredFiles, status_code=http.HTTP_201_CREATED
)
def register_files(
    batch_id: uuid.UUID, body: FilesIn, caller: TenantAdmin, store: StorageDep
) -> RegisteredFiles:
    _owned_open_batch(caller, batch_id)
    try:
        classified = [batches.classify(f.name) for f in body.files]
        for (kind, _), f in zip(classified, body.files, strict=True):
            batches.check_size(kind, f.size_bytes)
        batches.check_new_files(caller.conn, batch_id, classified)
    except batches.BatchError as exc:
        raise _batch_error(exc) from None

    tenant_id = caller.account.tenant_id
    try:
        ctx = store.context(tenant_id, StorageAccess.UPLOAD, f"upload-{batch_id.hex[:16]}")
    except TssRequestError:
        log.exception("no upload context", extra={"tenant_id": str(tenant_id)})
        raise _error(http.HTTP_502_BAD_GATEWAY, "storage_unavailable") from None

    new = []
    for (kind, base), f in zip(classified, body.files, strict=True):
        file_id = new_file_id()
        new.append(
            batches.NewFile(
                id=file_id,
                kind=kind,
                file_name=f.name.strip(),
                base_name=base,
                size_bytes=f.size_bytes,
                sha256=f.sha256,
                s3_key=ctx.inbox_key(batch_id, file_id),
            )
        )
    batches.insert_files(
        caller.conn,
        tenant_id=tenant_id,
        batch_id=batch_id,
        user_id=caller.account.user_id,
        files=new,
    )
    out = []
    for registered in new:
        link = upload_link(
            ctx,
            registered.s3_key,
            content_type=registered.content_type,
            size_bytes=registered.size_bytes,
            sha256_hex=registered.sha256,
        )
        out.append(
            RegisteredFile(
                id=registered.id,
                name=registered.file_name,
                kind="pdf" if registered.kind == batches.PDF else "companion",
                upload=UploadOut(url=link.url, headers=link.headers, expires_at=link.expires_at),
            )
        )
    return RegisteredFiles(files=out)


@router.delete("/batches/{batch_id}/files/{file_id}", status_code=http.HTTP_204_NO_CONTENT)
def remove_file(batch_id: uuid.UUID, file_id: uuid.UUID, caller: TenantAdmin) -> Response:
    _owned_open_batch(caller, batch_id)
    try:
        batches.remove_file(caller.conn, batch_id, file_id)
    except batches.BatchError as exc:
        raise _batch_error(exc) from None
    return Response(status_code=http.HTTP_204_NO_CONTENT)


@router.post("/batches/{batch_id}/close", response_model=BatchOut)
def close_batch(
    batch_id: uuid.UUID, caller: TenantAdmin, queue: QueueDep, background: BackgroundTasks
) -> BatchOut:
    """Refused with 409 `batch_not_ready` while anything blocks it; GET the batch to see what."""
    _owned_open_batch(caller, batch_id)
    try:
        eligible = batches.close(caller.conn, batch_id)
    except batches.BatchError as exc:
        raise _batch_error(exc) from None
    # Background tasks run after the response, which is sent after this transaction commits.
    background.add_task(enqueue, queue, caller.account.tenant_id, eligible)
    return _batch_out(caller.conn, batch_id, caller.account.user_id)


@router.delete("/batches/{batch_id}", status_code=http.HTTP_204_NO_CONTENT)
def discard_batch(batch_id: uuid.UUID, caller: TenantAdmin) -> Response:
    _owned_open_batch(caller, batch_id)
    try:
        batches.discard(caller.conn, batch_id)
    except batches.BatchError as exc:
        raise _batch_error(exc) from None
    return Response(status_code=http.HTTP_204_NO_CONTENT)
