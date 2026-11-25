"""One document's attempt, in the worker task's parent process.

Part 1 (build step 6, second half): the `inspect` step.

1. The session is already bound to the attempt grant (`db.bound_engine`), so every query
   below sees this one document. The document must be `processing` at this attempt.
2. The PDF is downloaded at exactly the version that scanned clean, into the task's work
   directory, and its SHA-256 is checked against the registration once more.
3. A sandbox child opens it (`sandbox.runner.inspect`). The parent never does.
   - needs a password: the document is held (`on_hold`, `pdf_encrypted`);
   - not a PDF that opens, or the child was stopped (a crash, the timeout, a limit): the
     document fails (`not_a_pdf`, `pdf_unreadable`). A stopped child is logged at ERROR: it
     is what a broken file looks like, and what an attack on the parser looks like.
   - otherwise the PDF, and its companion file at its own clean version, are copied to
     `originals/`, and the document gets its key and page count.
4. Steps 5 to 10 follow in the later stages. Until then the document stays `processing`.

Each step writes a `running` mark before it starts and its result when it ends
(`assay_workers.progress`). A repeat of the task finds the result and does nothing again.
Nothing here builds an object key except through the storage context, lists a bucket, or
logs anything from a file.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from assay_db import batches
from assay_tss import keys
from assay_workers import progress
from assay_workers.runtime import TenantStorage
from assay_workers.sandbox.runner import InspectResult, SandboxError
from assay_workers.worker.db import Job

log = logging.getLogger("assay.worker")

CONTINUE, ON_HOLD, FAILED, NOT_ELIGIBLE = "continue", "on_hold", "failed", "not_eligible"
INSPECT = "inspect"
PDF_UNREADABLE = "pdf_unreadable"
CHECKSUM_MISMATCH = "checksum_mismatch"


class Refused(Exception):
    """This document must not be processed (the reserved ID). Never retried."""


@dataclass(frozen=True)
class Deps:
    # Bound to the job's grant: it sees one document of one tenant.
    engine: Engine
    # The document's storage, from the TSS's /resolve-worker. Asked for once, when needed.
    storage: Callable[[], TenantStorage]
    # Opens the PDF in a sandbox child.
    inspect: Callable[[Path], InspectResult]
    # The task's writable directory.
    work_dir: Path


@dataclass(frozen=True)
class Document:
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    files: tuple[Any, ...]

    def file(self, kind: str) -> Any | None:
        return next((f for f in self.files if f.kind == kind), None)

    @property
    def fields(self) -> dict[str, str]:
        return {"tenant_id": str(self.tenant_id), "document_id": str(self.document_id)}


def _event(
    conn: Connection, doc: Document, step: str, status: str, detail: dict[str, Any] | None = None
) -> None:
    batches.document_event(
        conn,
        tenant_id=doc.tenant_id,
        document_id=doc.document_id,
        step=step,
        status=status,
        detail=detail,
    )


def _result(conn: Connection, doc: Document, step: str) -> str | None:
    """The result an earlier run of this step recorded, if any (never a progress mark)."""
    row = conn.execute(
        text(
            "SELECT status FROM assay.document_events WHERE document_id = :id AND step = :step "
            "AND status <> ALL(:marks) ORDER BY at DESC LIMIT 1"
        ),
        {"id": doc.document_id, "step": step, "marks": list(progress.MARKS)},
    ).scalar_one_or_none()
    return None if row is None else str(row)


def load(conn: Connection, job: Job) -> Document | None:
    """The job's document and its clean files, or None if this attempt is not to be run."""
    tenant_id = conn.execute(text("SELECT pipeline.bound_tenant()")).scalar_one_or_none()
    row = conn.execute(
        text("SELECT id, status, attempt FROM assay.documents WHERE id = :id"),
        {"id": job.document_id},
    ).one_or_none()
    if tenant_id is None or row is None or row.status != "processing":
        return None
    if row.attempt != job.attempt:
        return None
    files = conn.execute(
        text(
            "SELECT id, kind, s3_key, version_id, sha256 FROM assay.batch_files "
            "WHERE document_id = :id AND status = 'clean' ORDER BY kind DESC"
        ),
        {"id": job.document_id},
    ).all()
    return Document(tenant_id=tenant_id, document_id=job.document_id, files=tuple(files))


def _finish(
    deps: Deps, doc: Document, *, status: str, outcome: str, code: str, level: int = logging.INFO
) -> str:
    """The document stops here: held or failed, with its code."""
    with deps.engine.begin() as conn:
        _event(conn, doc, INSPECT, outcome, {"error": code})
        conn.execute(
            text(
                "UPDATE assay.documents SET status = :status, error_code = :code, "
                "completed_at = now() WHERE id = :id AND status = 'processing'"
            ),
            {"id": doc.document_id, "status": status, "code": code},
        )
    log.log(level, "not processed", extra=doc.fields | {"error": code})
    return outcome


