"""Step 2: record each GuardDuty Malware Protection scan result (BUILD_SPEC.md section 8).

Invoked by an EventBridge rule for "GuardDuty Malware Protection Object Scan Result" events
from `aws.guardduty` about tenant buckets. For one event:

1. The key must be an inbox key (`inbox/{batch_id}/{file_id}`). Anything else is logged and
   ignored: the pipeline's own writes outside inbox/ are scanned too while a tenant's plan
   still covers the whole bucket, and nothing outside inbox/ is ever an upload.
   Two exceptions under inbox/: the isolation tests' inbox fixture
   (`keys.isolation_fixture_inbox_key()`, exactly that key) is ignored at INFO, and any
   other key under inbox/ that is not an upload key is logged at ERROR (an alarm).
2. The tenant is the one whose registered upload has that key (`pipeline.tenant_for_upload`),
   and the event's bucket must be that tenant's bucket (owner decision 3, build step 6).
   A key no upload was registered under is logged at ERROR, which raises an alarm, and
   otherwise ignored (condition d).
3. In that tenant only, with the batch locked: the first decisive result for the file wins
   and pins the scanned object version. A result for another version of the same key (a
   second PUT through the same link) is never used (condition b).
   - `NO_THREATS_FOUND`: the stored object at that version must have the registered size and
     SHA-256 (condition a); a companion file is then read, at most 64 KB, and validated with
     plain code (condition e). Its content is never logged.
   - `THREATS_FOUND`: copied to quarantine/ at that version, and the inbox key gets a delete
     marker. The PDF's document becomes `quarantined`.
   - `UNSUPPORTED`, `FAILED`, or `ACCESS_DENIED` (except `OBJECT_E_TAG_CHANGED`, which means a
     newer version will be scanned): held, `on_hold`.
4. If the batch is already closed, any document now eligible is queued after the commit.

Errors that might pass (the database, the TSS, S3) raise, so Lambda retries the event and
then sends it to the dead-letter queue, whose depth raises an alarm.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from assay_db import batches
from assay_schemas import companion
from assay_schemas.pipeline import checksum_b64
from assay_tss import keys, naming
from assay_workers.runtime import (
    IngestQueue,
    Settings,
    StorageSource,
    TenantStorage,
    jobs_for,
    lookup_tx,
    tenant_tx,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVICE = "scan"
DETAIL_TYPE = "GuardDuty Malware Protection Object Scan Result"
SOURCE = "aws.guardduty"
# A newer version of the object is being scanned; its own result follows.
SUPERSEDED = "OBJECT_E_TAG_CHANGED"
HELD = {
    "UNSUPPORTED": "scan_unsupported",
    "FAILED": "scan_failed",
    "ACCESS_DENIED": "scan_access_denied",
}
CLEAN, THREATS = "NO_THREATS_FOUND", "THREATS_FOUND"


@dataclass(frozen=True)
class ScanEvent:
    event_id: str
    bucket: str
    key: str
    version_id: str | None
    result: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Deps:
    engine: Engine
    storage: StorageSource
    queue: IngestQueue
    name_prefix: str


def parse(event: dict[str, Any]) -> ScanEvent | None:
    """The fields we use, or None if this is not a scan result we handle."""
    if event.get("source") != SOURCE or event.get("detail-type") != DETAIL_TYPE:
        return None
    detail = event.get("detail") or {}
    obj = detail.get("s3ObjectDetails") or {}
    result = detail.get("scanResultDetails") or {}
    bucket = obj.get("bucketName")
    key = obj.get("objectKey")
    status = result.get("scanResultStatus")
    if not (isinstance(bucket, str) and isinstance(key, str) and isinstance(status, str)):
        return None
    version = obj.get("versionId")
    reasons = result.get("statusReasons") or []
    return ScanEvent(
        event_id=str(event.get("id", "")),
        bucket=bucket,
        key=key,
        version_id=version if isinstance(version, str) and version else None,
        result=status,
        reasons=tuple(str(r) for r in reasons if isinstance(r, str)),
    )


def _mismatch(event: ScanEvent, code: str) -> batches.ScanOutcome:
    return batches.ScanOutcome(
        status="mismatch", scan_result=event.result, version_id=event.version_id, error_code=code
    )


def clean_outcome(
    storage: TenantStorage, file: Any, event: ScanEvent, tenant_id: uuid.UUID
) -> batches.ScanOutcome:
    """A clean scan: the stored object must be the registered one; then a companion's rules."""
    version = event.version_id
    info = storage.objects.head(file.s3_key, version) if version else None
    if info is None or info.version_id != version:
        # Without the exact scanned version there is nothing safe to pin.
        return _mismatch(event, "version_missing")
    if info.size_bytes != file.size_bytes:
        return _mismatch(event, "size_mismatch")
    if info.checksum_sha256 != checksum_b64(file.sha256):
        return _mismatch(event, "checksum_mismatch")
    if file.kind == batches.COMPANION:
        # Condition (e): the stored size was just checked against the registration, which is
        # at most 64 KB; the read is capped at that in any case. Never log the content.
        if info.size_bytes > companion.MAX_BYTES:
            return batches.ScanOutcome(
                status="invalid",
                scan_result=event.result,
                version_id=version,
                error_code="companion_invalid",
                error_detail=[companion.CompanionError("too_large").as_detail()],
            )
        assert version is not None  # noqa: S101  # nosec B101  (checked above)
        raw = storage.objects.read(file.s3_key, version, companion.MAX_BYTES)
        try:
            companion.validate(raw, tenant_id=tenant_id)
        except companion.CompanionError as exc:
            return batches.ScanOutcome(
                status="invalid",
                scan_result=event.result,
                version_id=version,
                error_code="companion_invalid",
                # Every problem found (at most companion.MAX_ERRORS), codes and keys only.
                error_detail=exc.details(),
            )
    return batches.ScanOutcome(status="clean", scan_result=event.result, version_id=version)


