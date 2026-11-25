"""Step 9: the companion file's values, stored by version (BUILD_SPEC.md section 8).

Plain code, never the model. Used in two places, with the same function:

- the pipeline's `companion` step, after the worker task, for every attempt of a document;
- the scan step, when a companion file attached or replaced after ingestion scans clean and
  valid (owner decision A4). No worker task runs and no AI service is called.

`apply` runs inside the caller's transaction, as `app_tenant` in one tenant:

1. The document's newest clean companion file is the one that counts (its upload, or a later
   replacement). With none there is nothing to store.
2. Its content is read from the inbox at exactly the version that scanned clean, at most
   64 KB, and validated again with the code the scan step used. It is never logged.
3. If it is not the file the document's current values came from, it is copied to
   `originals/{document}.companion.json` (a new version; the old one stays).
4. The values are written as a new version of the document's companion data and the
   document's pointer moves to it, in this transaction: `signers` (source `companion`),
   `companion_loan_terms`, `document_labels`, and the reserved keys on `documents`. Nothing
   is deleted (owner decision 2); readers follow `documents.companion_attempt`.
5. A replacement that takes effect writes an audit row naming both files and their versions
   (owner decision 3), with the user who uploaded it as the actor. No file name: a file is
   named by its ID.

Companion values never overwrite what the pipeline reads from the PDF: both are kept, each
with its source.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from assay_db import audit, batches
from assay_schemas import companion
from assay_workers.runtime import TenantStorage

log = logging.getLogger()

STEP = "companion"
STORED, NONE, INVALID = "stored", "none", "invalid"
CATEGORY = "document"
ATTACHED, REPLACED = "companion_attached", "companion_replaced"


@dataclass(frozen=True)
class Applied:
    outcome: str
    # The new version of the document's companion data, when one was stored.
    version: int | None = None


def _candidate(conn: Connection, document_id: uuid.UUID) -> Any | None:
    return conn.execute(
        text(
            "SELECT id, s3_key, version_id, purpose, registered_by, applied_at "
            "FROM assay.batch_files WHERE document_id = :id AND kind = 'companion' "
            "AND status = 'clean' AND version_id IS NOT NULL "
            "ORDER BY registered_at DESC, id LIMIT 1"
        ),
        {"id": document_id},
    ).one_or_none()


def _store(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    file_id: uuid.UUID,
    version: int,
    values: companion.Companion,
) -> None:
    keys = {"tenant_id": tenant_id, "document_id": document_id, "attempt": version}
    for signer in values.signers:
        conn.execute(
            text(
                "INSERT INTO assay.signers (tenant_id, document_id, attempt, name, email, "
                "phone, location, source) VALUES (:tenant_id, :document_id, :attempt, :name, "
                ":email, :phone, :location, 'companion')"
            ),
            keys
            | {
                "name": signer.name,
                "email": signer.email,
                "phone": signer.phone,
                "location": signer.location,
            },
        )
    terms = (values.loan_term_months, values.loan_maturity_date, values.loan_interest_rate)
    if any(value is not None for value in terms):
        conn.execute(
            text(
                "INSERT INTO assay.companion_loan_terms (tenant_id, document_id, attempt, "
                "term_months, maturity_date, rate) VALUES (:tenant_id, :document_id, "
                ":attempt, :term, :maturity, :rate)"
            ),
            keys | {"term": terms[0], "maturity": terms[1], "rate": terms[2]},
        )
    for key, value in values.labels.items():
        conn.execute(
            text(
                "INSERT INTO assay.document_labels (tenant_id, document_id, attempt, key, "
                "value, source) VALUES (:tenant_id, :document_id, :attempt, :key, "
                "CAST(:value AS jsonb), 'companion')"
            ),
            keys | {"key": key, "value": json.dumps(value)},
        )
    conn.execute(
        text(
            "UPDATE assay.documents SET companion_attempt = :attempt, "
            "companion_file_id = :file, has_companion = true, application_ref = :ref, "
            "loan_number = :loan, cosigner_name = :cosigner, package_created_on = :created, "
            "package_completed_on = :completed WHERE id = :document_id"
        ),
        keys
        | {
            "file": file_id,
            "ref": values.application_ref,
            "loan": values.loan_number,
            "cosigner": values.cosigner_name,
            "created": values.package_created_on,
            "completed": values.package_completed_on,
        },
    )
    conn.execute(
        text("UPDATE assay.batch_files SET applied_at = now() WHERE id = :file"),
        {"file": file_id},
    )


def _audit_replacement(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    file: Any,
    previous_file: uuid.UUID | None,
    version: int,
    object_version: str | None,
) -> None:
    user = conn.execute(
        text("SELECT id, email, role FROM assay.users WHERE id = :id"), {"id": file.registered_by}
    ).one_or_none()
    before = None
    if previous_file is not None:
        before_version = conn.execute(
            text("SELECT version_id FROM assay.batch_files WHERE id = :id"), {"id": previous_file}
        ).scalar_one_or_none()
        before = {"file_id": str(previous_file), "version_id": before_version}
    audit.write(
        conn,
        tenant_id=tenant_id,
        actor=audit.AuditActor(
            role=user.role if user else "system",
            user_id=user.id if user else None,
            email=user.email if user else None,
        ),
        category=CATEGORY,
        action=REPLACED if previous_file is not None else ATTACHED,
        target_type="document",
        target_id=str(document_id),
        target_label=None,
        detail={
            "before": before,
            "after": {
                "file_id": str(file.id),
                "version_id": file.version_id,
                "stored_version_id": object_version,
            },
            "companion_version": version,
            "reported_by": "pipeline",
        },
    )


def apply(
    conn: Connection, storage: TenantStorage, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Applied:
    """Store the document's companion values as a new version. See the module docstring."""
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    document = conn.execute(
        text(
            "SELECT companion_attempt, companion_file_id FROM assay.documents "
            "WHERE id = :id FOR UPDATE"
        ),
        {"id": document_id},
    ).one()
    file = _candidate(conn, document_id)
    if file is None:
        batches.document_event(
            conn, tenant_id=tenant_id, document_id=document_id, step=STEP, status=NONE
        )
        log.info("no companion file", extra=fields)
        return Applied(NONE)

    raw = storage.objects.read(file.s3_key, file.version_id, companion.MAX_BYTES)
    try:
        values = companion.validate(raw, tenant_id=tenant_id)
    except companion.CompanionError as exc:
        # The scan step accepted this file, so the rules have changed since. The document
        # keeps the values it has. Codes and keys only, never a value.
        batches.document_event(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            step=STEP,
            status=INVALID,
            detail={"error": "companion_invalid", "problems": len(exc.details())},
        )
        log.warning("the companion file no longer validates; values kept", extra=fields)
        return Applied(INVALID)

    changed = file.id != document.companion_file_id
    object_version: str | None = None
    if changed:
        object_version = storage.objects.copy(
            source_key=file.s3_key,
            source_version=file.version_id,
            target_key=storage.context.companion_key(document_id),
            content_type=batches.CONTENT_TYPES[batches.COMPANION],
        )
    version = int(document.companion_attempt or 0) + 1
    _store(
        conn,
        tenant_id=tenant_id,
        document_id=document_id,
        file_id=file.id,
        version=version,
        values=values,
    )
    replacement = file.purpose == batches.REPLACEMENT and file.applied_at is None
    if replacement:
        _audit_replacement(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            file=file,
            previous_file=document.companion_file_id,
            version=version,
            object_version=object_version,
        )
    batches.document_event(
        conn,
        tenant_id=tenant_id,
        document_id=document_id,
        step=STEP,
        status=STORED,
        detail={
            "companion_version": version,
            "signers": len(values.signers),
            "labels": len(values.labels),
            "replaced": replacement,
        },
    )
    log.info(
        "companion values stored",
        extra=fields | {"companion_version": version, "file_id": str(file.id)},
    )
    return Applied(STORED, version)
