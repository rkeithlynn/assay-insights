"""What a tenant admin may do to a document after ingestion (owner decisions 3 and 7 of the
second-half plan, build step 6). None of these is in BUILD_SPEC.md section 10; the owner
approved them on 2026-11-24.

    POST /documents/{id}/reprocess   "Process again": a new attempt of the pipeline
    POST /documents/{id}/reviewed    "Mark as reviewed": needs_review becomes ready
    POST /documents/{id}/companion   attach or replace the companion file

Tenant admins only, own tenant (`app_tenant` and row-level security: another tenant's
document is not found). No master role may use them. Each writes an audit row (category
`document`) in the request's transaction, naming the user, with no file name.

- **Process again** starts a new attempt from the upload at the version that scanned clean.
  Refused for a document that is quarantined, on hold, a duplicate or discarded, and while
  an attempt is under way. Reprocessing a `ready` document costs money (the AI calls run
  again), so the portal asks first; this route does what it is told.
- **Companion.** The file is registered against the document and uploaded with a presigned
  link to the inbox, so it is scanned like any upload. Clean and valid, it takes effect in
  the scan step, which writes a second audit row naming both files and their object
  versions. Invalid or infected, the document is unchanged.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi import status as http
from pydantic import BaseModel

from assay_api.requestinfo import request_info
from assay_api.routes.batches import (
    FileIn,
    QueueDep,
    RegisteredFile,
    StorageDep,
    UploadOut,
    enqueue,
    new_file_id,
)
from assay_api.security import TenantAdmin
from assay_api.storage import upload_link
from assay_db import audit, batches
from assay_tss import keys
from assay_tss.client import TssRequestError
from assay_tss.models import StorageAccess

router = APIRouter()
log = logging.getLogger(__name__)

CATEGORY = "document"
REPROCESS = "reprocess_requested"
REVIEWED = "marked_reviewed"
COMPANION_REQUESTED = "companion_replace_requested"
TARGET = "document"

NOT_FOUND = frozenset({"document_not_found"})
UNPROCESSABLE = frozenset(
    {"invalid_file_name", "unsupported_file_type", "file_too_large", "empty_file"}
)


class Reprocessed(BaseModel):
    document_id: uuid.UUID
    attempt: int


def _error(exc: batches.BatchError) -> HTTPException:
    if exc.code in NOT_FOUND:
        return HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail=exc.code)
    if exc.code in UNPROCESSABLE:
        return HTTPException(status_code=422, detail=exc.code)
    return HTTPException(status_code=http.HTTP_409_CONFLICT, detail=exc.code)


def _refuse_reserved(document_id: uuid.UUID) -> None:
    if keys.is_reserved(document_id):
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="document_not_found")


def _audit(
    caller: Any, request: Request, *, action: str, document_id: uuid.UUID, detail: dict[str, Any]
) -> None:
    account = caller.account
    audit.write(
        caller.conn,
        tenant_id=account.tenant_id,
        actor=audit.AuditActor(role=account.role, user_id=account.user_id, email=account.email),
        category=CATEGORY,
        action=action,
        target_type=TARGET,
        target_id=str(document_id),
        target_label=None,
        detail=detail,
        request=request_info(request),
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=Reprocessed,
    status_code=http.HTTP_202_ACCEPTED,
)
def reprocess(
    document_id: uuid.UUID,
    caller: TenantAdmin,
    queue: QueueDep,
    background: BackgroundTasks,
    request: Request,
) -> Reprocessed:
    _refuse_reserved(document_id)
    tenant_id = caller.account.tenant_id
    try:
        attempt, previous = batches.reprocess(
            caller.conn, tenant_id=tenant_id, document_id=document_id, now=dt.datetime.now(dt.UTC)
        )
    except batches.BatchError as exc:
        raise _error(exc) from None
    _audit(
        caller,
        request,
        action=REPROCESS,
        document_id=document_id,
        detail={
            "before": {"status": previous, "attempt": attempt - 1},
            "after": {"status": "processing", "attempt": attempt},
        },
    )
    # Background tasks run after the response, which is sent after this transaction commits.
    background.add_task(enqueue, queue, tenant_id, [document_id])
    return Reprocessed(document_id=document_id, attempt=attempt)


@router.post("/documents/{document_id}/reviewed", status_code=http.HTTP_204_NO_CONTENT)
def mark_reviewed(document_id: uuid.UUID, caller: TenantAdmin, request: Request) -> Response:
    _refuse_reserved(document_id)
    account = caller.account
    try:
        batches.mark_reviewed(
            caller.conn,
            tenant_id=account.tenant_id,
            document_id=document_id,
            user_id=account.user_id,
        )
    except batches.BatchError as exc:
        raise _error(exc) from None
    _audit(
        caller,
        request,
        action=REVIEWED,
        document_id=document_id,
        detail={"before": {"status": "needs_review"}, "after": {"status": "ready"}},
    )
    return Response(status_code=http.HTTP_204_NO_CONTENT)


@router.post(
    "/documents/{document_id}/companion",
    response_model=RegisteredFile,
    status_code=http.HTTP_201_CREATED,
)
def attach_companion(
    document_id: uuid.UUID, body: FileIn, caller: TenantAdmin, store: StorageDep, request: Request
) -> RegisteredFile:
    _refuse_reserved(document_id)
    tenant_id = caller.account.tenant_id
    try:
        kind, base = batches.classify(body.name)
        if kind != batches.COMPANION:
            raise batches.BatchError("unsupported_file_type")
        batches.check_size(kind, body.size_bytes)
        target = batches.replacement_target(caller.conn, document_id)
    except batches.BatchError as exc:
        raise _error(exc) from None
    try:
        ctx = store.context(tenant_id, StorageAccess.UPLOAD, f"companion-{document_id.hex[:16]}")
    except TssRequestError:
        log.exception("no upload context", extra={"tenant_id": str(tenant_id)})
        raise HTTPException(
            status_code=http.HTTP_502_BAD_GATEWAY, detail="storage_unavailable"
        ) from None
    file_id = new_file_id()
    new = batches.NewFile(
        id=file_id,
        kind=kind,
        file_name=body.name.strip(),
        base_name=base,
        size_bytes=body.size_bytes,
        sha256=body.sha256,
        # The document's own batch: the key holds IDs only, as every inbox key does.
        s3_key=ctx.inbox_key(target.batch_id, file_id),
    )
    batches.insert_replacement(
        caller.conn,
        tenant_id=tenant_id,
        batch_id=target.batch_id,
        document_id=document_id,
        user_id=caller.account.user_id,
        file=new,
    )
    _audit(
        caller,
        request,
        action=COMPANION_REQUESTED,
        document_id=document_id,
        detail={
            "before": (
                None
                if target.companion_file_id is None
                else {
                    "file_id": str(target.companion_file_id),
                    "version_id": target.companion_version_id,
                }
            ),
            "after": {"file_id": str(file_id)},
        },
    )
    link = upload_link(
        ctx,
        new.s3_key,
        content_type=new.content_type,
        size_bytes=new.size_bytes,
        sha256_hex=new.sha256,
    )
    return RegisteredFile(
        id=file_id,
        name=new.file_name,
        kind="companion",
        upload=UploadOut(url=link.url, headers=link.headers, expires_at=link.expires_at),
    )