def inspect(doc: Document, deps: Deps) -> str:
    pdf = doc.file(batches.PDF)
    companion = doc.file(batches.COMPANION)
    if pdf is None or pdf.version_id is None:
        raise RuntimeError("the document has no clean PDF to inspect")
    storage = deps.storage()
    ctx, objects = storage.context, storage.objects

    with tempfile.TemporaryDirectory(dir=deps.work_dir) as work:
        # The sandbox user must be able to reach the file, and only read it.
        Path(work).chmod(0o755)  # nosec B103
        path = Path(work) / "document.pdf"
        digest = objects.download(pdf.s3_key, pdf.version_id, path)
        path.chmod(0o444)
        if digest != pdf.sha256:
            # The scan step and the fingerprint step both checked this version.
            return _finish(
                deps,
                doc,
                status="failed",
                outcome=FAILED,
                code=CHECKSUM_MISMATCH,
                level=logging.ERROR,
            )
        try:
            result = deps.inspect(path)
        except SandboxError as exc:
            # The reason is a code from the sandbox runner, never anything the child wrote.
            log.error("the PDF could not be opened", extra=doc.fields | {"reason": exc.reason})
            return _finish(deps, doc, status="failed", outcome=FAILED, code=PDF_UNREADABLE)

    if result.problem == "pdf_encrypted":
        return _finish(deps, doc, status="on_hold", outcome=ON_HOLD, code=result.problem)
    if result.problem is not None:
        return _finish(deps, doc, status="failed", outcome=FAILED, code=result.problem)

    original_key = ctx.original_key(doc.document_id)
    objects.copy(
        source_key=pdf.s3_key,
        source_version=pdf.version_id,
        target_key=original_key,
        content_type=batches.CONTENT_TYPES[batches.PDF],
    )
    has_companion = companion is not None and companion.version_id is not None
    if companion is not None and companion.version_id is not None:
        objects.copy(
            source_key=companion.s3_key,
            source_version=companion.version_id,
            target_key=ctx.companion_key(doc.document_id),
            content_type=batches.CONTENT_TYPES[batches.COMPANION],
        )
    with deps.engine.begin() as conn:
        _event(conn, doc, INSPECT, CONTINUE, {"pages": result.pages, "companion": has_companion})
        conn.execute(
            text(
                "UPDATE assay.documents SET s3_key = :key, page_count = :pages "
                "WHERE id = :id AND status = 'processing'"
            ),
            {"id": doc.document_id, "key": original_key, "pages": result.pages},
        )
    log.info("inspected", extra=doc.fields | {"pages": result.pages})
    return CONTINUE


STEPS: dict[str, Callable[[Document, Deps], str]] = {INSPECT: inspect}


def run(job: Job, deps: Deps) -> str:
    """Run the attempt's steps in order. Returns the last outcome; raises on a failure that
    is not the document's (the database, storage, the TSS)."""
    if keys.is_reserved(job.document_id):
        raise Refused("the reserved isolation-test document ID is never processed")
    with deps.engine.begin() as conn:
        doc = load(conn, job)
        if doc is None:
            log.warning(
                "not this attempt's turn; nothing done",
                extra={"document_id": str(job.document_id), "attempt": job.attempt},
            )
            return NOT_ELIGIBLE
        _event(conn, doc, progress.WORKER, progress.RUNNING)

    outcome = CONTINUE
    for step in progress.WORKER_STEPS:
        with deps.engine.begin() as conn:
            previous = _result(conn, doc, step)
            if previous is None:
                _event(conn, doc, step, progress.RUNNING)
        if previous is not None:
            log.info("step already done", extra=doc.fields | {"step": step, "outcome": previous})
            outcome = previous
        else:
            try:
                outcome = STEPS[step](doc, deps)
            except Exception as exc:
                _record_error(deps, doc, step, exc)
                raise
        if outcome != CONTINUE:
            break
    return outcome


def _record_error(deps: Deps, doc: Document, step: str, exc: Exception) -> None:
    """Best effort: name the step and the exception's type for the failure handler. If the
    database is what failed, the step's `running` mark still names the step."""
    try:
        with deps.engine.begin() as conn:
            _event(conn, doc, step, progress.ERROR, {"error_type": type(exc).__name__})
    except Exception:  # the task is failing anyway; its failure must not be hidden by this one
        log.warning("could not record the failed step", extra=doc.fields | {"step": step})
