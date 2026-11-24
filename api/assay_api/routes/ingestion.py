"""GET /admin/ingestion: ingestion history across tenants, metadata only (BUILD_SPEC.md 5, 10).

Both master roles. Read in the account check's read-only `platform_reader` transaction,
through `platform.platform_ingestion_v` and `platform.ingestion_events_v` only: no file name,
no batch name, no uploader, no page text and no score. Same filters as the tenant view plus
`tenant_id`; `discarded` documents are left out unless asked for.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import status as http
from pydantic import BaseModel
from sqlalchemy import text

from assay_api import ingestion_read
from assay_api.routes.documents import DocumentStatus
from assay_api.security import PlatformMember

router = APIRouter(prefix="/admin")

Sort = Literal[
    "received",
    "-received",
    "tenant",
    "-tenant",
    "batch",
    "-batch",
    "document",
    "-document",
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
]
if set(get_args(Sort)) != set(ingestion_read.MASTER_SORTS):
    raise RuntimeError("Sort must list every master sort order")


class IngestionRow(BaseModel):
    document_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    batch_id: uuid.UUID
    status: DocumentStatus
    error_code: str | None
    page_count: int | None
    size_bytes: int | None
    has_companion: bool
    forms_found: int
    received_at: dt.datetime
    completed_at: dt.datetime | None
    duration_seconds: int | None


class IngestionPage(BaseModel):
    documents: list[IngestionRow]
    total: int
    page: int
    page_size: int
    start: dt.datetime
    end: dt.datetime


class IngestionEvent(BaseModel):
    step: str
    status: str
    at: dt.datetime
    error_code: str | None


class IngestionDetail(BaseModel):
    document_id: uuid.UUID
    events: list[IngestionEvent]


def _row(r: Any) -> IngestionRow:
    return IngestionRow(
        document_id=r.document_id,
        tenant_id=r.tenant_id,
        tenant_name=r.tenant_name,
        batch_id=r.batch_id,
        status=r.status,
        error_code=r.error_code,
        page_count=r.page_count,
        size_bytes=r.size_bytes,
        has_companion=r.has_companion,
        forms_found=int(r.part_count),
        received_at=r.received_at,
        completed_at=r.completed_at,
        duration_seconds=ingestion_read.duration_seconds(r),
    )


@router.get(
    "/ingestion",
    response_model=IngestionPage,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def ingestion_history(
    caller: PlatformMember,
    start: Annotated[dt.datetime | None, Query(alias="from")] = None,
    end: Annotated[dt.datetime | None, Query(alias="to")] = None,
    tenant_id: uuid.UUID | None = None,
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
        start=start_at,
        end=end_at,
        statuses=tuple(status or ()),
        batch_id=batch_id,
        tenant_id=tenant_id,
    )
    if format == "csv":
        _, rows = ingestion_read.master_page(
            caller.conn, filters, sort=sort, number=1, size=ingestion_read.CSV_MAX_ROWS
        )
        name = f"ingestion-all-tenants-{start_at:%Y%m%d}-{end_at:%Y%m%d}.csv"
        return Response(
            content=ingestion_read.master_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Row-Count": str(len(rows)),
                "Cache-Control": "no-store",
            },
        )
    total, rows = ingestion_read.master_page(
        caller.conn, filters, sort=sort, number=page, size=page_size
    )
    return IngestionPage(
        documents=[_row(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        start=start_at,
        end=end_at,
    )


@router.get("/ingestion/{document_id}", response_model=IngestionDetail)
def ingestion_detail(document_id: uuid.UUID, caller: PlatformMember) -> IngestionDetail:
    exists = caller.conn.execute(
        text("SELECT 1 FROM platform.platform_ingestion_v WHERE document_id = :id"),
        {"id": document_id},
    ).first()
    if exists is None:
        raise HTTPException(status_code=http.HTTP_404_NOT_FOUND, detail="document_not_found")
    events = caller.conn.execute(
        text(
            "SELECT step, status, at, error_code FROM platform.ingestion_events_v "
            "WHERE document_id = :id ORDER BY at"
        ),
        {"id": document_id},
    ).all()
    return IngestionDetail(
        document_id=document_id,
        events=[
            IngestionEvent(step=e.step, status=e.status, at=e.at, error_code=e.error_code)
            for e in events
        ],
    )
