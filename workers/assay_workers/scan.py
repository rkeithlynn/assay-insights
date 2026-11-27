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
     marker. The PDF's document becomes `quarantined`. An audit row (category `security`,
     `file_quarantined`) is written in the same transaction, and after the commit a
     security notice goes to its own SNS topic (owner decision 4, second half). Both name
     the tenant, the uploading user, the time, the batch and document IDs and GuardDuty's
     threat name, and never the file name or any content. It is a notice, not a failure:
     nothing here is logged at ERROR. The quarantine key and the source version are logged
     and kept in the audit row, so the copy can be confirmed later.
   - `UNSUPPORTED`, `FAILED`, or `ACCESS_DENIED` (except `OBJECT_E_TAG_CHANGED`, which means a
     newer version will be scanned): held, `on_hold`.
4. If the batch is already closed, any document now eligible is queued after the commit.
5. A companion file attached or replaced after ingestion (`purpose` `companion_replacement`)
   that scans clean and valid takes effect at once, in the same transaction
   (`assay_workers.companion_data`). One that is invalid or infected changes nothing about
   its document.

Errors that might pass (the database, the TSS, S3) raise, so Lambda retries the event and
then sends it to the dead-letter queue, whose depth raises an alarm.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from assay_db import audit, batches
from assay_schemas import companion
from assay_schemas.pipeline import checksum_b64
from assay_tss import keys, naming
from assay_workers import companion_data
from assay_workers.runtime import (
    IngestQueue,
    Notices,
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
# GuardDuty's name for what it found: `detail.scanResultDetails.threats[0].name` ("contains
# only one threat ... the first detected threat", GuardDuty user guide). It comes from the
# event, so only a plain printable name of a sensible length is kept.
THREAT_NAME = re.compile(r"[\x20-\x7e]{1,200}")
UNNAMED_THREAT = "not named in the scan result"
SECURITY_CATEGORY = "security"
QUARANTINED = "file_quarantined"
SYSTEM = "system"
NOTICE_SUBJECT = "Assay Insights security notice: malware quarantined"


@dataclass(frozen=True)
class ScanEvent:
    event_id: str
    bucket: str
    key: str
    version_id: str | None
    result: str
    reasons: tuple[str, ...]
    threat: str | None = None


@dataclass(frozen=True)
class Deps:
    engine: Engine
    storage: StorageSource
    queue: IngestQueue
    name_prefix: str
    # The security-notice topic. None: no notice is sent (the audit row is still written).
    notices: Notices | None = None


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
    threats = result.get("threats") or []
    first = threats[0] if isinstance(threats, list) and threats else None
    name = first.get("name") if isinstance(first, dict) else None
    return ScanEvent(
        threat=name if isinstance(name, str) and THREAT_NAME.fullmatch(name) else None,
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


@dataclass(frozen=True)
class Quarantine:
    """What a notice says. IDs, a threat name and an email address; never a file name."""

    tenant_id: uuid.UUID
    tenant_name: str | None
    uploader_id: uuid.UUID | None
    uploader_email: str | None
    batch_id: uuid.UUID
    file_id: uuid.UUID
    document_id: uuid.UUID | None
    kind: str
    threat: str
    at: dt.datetime

    def message(self) -> str:
        lines = [
            "A file uploaded to Assay Insights was found to contain malware and was "
            "quarantined. It was not processed and cannot be opened in the portal.",
            "",
            f"Threat (GuardDuty): {self.threat}",
            f"Time (UTC): {self.at:%Y-%m-%d %H:%M:%S}",
            f"Tenant: {self.tenant_name or 'unknown'} ({self.tenant_id})",
            f"Uploaded by: {self.uploader_email or 'unknown'} ({self.uploader_id})",
            f"Batch ID: {self.batch_id}",
            f"Document ID: {self.document_id or 'none (a companion file with no document)'}",
            f"File: a {'PDF' if self.kind == batches.PDF else 'companion file'}, "
            f"file ID {self.file_id}",
            "",
            "This is a security notice, not a system failure. The audit history has a "
            "'security' row for it (file_quarantined).",
        ]
        return "\n".join(lines)


def describe(conn: Connection, file: Any, threat: str | None) -> Quarantine:
    """Who and what the quarantine concerns, read in the file's tenant."""
    tenant = conn.execute(
        text("SELECT name FROM assay.tenants WHERE id = :id"), {"id": file.tenant_id}
    ).scalar_one_or_none()
    user = conn.execute(
        text("SELECT id, email FROM assay.users WHERE id = :id"), {"id": file.registered_by}
    ).one_or_none()
    return Quarantine(
        tenant_id=file.tenant_id,
        tenant_name=tenant,
        uploader_id=user.id if user else None,
        uploader_email=user.email if user else None,
        batch_id=file.batch_id,
        file_id=file.id,
        document_id=file.id if file.kind == batches.PDF else file.document_id,
        kind=file.kind,
        threat=threat or UNNAMED_THREAT,
        at=dt.datetime.now(dt.UTC),
    )


def audit_quarantine(
    conn: Connection, q: Quarantine, *, quarantine_key: str, source_version: str | None
) -> None:
    """The `security` row, in the scan result's transaction. The upload's user is named in
    the detail; the actor is the system, which is what moved the file."""
    audit.write(
        conn,
        tenant_id=q.tenant_id,
        actor=audit.AuditActor(role=SYSTEM),
        category=SECURITY_CATEGORY,
        action=QUARANTINED,
        target_type="document" if q.document_id else "file",
        target_id=str(q.document_id or q.file_id),
        target_label=None,
        detail={
            "threat": q.threat,
            "uploaded_by": {
                "user_id": None if q.uploader_id is None else str(q.uploader_id),
                "email": q.uploader_email,
            },
            "batch_id": str(q.batch_id),
            "document_id": None if q.document_id is None else str(q.document_id),
            "file_id": str(q.file_id),
            "kind": q.kind,
            "quarantine_key": quarantine_key,
            "source_version_id": source_version,
        },
    )


def notify(deps: Deps, q: Quarantine, fields: dict[str, Any]) -> None:
    """Publish the notice and mark the file. A failed publish raises, so Lambda retries the
    event (the retry finds the file infected with no notice sent, and sends it); after its
    retries the event is in the scan dead-letter queue, whose depth raises an alarm."""
    if deps.notices is None:
        log.warning("no security-notice topic is configured; notice not sent", extra=fields)
        return
    deps.notices.publish(subject=NOTICE_SUBJECT, message=q.message())
    with tenant_tx(deps.engine, q.tenant_id) as conn:
        conn.execute(
            text(
                "UPDATE assay.batch_files SET notice_sent_at = now() "
                "WHERE id = :id AND notice_sent_at IS NULL"
            ),
            {"id": q.file_id},
        )
    log.info("security notice sent", extra=fields)


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
    quarantined: Quarantine | None = None
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
            if not (same and file.status == "infected" and file.notice_sent_at is None):
                return "already_recorded" if same else "later_version_ignored"
            # Recorded, but its notice was never published (a failed publish): send it now.
            quarantined = describe(conn, file, scan.threat)

        if quarantined is not None:
            pass
        elif scan.result == CLEAN:
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
            quarantined = describe(conn, file, scan.threat)
            audit_quarantine(
                conn, quarantined, quarantine_key=target, source_version=scan.version_id
            )
            # INFO on purpose: a quarantine is a security notice, not a pipeline error.
            log.info(
                "file quarantined",
                extra=fields | {"quarantine_key": target, "source_version_id": scan.version_id},
            )
        else:
            outcome = batches.ScanOutcome(
                status="unsupported",
                scan_result=scan.result,
                version_id=scan.version_id,
                error_code=HELD[scan.result],
            )
        if file.status != "registered":
            eligible: list[uuid.UUID] = []
        else:
            eligible = batches.record_scan(conn, file, outcome)
            if file.purpose == batches.REPLACEMENT and file.document_id is not None:
                # A companion file attached or replaced after ingestion. Clean and valid, it
                # takes effect here, with the result that made it usable: no task runs, no
                # model is called. Otherwise the document is unchanged, and the audit trail
                # says how it ended (reason codes only; the file's content is never repeated).
                ended, reason = outcome.status, outcome.error_code or outcome.status
                if outcome.status == "clean":
                    applied = companion_data.apply(
                        conn, storage, tenant_id=tenant_id, document_id=file.document_id
                    )
                    # Accepted here and refused there cannot happen with one set of rules;
                    # if it ever does, the trail still says the file was refused.
                    ended, reason = "invalid", "companion_invalid"
                    if applied.outcome == companion_data.STORED:
                        ended = "clean"
                if ended != "clean":
                    # Rejected (checked and refused) or expired (GuardDuty could not scan it).
                    companion_data.audit_not_applied(
                        conn,
                        tenant_id=tenant_id,
                        file=file,
                        file_status=ended,
                        reason=reason,
                        problems=outcome.error_detail,
                        version_id=outcome.version_id,
                    )
    if file.status != "registered":
        # The result was recorded by an earlier delivery; only its notice was outstanding.
        if quarantined is not None:
            notify(deps, quarantined, fields)
        return "notice_sent"
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
    if quarantined is not None:
        # Last: everything else about the result is done, whatever happens to the notice.
        notify(deps, quarantined, fields)
    return outcome.status


_deps: Deps | None = None


def _state() -> Deps:
    global _deps
    if _deps is None:
        from assay_workers.runtime import (
            SnsNotices,
            SqsIngestQueue,
            TssStorageSource,
            app_tenant_engine,
        )

        settings = Settings.from_env()
        _deps = Deps(
            engine=app_tenant_engine(),
            storage=TssStorageSource(settings.tss_endpoint_url, region=settings.region),
            queue=SqsIngestQueue(settings.ingest_queue_url, region=settings.region),
            name_prefix=settings.name_prefix,
            notices=(
                SnsNotices(settings.security_topic_arn, region=settings.region)
                if settings.security_topic_arn
                else None
            ),
        )
    return _deps


def handler(event: dict[str, Any], context: object) -> dict[str, str]:
    return {"outcome": handle(event, _state())}
