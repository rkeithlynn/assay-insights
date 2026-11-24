"""The pipeline state machine's Lambda steps (BUILD_SPEC.md section 8).

One function, two steps, chosen by the state's `step` field. The input's tenant and document
come from the execution's input, which is the ingest job (section 6, rule 4).

- `fingerprint` (step 4). Reads the PDF at exactly the version that scanned clean (condition
  b), hashes it and checks the hash against the registration. Then, with an advisory lock
  on tenant and hash:
  - if the tenant already has a document with that hash (never another tenant's: row-level
    security and an explicit tenant condition, condition g), the document becomes
    `duplicate`, points at it, and the execution stops;
  - a file that is not a PDF fails (`not_a_pdf`); a password-protected PDF is held
    (`on_hold`, `pdf_encrypted`), as GuardDuty may pass it as clean (owner decision 13);
  - otherwise the PDF, and its companion file at its own clean version, are copied to
    `originals/`, the document's key, size and page count are set, and processing goes on.
  The PDF itself is never modified. A repeat of the step finds its own `fingerprint` event
  and returns the same outcome without doing anything again.
- `failed`: the state machine's catch. The document becomes `failed` with `{step}_failed`
  (the step from the state machine's definition, checked against `PIPELINE_STEPS`), which is
  all a tenant user sees. Its `failed` event also keeps the error's name (`error_type`,
  checked against `ERROR_TYPE`) for the master view. The error's message and stack trace
  never reach this step.

The reserved isolation-test document ID is refused by both: no row read or written, no
object touched.
"""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from assay_db import batches
from assay_tss import keys
from assay_workers.runtime import Settings, StorageSource, tenant_tx

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVICE = "pipeline"
PDF_MAGIC = b"%PDF-"
# The PDF header may follow a little leading junk; PDF readers accept up to 1 KB of it.
HEADER_WINDOW = 1024
# pypdfium2.raw.FPDF_ERR_PASSWORD: the document needs a password to open.
PDFIUM_PASSWORD_ERROR = 4

CONTINUE, DUPLICATE, ON_HOLD, FAILED, REFUSED = (
    "continue",
    "duplicate",
    "on_hold",
    "failed",
    "refused",
)
# The steps a failure can name (the state machine's `failed_step`). The document's code is
# `{step}_failed`; anything else is `pipeline_error`. The second half adds steps 5 to 10.
PIPELINE_STEPS = frozenset({"fingerprint"})
DEFAULT_FAILURE = "pipeline_error"
# The error's name as Step Functions gives it: an exception type, an AWS error code or a
# `States.*` name. Kept only if it matches; never its message or stack trace.
ERROR_TYPE = re.compile(r"[A-Za-z0-9_.]{1,64}")


class Refused(Exception):
    """This document must not be processed (the reserved ID). Never retried."""


@dataclass(frozen=True)
class Deps:
    engine: Engine
    storage: StorageSource


