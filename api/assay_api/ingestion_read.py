"""Reading ingestion history (BUILD_SPEC.md sections 10 and 11), for both views.

- Tenant view (`GET /documents`): `app_tenant`, so row-level security shows the token's
  tenant only. It has file names, batch names and who uploaded.
- Master view (`GET /admin/ingestion`): `platform_reader`, through `platform.platform_ingestion_v`
  only, which has no file name (section 7). No batch name and no uploader either.

Every filter is a bound parameter; sort orders come from fixed allow-lists, so no request
value is ever written into SQL. Documents with status `discarded` are left out unless the
caller asks for that status (owner decision 15, build step 6). CSV cells a spreadsheet would
read as a formula are prefixed with an apostrophe (as in Audit history).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, Row, text

from assay_api.audit_read import csv_cell

PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
CSV_MAX_ROWS = 10_000
DEFAULT_DAYS = 7
MAX_RANGE = dt.timedelta(days=400)
DISCARDED = "discarded"

# Sort keys -> ORDER BY. Every order ends with the received time and ID, so pages are stable.
_TAIL = "d.received_at DESC, d.id DESC"
# Duration: one definition for both views and both CSV exports (owner, 2026-11-25).
#
# A document is timed from its latest `queued` event to `completed_at`. That event is written
# when the document becomes eligible for the pipeline (its batch closed and its files scanned
# clean), and again by every "Process again", so the latest one is where the current attempt
# began: a document uploaded yesterday and processed again today took seconds, not a day. A
# first attempt's duration therefore leaves out the upload, the scan and the time its batch
# stayed open. A document that was never queued (held at its scan, quarantined, failed before
# the pipeline) is timed from when it was received.
#
# No grant was needed: the master view reads the same events through
# `platform.ingestion_events_v`, which `platform_reader` could already read.
STARTED_STEP = "queued"

# --- The Text step's page counts (build step 6, second half, stage 2) -----------------------
#
# The worker's `text` event says how many pages it read, how many from the text layer, how
# many through Textract, and how many Textract would not take. Both step panels show them
# (owner decision C, 2026-11-27: the unread count in the tenant view and the master view).
# Counts only: no page's text is in any response, and none is in the database to begin with.
TEXT_STEP = "text"
TEXT_COUNT_KEYS = ("pages", "pages_native", "pages_ocr", "pages_unread")


def text_counts_sql(detail: str) -> str:
    """The four counts as columns, from an event's `detail`: each only for a `text` event,
    and only if the stored value is a plain whole number. `detail` is a column name from this
    module's callers, never anything from a request."""
    return ", ".join(
        f"CASE WHEN step = '{TEXT_STEP}' AND {detail} ->> '{key}' ~ '^[0-9]{{1,7}}$' "
        f"THEN ({detail} ->> '{key}')::integer END AS {key}"
        for key in TEXT_COUNT_KEYS
    )


class TextPages(BaseModel):
    """What the Text step did with a document's pages. Numbers only."""

    # Every page of the document.
    total: int
    # Read from the page's own text layer.
    native: int
    # Read by Textract from the page's picture.
    ocr: int
    # Sent to Textract, which would not read them: these pages have no text.
    unread: int


def text_pages(event: Any) -> TextPages | None:
    """The counts of a `text` event's row (from `text_counts_sql`, or the master view's own
    columns), or None for any other event and for a `text` event that has none."""
    if event.step != TEXT_STEP or event.pages is None:
        return None
    return TextPages(
        total=event.pages,
        native=event.pages_native or 0,
        ocr=event.pages_ocr or 0,
        unread=event.pages_unread or 0,
    )


def _started(*, events: str, same_document: str, received: str) -> str:
    """When the document's current attempt began, as SQL. Every argument is fixed text from
    this module (a relation, a join condition, a column): nothing from a request."""
    return (
        f"COALESCE((SELECT max(e.at) FROM {events} e "  # noqa: S608  # nosec B608
        f"WHERE {same_document} AND e.step = '{STARTED_STEP}'), {received})"
    )