def quarantine(storage: TenantStorage, file: Any, event: ScanEvent) -> str:
    """Copy the infected version to quarantine/ and leave a delete marker in the inbox."""
    ctx = storage.context
    target = (
        ctx.quarantine_key(file.id)
        if file.kind == batches.PDF
        else ctx.quarantine_companion_key(file.id)
    )
    if event.version_id is not None:
        storage.objects.copy(
            source_key=file.s3_key,
            source_version=event.version_id,
            target_key=target,
            content_type=batches.CONTENT_TYPES[file.kind],
        )
    storage.objects.delete(file.s3_key)
    return target


def handle(event: dict[str, Any], deps: Deps) -> str:
    """Returns what was done, for the log and the tests."""
    scan = parse(event)
    if scan is None:
        log.warning("not a scan result event; ignored")
        return "ignored_event"
    fields: dict[str, Any] = {"event_id": scan.event_id, "result": scan.result}
    if scan.key == keys.isolation_fixture_inbox_key():
        # The isolation tests' inbox fixture, written by them under the reserved ID. No upload
        # can have this key (`inbox_key` refuses the reserved ID). This exact key only.
        log.info("scan result for the isolation tests' inbox fixture; ignored", extra=fields)
        return "isolation_fixture"
    inbox = keys.parse_inbox_key(scan.key)
    if inbox is None:
        if scan.key.startswith(keys.INBOX_PREFIX):
            # Under inbox/ but not a key `inbox_key` builds (or the reserved ID in another
            # shape): nothing registered could have written it. Condition (d).
            log.error("scan result for an inbox key no upload can have", extra=fields)
            return "not_an_upload_key"
        log.info("scan result outside the inbox; ignored", extra=fields)
        return "outside_inbox"
    batch_id, file_id = inbox
    fields |= {"batch_id": str(batch_id), "file_id": str(file_id)}

    with lookup_tx(deps.engine) as conn:
        tenant_id = batches.tenant_for_upload(conn, scan.key)
    if tenant_id is None:
        log.error("scan result for a key no upload was registered under", extra=fields)
        return "unregistered"
    fields["tenant_id"] = str(tenant_id)
    if scan.bucket != naming.bucket_name(deps.name_prefix, tenant_id):
        log.error("scan result names a bucket that is not the upload's tenant's", extra=fields)
        return "wrong_bucket"
    if scan.result not in (CLEAN, THREATS, *HELD):
        log.error("unknown scan result; ignored", extra=fields)
        return "unknown_result"
    if scan.result == "ACCESS_DENIED" and SUPERSEDED in scan.reasons:
        log.info("scan superseded by a newer version; waiting for its result", extra=fields)
        return "superseded"

    storage = deps.storage.pipeline(tenant_id, SERVICE, f"scan-{file_id.hex[:16]}")
    with tenant_tx(deps.engine, tenant_id) as conn:
        if batches.lock_batch(conn, batch_id) is None:
            log.error("scan result for a batch that does not exist", extra=fields)
            return "unregistered"
        file = batches.lock_file(conn, file_id)
        if file is None or file.batch_id != batch_id or file.s3_key != scan.key:
            log.error("scan result does not match its registration", extra=fields)
            return "unregistered"
        if file.status != "registered":
            # The first decisive result won; this one (a repeat, or another version) is not
            # used (condition b).
            same = file.version_id == scan.version_id
            log.info(
                "scan result already recorded" if same else "a later version is never used",
                extra=fields | {"file_status": file.status},
            )
            return "already_recorded" if same else "later_version_ignored"

        if scan.result == CLEAN:
            outcome = clean_outcome(storage, file, scan, tenant_id)
        elif scan.result == THREATS:
            target = quarantine(storage, file, scan)
            outcome = batches.ScanOutcome(
                status="infected",
                scan_result=scan.result,
                version_id=scan.version_id,
                error_code="threats_found",
                quarantine_key=target if file.kind == batches.PDF else None,
            )
        else:
            outcome = batches.ScanOutcome(
                status="unsupported",
                scan_result=scan.result,
                version_id=scan.version_id,
                error_code=HELD[scan.result],
            )
        eligible = batches.record_scan(conn, file, outcome)
    fields |= {"file_status": outcome.status, "error": outcome.error_code}
    log.info("scan result recorded", extra=fields)
    if eligible:
        try:
            deps.queue.send(jobs_for(tenant_id, eligible))
            log.info("documents queued", extra=fields | {"documents": len(eligible)})
        except Exception:
            # The result is recorded; retrying the event would change nothing. The sweeper
            # queues the documents again (assay_db.batches.REQUEUE_AFTER).
            log.exception("documents not queued; the sweeper will queue them again", extra=fields)
    return outcome.status


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
            name_prefix=settings.name_prefix,
        )
    return _deps


def handler(event: dict[str, Any], context: object) -> dict[str, str]:
    return {"outcome": handle(event, _state())}
