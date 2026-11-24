"""GET /admin/audit: Audit history across tenants (BUILD_SPEC.md sections 7a, 10 and 11).

Both master roles. The rows are read in the account check's read-only `platform_reader`
transaction: the route changes nothing. Newest first, 50 per page at most (enforced here,
whatever the request asks), server-side sort and filters, and the last 7 days unless a range
is given. Every filter is a bound parameter and the sort comes from a fixed allow-list.

Both master roles see every row's actor and target, tenant users' email addresses included
(owner decision, 2026-11-23: section 7 asks for an audit history of sign-ins and user
changes; D8 applies to the admin list views, not to the audit record). Before and after
values pass through `assay_api.audit_read.redact`, so no password, token or code is shown.

`format=csv` returns up to 10,000 rows, and each export writes its own `audit` / `exported`
row (actor, filters, row count) as `platform_writer`, in a transaction of its own that
commits before the file is sent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import Row

from assay_api import audit_read
from assay_api.database import Databases
from assay_api.requestinfo import request_info
from assay_api.security import PlatformMember
from assay_db import audit

router = APIRouter(prefix="/admin")

Category = Literal["auth", "user", "tenant", "audit"]
Sort = Literal[
    "at",
    "-at",
    "tenant",
    "-tenant",
    "actor",
    "-actor",
    "action",
    "-action",
    "category",
    "-category",
]
MAX_RANGE = dt.timedelta(days=400)  # 13 months online, plus a margin


def databases(request: Request) -> Databases:
    dbs: Databases = request.app.state.databases
    return dbs


Dbs = Annotated[Databases, Depends(databases)]


class AuditEvent(BaseModel):
    id: uuid.UUID
    at: dt.datetime
    tenant_id: uuid.UUID
    tenant_name: str
    actor_email: str | None
    actor_role: str | None
    category: str
    action: str
    target_type: str | None
    target_id: str | None
    target_label: str | None
    outcome: str
    # Values before and after, with anything secret redacted.
    detail: dict[str, Any] | None
    ip_address: str | None
    user_agent: str | None


class AuditPage(BaseModel):
    events: list[AuditEvent]
    total: int
    page: int
    page_size: int
    start: dt.datetime
    end: dt.datetime


def _event(row: Row[Any]) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        at=row.at,
        tenant_id=row.tenant_id,
        tenant_name=row.tenant_name,
        actor_email=row.actor_email,
        actor_role=row.actor_role,
        category=row.category,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        target_label=row.target_label,
        outcome=row.outcome,
        detail=audit_read.detail_of(row),
        ip_address=row.ip_address,
        user_agent=row.user_agent,
    )


@router.get(
    "/audit",
    response_model=AuditPage,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def audit_history(
    caller: PlatformMember,
    dbs: Dbs,
    request: Request,
    start: Annotated[dt.datetime | None, Query(alias="from")] = None,
    end: Annotated[dt.datetime | None, Query(alias="to")] = None,
    tenant_id: uuid.UUID | None = None,
    category: Category | None = None,
    actor_email: Annotated[str | None, Query(max_length=320)] = None,
    sort: Sort = "-at",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=audit_read.MAX_PAGE_SIZE)] = audit_read.PAGE_SIZE,
    format: Literal["json", "csv"] = "json",
) -> Any:
    now = dt.datetime.now(dt.UTC)
    end_at = end or now
    start_at = start or end_at - dt.timedelta(days=audit_read.DEFAULT_DAYS)
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="times_need_a_time_zone")
    if start_at >= end_at or end_at - start_at > MAX_RANGE:
        raise HTTPException(status_code=422, detail="invalid_range")
    filters = audit_read.AuditFilters(
        start=start_at,
        end=end_at,
        tenant_id=tenant_id,
        category=category,
        actor_email=(actor_email or "").strip() or None,
    )

    if format == "csv":
        rows = audit_read.export(caller.conn, filters, sort=sort)
        account = caller.account
        with dbs.platform_writer() as writer:
            audit.write(
                writer,
                tenant_id=account.tenant_id,
                actor=audit.AuditActor(
                    role=account.role, user_id=account.user_id, email=account.email
                ),
                category="audit",
                action="exported",
                target_type="audit_log",
                target_id=None,
                target_label="Audit history CSV",
                detail={
                    "filters": filters.as_detail(),
                    "sort": sort,
                    "rows": len(rows),
                    "limit": audit_read.CSV_MAX_ROWS,
                },
                request=request_info(request),
            )
        name = f"audit-{start_at:%Y%m%d}-{end_at:%Y%m%d}.csv"
        return Response(
            content=audit_read.to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Row-Count": str(len(rows)),
                "Cache-Control": "no-store",
            },
        )

    total, rows = audit_read.page(caller.conn, filters, sort=sort, number=page, size=page_size)
    return AuditPage(
        events=[_event(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        start=start_at,
        end=end_at,
    )