_STARTED = _started(
    events="assay.document_events",
    same_document="e.tenant_id = d.tenant_id AND e.document_id = d.id",
    received="d.received_at",
)
_DURATION = f"(d.completed_at - {_STARTED})"
TENANT_SORTS: dict[str, str] = {
    "received": "d.received_at ASC, d.id ASC",
    "-received": _TAIL,
    "batch": f"b.name ASC NULLS LAST, {_TAIL}",
    "-batch": f"b.name DESC NULLS LAST, {_TAIL}",
    "file_name": f"lower(d.file_name) ASC, {_TAIL}",
    "-file_name": f"lower(d.file_name) DESC, {_TAIL}",
    "pages": f"d.page_count ASC NULLS LAST, {_TAIL}",
    "-pages": f"d.page_count DESC NULLS LAST, {_TAIL}",
    "size": f"d.size_bytes ASC NULLS LAST, {_TAIL}",
    "-size": f"d.size_bytes DESC NULLS LAST, {_TAIL}",
    "companion": f"d.has_companion ASC, {_TAIL}",
    "-companion": f"d.has_companion DESC, {_TAIL}",
    "status": f"d.status ASC, {_TAIL}",
    "-status": f"d.status DESC, {_TAIL}",
    "forms": f"forms_found ASC, {_TAIL}",
    "-forms": f"forms_found DESC, {_TAIL}",
    "duration": f"{_DURATION} ASC NULLS LAST, {_TAIL}",
    "-duration": f"{_DURATION} DESC NULLS LAST, {_TAIL}",
    "uploaded_by": f"lower(u.name) ASC, {_TAIL}",
    "-uploaded_by": f"lower(u.name) DESC, {_TAIL}",
}
_MTAIL = "v.received_at DESC, v.document_id DESC"
_MSTARTED = _started(
    events="platform.ingestion_events_v",
    same_document="e.document_id = v.document_id",
    received="v.received_at",
)
_MDURATION = f"(v.completed_at - {_MSTARTED})"
MASTER_SORTS: dict[str, str] = {
    "received": "v.received_at ASC, v.document_id ASC",
    "-received": _MTAIL,
    "tenant": f"v.tenant_name ASC, {_MTAIL}",
    "-tenant": f"v.tenant_name DESC, {_MTAIL}",
    "batch": f"v.batch_id ASC, {_MTAIL}",
    "-batch": f"v.batch_id DESC, {_MTAIL}",
    "document": f"v.document_id ASC, {_MTAIL}",
    "-document": f"v.document_id DESC, {_MTAIL}",
    "pages": f"v.page_count ASC NULLS LAST, {_MTAIL}",
    "-pages": f"v.page_count DESC NULLS LAST, {_MTAIL}",
    "size": f"v.size_bytes ASC NULLS LAST, {_MTAIL}",
    "-size": f"v.size_bytes DESC NULLS LAST, {_MTAIL}",
    "companion": f"v.has_companion ASC, {_MTAIL}",
    "-companion": f"v.has_companion DESC, {_MTAIL}",
    "status": f"v.status ASC, {_MTAIL}",
    "-status": f"v.status DESC, {_MTAIL}",
    "forms": f"v.part_count ASC, {_MTAIL}",
    "-forms": f"v.part_count DESC, {_MTAIL}",
    "duration": f"{_MDURATION} ASC NULLS LAST, {_MTAIL}",
    "-duration": f"{_MDURATION} DESC NULLS LAST, {_MTAIL}",
}
DEFAULT_SORT = "-received"


@dataclass(frozen=True)
class Filters:
    start: dt.datetime
    end: dt.datetime
    statuses: tuple[str, ...] = ()
    batch_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None  # master view only

    def as_detail(self) -> dict[str, Any]:
        """The filters, for an export's audit row: times, statuses and IDs only."""
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "statuses": list(self.statuses),
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
        }


# An export's audit row (owner decision, 2026-11-24): the same category and action as an
# Audit history export, told apart by `target_type`.
EXPORT_CATEGORY, EXPORT_ACTION = "audit", "exported"
TENANT_EXPORT_TARGET = ("ingestion_history", "Ingestion history CSV")
MASTER_EXPORT_TARGET = ("ingestion_history_all_tenants", "Ingestion history CSV (all tenants)")


def _where(filters: Filters, alias: str) -> tuple[str, dict[str, Any]]:
    clauses = [f"{alias}.received_at >= :start", f"{alias}.received_at < :end"]
    params: dict[str, Any] = {"start": filters.start, "end": filters.end}
    if filters.statuses:
        clauses.append(f"{alias}.status = ANY(:statuses)")
        params["statuses"] = list(filters.statuses)
    else:
        clauses.append(f"{alias}.status <> '{DISCARDED}'")
    if filters.batch_id is not None:
        clauses.append(f"{alias}.batch_id = :batch_id")
        params["batch_id"] = filters.batch_id
    if filters.tenant_id is not None:
        clauses.append(f"{alias}.tenant_id = :tenant_id")
        params["tenant_id"] = filters.tenant_id
    return " AND ".join(clauses), params


