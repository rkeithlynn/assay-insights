"""One document's attempt, in the worker task's parent process.

The steps, in order (`assay_workers.progress.WORKER_STEPS`):

- **`inspect`** (build step 6, second half, part 1), below.
- **`text`** (stage 2, step 5 of BUILD_SPEC.md section 8), in `assay_workers.worker.text`.

How the attempt runs:

1. The session is already bound to the attempt grant (`db.bound_engine`), so every query
   below sees this one document. The document must be `processing` at this attempt.
2. The PDF is downloaded once, at exactly the version that scanned clean, into the task's
   work directory, and its SHA-256 is checked against the registration once more
   (`context.Source`). Every step that needs it uses that one copy.
3. `inspect`: a sandbox child opens it (`sandbox.runner.inspect`). The parent never does.
   - needs a password: the document is held (`on_hold`, `pdf_encrypted`);
   - not a PDF that opens: the document fails (`not_a_pdf`);
   - the child did not end well: the document fails (`pdf_unreadable`, which is all a tenant
     user sees). How it ended is named (`sandbox.runner.ending`) in the log line and in the
     event's `error_type`, which master admins see: `sandbox.cpu_limit` and
     `sandbox.wall_clock_timeout` are the sandbox's limits doing their job;
     `sandbox.signal_SIGSEGV` and the like are a child that died some other way. The two
     kinds have different log messages. Both are logged at ERROR, which raises the worker's
     alarm: either is what a broken file looks like, and what an attack on the parser looks
     like;
   - the child could not be started at all: that is the task's fault, not the file's, so the
     step fails (`inspect_failed`) and the document is not blamed.
   - otherwise the PDF is copied to `originals/`, and the document gets its key and page
     count.
4. `text`: the text layer, Textract for the pages that fail the quality score, the text
   object and the `pages` rows.
5. Steps 6 to 8 and 10 follow in the later stages. Until then the document stays
   `processing`.

Each step writes a `running` mark before it starts and its result when it ends
(`assay_workers.progress`). A repeat of the task finds the result and does nothing again.
Nothing here builds an object key except through the storage context, lists a bucket, or
logs anything from a file.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Connection, text

from assay_db import batches
from assay_tss import keys
from assay_workers import progress
from assay_workers.sandbox.runner import SandboxError, SandboxLimit, SandboxNotStarted
from assay_workers.worker import text as text_step
from assay_workers.worker.context import (
    CHECKSUM_MISMATCH,
    CONTINUE,
    FAILED,
    NOT_ELIGIBLE,
    ON_HOLD,
    SANDBOX,
    ChecksumMismatch,
    Deps,
    Document,
    Pages,
    Source,
    TextSettings,
    checksum_mismatch,
    event,
    finish,
)
from assay_workers.worker.db import Job

log = logging.getLogger("assay.worker")

INSPECT = "inspect"
PDF_UNREADABLE = "pdf_unreadable"
# The two log messages for a sandbox child that did not end well. They differ on purpose: a
# limit that did its job is not the same event as a child that died some other way.
STOPPED_AT_A_LIMIT = "the PDF check was stopped at a sandbox limit"
ENDED_UNEXPECTEDLY = "the PDF check ended unexpectedly"
# An error's name as the failure handler accepts it (assay_workers.steps.ERROR_TYPE).
ERROR_NAME = re.compile(r"[A-Za-z0-9_.]{1,64}")

__all__ = [
    "CHECKSUM_MISMATCH",
    "CONTINUE",
    "FAILED",
    "INSPECT",
    "NOT_ELIGIBLE",
    "ON_HOLD",
    "PDF_UNREADABLE",
    "SANDBOX",
    "STEPS",
    "Deps",
    "Document",
    "Pages",
    "Refused",
    "Source",
    "TextSettings",
    "inspect",
    "load",
    "run",
]


# The name this module's own code and its tests knew the event writer by.
_event = event


class Refused(Exception):
    """This document must not be processed (the reserved ID). Never retried."""


def _result(conn: Connection, doc: Document, step: str) -> str | None:
    """The result an earlier run of this step recorded for this attempt, if any (never a
    progress mark)."""
    row = conn.execute(
        text(
            "SELECT status FROM assay.document_events WHERE document_id = :id AND step = :step "
            "AND attempt = :attempt AND status <> ALL(:marks) ORDER BY at DESC LIMIT 1"
        ),
        {
            "id": doc.document_id,
            "step": step,
            "attempt": doc.attempt,
            "marks": list(progress.MARKS),
        },
    ).scalar_one_or_none()
    return None if row is None else str(row)


def load(conn: Connection, job: Job) -> Document | None:
    """The job's document and its clean files, or None if this attempt is not to be run."""
    tenant_id = conn.execute(text("SELECT pipeline.bound_tenant()")).scalar_one_or_none()
    row = conn.execute(
        text("SELECT id, status, attempt, s3_key FROM assay.documents WHERE id = :id"),
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
    return Document(
        tenant_id=tenant_id,
        document_id=job.document_id,
        files=tuple(files),
        attempt=job.attempt,
        s3_key=row.s3_key,
    )


def inspect(doc: Document, deps: Deps, source: Source) -> str:
    pdf = doc.file(batches.PDF)
    companion = doc.file(batches.COMPANION)
    if pdf is None or pdf.version_id is None:
        raise RuntimeError("the document has no clean PDF to inspect")
    try:
        path = source.fetch()
    except ChecksumMismatch:
        return checksum_mismatch(deps, doc, INSPECT)
    try:
        result = deps.inspect(path)
    except SandboxNotStarted:
        # The task could not start the child as the sandbox user. Not the file's fault:
        # the step fails and the document is not blamed.
        raise
    except SandboxError as exc:
        # The ending is a name from the sandbox runner, never anything the child wrote.
        at_a_limit = isinstance(exc, SandboxLimit)
        log.error(
            STOPPED_AT_A_LIMIT if at_a_limit else ENDED_UNEXPECTEDLY,
            extra=doc.fields | {"ending": exc.reason, "sandbox_limit": at_a_limit},
        )
        return finish(
            deps,
            doc,
            step=INSPECT,
            status="failed",
            outcome=FAILED,
            code=PDF_UNREADABLE,
            error_type=f"{SANDBOX}.{exc.reason}",
        )

    if result.problem == "pdf_encrypted":
        return finish(
            deps, doc, step=INSPECT, status="on_hold", outcome=ON_HOLD, code=result.problem
        )
    if result.problem is not None:
        return finish(deps, doc, step=INSPECT, status="failed", outcome=FAILED, code=result.problem)

    # The storage the PDF was just downloaded with.
    storage = source.storage or deps.storage()
    original_key = storage.context.original_key(doc.document_id)
    if doc.s3_key != original_key:
        # The first attempt to get here copies the PDF. A later attempt ("Process again")
        # finds it there: the same bytes, checked against the same hash above, are not
        # written to `originals/` a second time.
        storage.objects.copy(
            source_key=pdf.s3_key,
            source_version=pdf.version_id,
            target_key=original_key,
            content_type=batches.CONTENT_TYPES[batches.PDF],
        )
    # The companion file is copied and stored by the pipeline's `companion` step, after this
    # task (assay_workers.companion_data): it is plain JSON and needs no sandbox.
    has_companion = companion is not None and companion.version_id is not None
    with deps.engine.begin() as conn:
        event(conn, doc, INSPECT, CONTINUE, {"pages": result.pages, "companion": has_companion})
        conn.execute(
            text(
                "UPDATE assay.documents SET s3_key = :key, page_count = :pages "
                "WHERE id = :id AND status = 'processing'"
            ),
            {"id": doc.document_id, "key": original_key, "pages": result.pages},
        )
    log.info("inspected", extra=doc.fields | {"pages": result.pages})
    return CONTINUE


STEPS: dict[str, Callable[[Document, Deps, Source], str]] = {
    INSPECT: inspect,
    text_step.STEP: text_step.text,
}


def run(job: Job, deps: Deps) -> str:
    """Run the attempt's steps in order. Returns the last outcome; raises on a failure that
    is not the document's (the database, storage, the TSS, Textract)."""
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
        event(conn, doc, progress.WORKER, progress.RUNNING)

    outcome = CONTINUE
    with tempfile.TemporaryDirectory(dir=deps.work_dir) as work:
        # The sandbox user must be able to reach the PDF, and only read it. What the parent
        # keeps for itself (pages' text and pictures) is in a directory closed to it.
        shared = Path(work)
        shared.chmod(0o755)  # nosec B103
        private = shared / "parent"
        private.mkdir(mode=0o700)
        source = Source(doc=doc, deps=deps, shared=shared, private=private)
        for step in progress.WORKER_STEPS:
            with deps.engine.begin() as conn:
                previous = _result(conn, doc, step)
                if previous is None:
                    event(conn, doc, step, progress.RUNNING)
            if previous is not None:
                log.info(
                    "step already done", extra=doc.fields | {"step": step, "outcome": previous}
                )
                outcome = previous
            else:
                try:
                    outcome = STEPS[step](doc, deps, source)
                except Exception as exc:
                    _record_error(deps, doc, step, exc)
                    raise
            if outcome != CONTINUE:
                break
    return outcome


def error_name(exc: BaseException) -> str:
    """What is kept of an error: a name. An exception that carries the service's own error
    code (`error_type`, as `ocr.OcrFailed` does) is named by it; any other by its type."""
    carried = getattr(exc, "error_type", None)
    if isinstance(carried, str) and ERROR_NAME.fullmatch(carried):
        return carried
    return type(exc).__name__


def _record_error(deps: Deps, doc: Document, step: str, exc: Exception) -> None:
    """Best effort: name the step and the error for the failure handler. If the database is
    what failed, the step's `running` mark still names the step."""
    try:
        with deps.engine.begin() as conn:
            event(conn, doc, step, progress.ERROR, {"error_type": error_name(exc)})
    except Exception:  # the task is failing anyway; its failure must not be hidden by this one
        log.warning("could not record the failed step", extra=doc.fields | {"step": step})