def _ids(event: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.UUID(str(event["tenant_id"]))
    document_id = uuid.UUID(str(event["document_id"]))
    if keys.is_reserved(document_id):
        raise Refused("the reserved isolation-test document ID is never processed")
    return tenant_id, document_id


def _done(conn: Connection, document_id: uuid.UUID) -> str | None:
    """The outcome a previous run of fingerprint recorded, if any."""
    row = conn.execute(
        text(
            "SELECT detail ->> 'outcome' FROM assay.document_events "
            "WHERE document_id = :id AND step = 'fingerprint' ORDER BY at DESC LIMIT 1"
        ),
        {"id": document_id},
    ).scalar_one_or_none()
    return None if row is None else str(row)


def inspect_pdf(path: Path) -> tuple[str | None, int | None]:
    """(problem, page count): problem is `not_a_pdf`, `pdf_encrypted` or None."""
    with path.open("rb") as handle:
        head = handle.read(HEADER_WINDOW)
    if PDF_MAGIC not in head:
        return "not_a_pdf", None
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError as exc:
        if getattr(exc, "err_code", None) == PDFIUM_PASSWORD_ERROR:
            return "pdf_encrypted", None
        return "not_a_pdf", None
    try:
        return None, len(pdf)
    finally:
        pdf.close()


def _record(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    batches.document_event(
        conn,
        tenant_id=tenant_id,
        document_id=document_id,
        step="fingerprint",
        status=outcome,
        detail={"outcome": outcome, **(detail or {})},
    )


def fingerprint(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    tenant_id, document_id = _ids(event)
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    with tenant_tx(deps.engine, tenant_id) as conn:
        previous = _done(conn, document_id)
        if previous is not None:
            log.info("fingerprint already done", extra=fields | {"outcome": previous})
            return {"outcome": previous}
        files = conn.execute(
            text(
                "SELECT f.id, f.kind, f.s3_key, f.version_id, f.sha256, f.status "
                "FROM assay.batch_files f WHERE f.document_id = :id "
                "AND f.status = 'clean' ORDER BY f.kind DESC"
            ),
            {"id": document_id},
        ).all()
        status = conn.execute(
            text("SELECT status FROM assay.documents WHERE id = :id"), {"id": document_id}
        ).scalar_one_or_none()
    pdf = next((f for f in files if f.kind == batches.PDF), None)
    companion_file = next((f for f in files if f.kind == batches.COMPANION), None)
    if status != "processing" or pdf is None or pdf.version_id is None:
        log.error("document is not ready for fingerprinting", extra=fields)
        raise RuntimeError("document is not eligible")

    storage = deps.storage.pipeline(tenant_id, SERVICE, f"fingerprint-{document_id.hex[:12]}")
    ctx, objects = storage.context, storage.objects
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "document.pdf"
        digest = objects.download(pdf.s3_key, pdf.version_id, path)
        size = path.stat().st_size
        problem, pages = inspect_pdf(path)

    with tenant_tx(deps.engine, tenant_id) as conn:
        if digest != pdf.sha256:
            # The upload link and the scan step both checked this; a difference here means the
            # object changed under its version, which S3 does not allow. Fail loudly.
            _record(
                conn,
                tenant_id=tenant_id,
                document_id=document_id,
                outcome=FAILED,
                detail={"error": "checksum_mismatch"},
            )
            conn.execute(
                text(
                    "UPDATE assay.documents SET status = 'failed', error_code = "
                    "'checksum_mismatch', completed_at = now() WHERE id = :id"
                ),
                {"id": document_id},
            )
            log.error("checksum differs from the registration", extra=fields)
            return {"outcome": FAILED}

        # One original per hash within the tenant; the lock makes check-then-write safe.
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"{tenant_id}:{digest}"},
        )
        original = conn.execute(
            text(
                "SELECT id FROM assay.documents WHERE tenant_id = :tenant AND sha256 = :sha "
                "AND duplicate_of IS NULL AND status <> 'failed' AND id <> :id "
                "ORDER BY received_at LIMIT 1"
            ),
            {"tenant": tenant_id, "sha": digest, "id": document_id},
        ).scalar_one_or_none()
        if original is not None:
            conn.execute(
                text(
                    "UPDATE assay.documents SET sha256 = :sha, duplicate_of = :original, "
                    "status = 'duplicate', completed_at = now() WHERE id = :id"
                ),
                {"id": document_id, "sha": digest, "original": original},
            )
            _record(
                conn,
                tenant_id=tenant_id,
                document_id=document_id,
                outcome=DUPLICATE,
                detail={"duplicate_of": str(original)},
            )
            log.info("duplicate", extra=fields)
            return {"outcome": DUPLICATE}

        if problem is not None:
            held = problem == "pdf_encrypted"
            conn.execute(
                text(
                    "UPDATE assay.documents SET sha256 = :sha, status = :status, "
                    "error_code = :error, completed_at = now() WHERE id = :id"
                ),
                {
                    "id": document_id,
                    "sha": digest,
                    "status": "on_hold" if held else "failed",
                    "error": problem,
                },
            )
            outcome = ON_HOLD if held else FAILED
            _record(
                conn,
                tenant_id=tenant_id,
                document_id=document_id,
                outcome=outcome,
                detail={"error": problem},
            )
            log.info("not processed", extra=fields | {"error": problem})
            return {"outcome": outcome}

        original_key = ctx.original_key(document_id)
        objects.copy(
            source_key=pdf.s3_key,
            source_version=pdf.version_id,
            target_key=original_key,
            content_type="application/pdf",
        )
        if companion_file is not None and companion_file.version_id is not None:
            objects.copy(
                source_key=companion_file.s3_key,
                source_version=companion_file.version_id,
                target_key=ctx.companion_key(document_id),
                content_type="application/json",
            )
        conn.execute(
            text(
                "UPDATE assay.documents SET sha256 = :sha, s3_key = :key, size_bytes = :size, "
                "page_count = :pages WHERE id = :id"
            ),
            {"id": document_id, "sha": digest, "key": original_key, "size": size, "pages": pages},
        )
        _record(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            outcome=CONTINUE,
            detail={"pages": pages, "companion": companion_file is not None},
        )
    log.info("fingerprinted", extra=fields | {"pages": pages})
    return {"outcome": CONTINUE}


def failure_code(step: object) -> str:
    """`{step}_failed` for a known step, else `pipeline_error`."""
    return f"{step}_failed" if isinstance(step, str) and step in PIPELINE_STEPS else DEFAULT_FAILURE


def error_type(event: dict[str, Any]) -> str | None:
    """The error's name, if it is one. An execution started before the definition passed
    only the error's name gave the whole error object (`error`); its name is read from it."""
    value = event.get("error_type")
    if value is None and isinstance(event.get("error"), dict):
        value = event["error"].get("Error")
    if isinstance(value, str) and ERROR_TYPE.fullmatch(value):
        return value
    return None


def failed(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    tenant_id, document_id = _ids(event)
    code = failure_code(event.get("failed_step"))
    name = error_type(event)
    with tenant_tx(deps.engine, tenant_id) as conn:
        changed = conn.execute(
            text(
                "UPDATE assay.documents SET status = 'failed', error_code = :code, "
                "completed_at = now() WHERE id = :id AND status = 'processing' RETURNING id"
            ),
            {"id": document_id, "code": code},
        ).first()
        if changed is not None:
            batches.document_event(
                conn,
                tenant_id=tenant_id,
                document_id=document_id,
                step="failed",
                status="failed",
                detail={"error": code, "error_type": name},
            )
    # The error's name is logged (an exception type or a state's error, never content).
    log.error(
        "document failed",
        extra={
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "error": code,
            "error_type": name,
        },
    )
    return {"outcome": FAILED, "error": code}


STEPS = {"fingerprint": fingerprint, "failed": failed}


def handle(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    step = STEPS.get(str(event.get("step")))
    if step is None:
        raise ValueError("unknown step")
    try:
        return step(event, deps)
    except Refused:
        log.warning("refused the reserved isolation-test document ID")
        return {"outcome": REFUSED}


_deps: Deps | None = None


def _state() -> Deps:
    global _deps
    if _deps is None:
        from assay_workers.runtime import TssStorageSource, app_tenant_engine

        settings = Settings.from_env()
        _deps = Deps(
            engine=app_tenant_engine(),
            storage=TssStorageSource(settings.tss_endpoint_url, region=settings.region),
        )
    return _deps


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return handle(event, _state())