TENANT_FROM = (
    "FROM assay.documents d "
    "JOIN assay.batches b ON b.tenant_id = d.tenant_id AND b.id = d.batch_id "
    "JOIN assay.users u ON u.tenant_id = d.tenant_id AND u.id = d.uploaded_by "
)
_TENANT_COLUMNS = (
    "d.id, d.received_at, d.batch_id, b.name AS batch_name, d.file_name, d.page_count, "
    "d.size_bytes, d.has_companion, d.status, d.error_code, d.completed_at, "
    "u.name AS uploaded_by, (SELECT count(*) FROM assay.document_parts p "
    "WHERE p.tenant_id = d.tenant_id AND p.document_id = d.id) AS forms_found"
)
# Two constants of this module joined; no request value is part of either.
TENANT_SELECT = f"SELECT {_TENANT_COLUMNS}, {_STARTED} AS started_at "
_MASTER_COLUMNS = (
    "v.document_id, v.tenant_id, v.tenant_name, v.batch_id, v.status, v.error_code, "
    "v.page_count, v.size_bytes, v.has_companion, v.part_count, v.received_at, v.completed_at"
)
# As for the tenant view: constants of this module joined; no request value.
MASTER_SELECT = f"SELECT {_MASTER_COLUMNS}, {_MSTARTED} AS started_at " + (
    "FROM platform.platform_ingestion_v v "
)


MASTER_FROM = "FROM platform.platform_ingestion_v v "


def _run(
    conn: Connection,
    *,
    select_from: str,
    count_from: str,
    where: str,
    params: dict[str, Any],
    order: str,
    limit: int,
    offset: int,
) -> tuple[int, list[Row[Any]]]:
    # Only fixed fragments are joined here; every value is a bound parameter.
    total = conn.execute(
        text(f"SELECT count(*) {count_from}WHERE {where}"),  # nosec B608
        params,
    ).scalar_one()
    rows = conn.execute(
        text(f"{select_from}WHERE {where} ORDER BY {order} LIMIT :limit OFFSET :offset"),
        {**params, "limit": limit, "offset": offset},
    ).all()
    return int(total), list(rows)


def tenant_page(
    conn: Connection, filters: Filters, *, sort: str, number: int, size: int
) -> tuple[int, list[Row[Any]]]:
    where, params = _where(filters, "d")
    return _run(
        conn,
        select_from=TENANT_SELECT + TENANT_FROM,
        count_from=TENANT_FROM,
        where=where,
        params=params,
        order=TENANT_SORTS[sort],
        limit=size,
        offset=(number - 1) * size,
    )


def master_page(
    conn: Connection, filters: Filters, *, sort: str, number: int, size: int
) -> tuple[int, list[Row[Any]]]:
    where, params = _where(filters, "v")
    return _run(
        conn,
        select_from=MASTER_SELECT,
        count_from=MASTER_FROM,
        where=where,
        params=params,
        order=MASTER_SORTS[sort],
        limit=size,
        offset=(number - 1) * size,
    )


def duration_seconds(row: Row[Any]) -> int | None:
    """How long the document's current attempt took: from `started_at` (see `_started`) to
    when it finished. None while it is still under way. Both views' tables, panels and CSV
    exports show this one number."""
    if row.completed_at is None:
        return None
    return int((row.completed_at - row.started_at).total_seconds())


TENANT_CSV_COLUMNS = (
    "received",
    "batch",
    "file_name",
    "pages",
    "size_bytes",
    "companion_file",
    "status",
    "error",
    "forms_found",
    "duration_seconds",
    "uploaded_by",
)
MASTER_CSV_COLUMNS = (
    "received",
    "tenant",
    "batch_id",
    "document_id",
    "pages",
    "size_bytes",
    "companion_file",
    "status",
    "error",
    "forms_found",
    "duration_seconds",
)


def _csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for values in rows:
        writer.writerow([csv_cell(v) for v in values])
    return out.getvalue()


def tenant_csv(rows: Sequence[Row[Any]]) -> str:
    return _csv(
        TENANT_CSV_COLUMNS,
        [
            (
                r.received_at.isoformat(),
                r.batch_name or r.batch_id,
                r.file_name,
                r.page_count,
                r.size_bytes,
                "yes" if r.has_companion else "no",
                r.status,
                r.error_code,
                r.forms_found,
                duration_seconds(r),
                r.uploaded_by,
            )
            for r in rows
        ],
    )


def master_csv(rows: Sequence[Row[Any]]) -> str:
    return _csv(
        MASTER_CSV_COLUMNS,
        [
            (
                r.received_at.isoformat(),
                r.tenant_name,
                r.batch_id,
                r.document_id,
                r.page_count,
                r.size_bytes,
                "yes" if r.has_companion else "no",
                r.status,
                r.error_code,
                r.part_count,
                duration_seconds(r),
            )
            for r in rows
        ],
    )


def resolve_range(
    start: dt.datetime | None, end: dt.datetime | None, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    """The last DEFAULT_DAYS unless a range is given; ValueError for a bad one."""
    end_at = end or now
    start_at = start or end_at - dt.timedelta(days=DEFAULT_DAYS)
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("times_need_a_time_zone")
    if start_at >= end_at or end_at - start_at > MAX_RANGE:
        raise ValueError("invalid_range")
    return start_at, end_at
