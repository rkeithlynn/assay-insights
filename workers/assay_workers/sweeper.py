"""Batch housekeeping, every five minutes (EventBridge Scheduler).

For each tenant with work (`pipeline.tenants_with_work`), in that tenant only:

1. **Overdue scans** (condition d). A registered file with no scan result 30 minutes after it
   was registered is looked up by its key (never by listing). If it is in the bucket, its
   scan never came: it becomes `scan_timeout`, its PDF `on_hold`, and an ERROR line raises an
   alarm. If it is not, nothing arrived before its link expired: `not_uploaded`. If the
   file was a companion attached or replaced after ingestion, either ending writes its
   `companion_replace_expired` audit row, with the reason for which.
2. **Batches open 24 hours** (section 10) are closed (owner decision 5): problem files are
   dropped or their PDF fails, and eligible documents are queued.
3. **Eligible documents never started** (a lost message, or a tenant that was suspended) are
   queued again after 15 minutes, but only while the tenant is active (owner decision 19).

Jobs are sent after each tenant's transaction commits.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from assay_db import batches
from assay_workers import companion_data
from assay_workers.runtime import (
    IngestQueue,
    Settings,
    StorageSource,
    jobs_for,
    lookup_tx,
    tenant_tx,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVICE = "sweeper"


@dataclass(frozen=True)
class Deps:
    engine: Engine
    storage: StorageSource
    queue: IngestQueue


@dataclass
class TenantResult:
    timed_out: int = 0
    not_uploaded: int = 0
    closed: int = 0
    queued: int = 0


def sweep_tenant(tenant_id: uuid.UUID, deps: Deps, now: dt.datetime) -> TenantResult:
    result = TenantResult()
    eligible: list[uuid.UUID] = []
    fields = {"tenant_id": str(tenant_id)}

    with tenant_tx(deps.engine, tenant_id) as conn:
        overdue = batches.overdue_files(conn, now)
    if overdue:
        storage = deps.storage.pipeline(tenant_id, SERVICE, f"sweep-{now:%Y%m%d%H%M}")
        for file in overdue:
            # A HEAD of the key, never a listing.
            uploaded = storage.objects.head(file.s3_key) is not None
            with tenant_tx(deps.engine, tenant_id) as conn:
                if batches.lock_batch(conn, file.batch_id) is None:
                    continue
                waiting = batches.lock_file(conn, file.id)
                eligible += batches.mark_overdue(conn, file.id, uploaded=uploaded)
                if (
                    waiting is not None
                    and waiting.status == "registered"
                    and waiting.purpose == batches.REPLACEMENT
                    and waiting.document_id is not None
                ):
                    # A companion file attached after ingestion that never got a scan
                    # result: its request expires here, and the audit trail says which way
                    # (it was never scanned, or it never arrived).
                    ended = "scan_timeout" if uploaded else "not_uploaded"
                    companion_data.audit_not_applied(
                        conn, tenant_id=tenant_id, file=waiting, file_status=ended, reason=ended
                    )
            if uploaded:
                result.timed_out += 1
                log.error(
                    "scan result never arrived; file held",
                    extra=fields | {"file_id": str(file.id), "batch_id": str(file.batch_id)},
                )
            else:
                result.not_uploaded += 1

    with tenant_tx(deps.engine, tenant_id) as conn:
        for batch_id in batches.stale_open_batches(conn, now):
            eligible += batches.auto_close(conn, batch_id)
            result.closed += 1
            log.info("batch open 24 hours; closed", extra=fields | {"batch_id": str(batch_id)})
        if batches.tenant_is_active(conn, tenant_id):
            again = batches.requeue_candidates(conn, now)
            eligible += again
        else:
            again = []

    if eligible:
        unique = list(dict.fromkeys(eligible))
        try:
            deps.queue.send(jobs_for(tenant_id, unique))
            result.queued = len(unique)
        except Exception:
            # They stay eligible and unstarted, so the next sweep queues them again.
            log.exception("documents not queued", extra=fields | {"documents": len(unique)})
    if again:
        log.info("eligible documents queued again", extra=fields | {"documents": len(again)})
    return result


def handle(deps: Deps, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    with lookup_tx(deps.engine) as conn:
        tenants = batches.tenants_with_work(conn, now)
    totals = TenantResult()
    for tenant_id in tenants:
        try:
            r = sweep_tenant(tenant_id, deps, now)
        except Exception:
            # One tenant's failure never stops the others; the ERROR line raises an alarm.
            log.exception("sweep failed for a tenant", extra={"tenant_id": str(tenant_id)})
            continue
        totals.timed_out += r.timed_out
        totals.not_uploaded += r.not_uploaded
        totals.closed += r.closed
        totals.queued += r.queued
    summary = {"tenants": len(tenants), **vars(totals)}
    log.info("sweep finished", extra=summary)
    return summary


_deps: Deps | None = None


def _state() -> Deps:
    global _deps
    if _deps is None:
        from assay_workers.runtime import SqsIngestQueue, TssStorageSource, app_tenant_engine

        settings = Settings.from_env()
        _deps = Deps(
            engine=app_tenant_engine(),
            storage=TssStorageSource(settings.tss_endpoint_url, region=settings.region),
            queue=SqsIngestQueue(settings.ingest_queue_url, region=settings.region),
        )
    return _deps


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return handle(_state())
