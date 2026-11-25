"""The pipeline state machine's Lambda steps (BUILD_SPEC.md section 8).

One function, several steps, chosen by the state's `step` field. The input's tenant and
document come from the execution's input, which is the ingest job (section 6, rule 4).

No step here opens a PDF (owner condition B, build step 6, second half): a Lambda has one
user and its credentials in its environment. Opening one is the worker task's sandboxed
`inspect` step (`assay_workers.worker`, `assay_workers.sandbox`).

- `fingerprint` (step 4). Reads the upload at exactly the version that scanned clean
  (condition b), hashes it and checks the hash against the registration. Then, with an
  advisory lock on tenant and hash:
  - if the tenant already has a document with that hash (never another tenant's: row-level
    security and an explicit tenant condition, condition g), the document becomes
    `duplicate`, points at it, and the execution stops;
  - a file with no PDF header fails (`not_a_pdf`): a comparison of its first bytes, not a
    parser;
  - otherwise its hash and size are recorded and the worker takes over.
  A repeat of the step finds its own `fingerprint` event and returns the same outcome.
- `claim`: takes one of the tenant's worker slots and creates the attempt grant
  (`assay_db.grants`), whose one-time token Step Functions passes to the worker task and to
  nothing else. With no slot free it answers `wait`, and the state machine asks again.
- `release`: runs after the task, however it ended. Ends the grant, and says whether the
  task ever started (it writes a `worker`/`running` event when it does). A task that never
  started, for lack of Fargate capacity for example, is not the document's failure: the
  state machine waits and claims again.
- `companion` (step 9), after the task: the companion file's values, stored by version
  (`assay_workers.companion_data`). Plain code; no PDF is opened and no model is called.
- `failed`: the state machine's catch. The document becomes `failed` with `{step}_failed`,
  which is all a tenant user sees. For the worker task the step is the one whose progress
  mark is still open (`assay_workers.progress`), read from the database, never from the
  error. Its `failed` event also keeps the error's name (`error_type`, checked against
  `ERROR_TYPE`) for the master view. The error's message and stack trace never reach this
  step.

Every step gets the attempt its execution was started for and does nothing unless that is
still the document's current attempt ("Process again" starts a new one): a superseded
attempt writes nothing.

The reserved isolation-test document ID is refused by all of them: no row read or written,
no object touched.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from assay_db import batches, grants
from assay_tss import keys
from assay_workers import companion_data, progress
from assay_workers.runtime import Settings, StorageSource, tenant_tx

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVICE = "pipeline"
PDF_MAGIC = b"%PDF-"
# The PDF header may follow a little leading junk; PDF readers accept up to 1 KB of it.
HEADER_WINDOW = 1024

CONTINUE, DUPLICATE, ON_HOLD, FAILED, REFUSED = (
    "continue",
    "duplicate",
    "on_hold",
    "failed",
    "refused",
)
CLAIMED, WAIT, STOPPED = "claimed", "wait", "stopped"
# The steps a failure can name. The document's code is `{step}_failed`; anything else is
# `pipeline_error`. `worker` is the task as a whole, when no step of it had started.
PIPELINE_STEPS = frozenset(
    {"fingerprint", "claim", progress.WORKER, *progress.WORKER_STEPS, companion_data.STEP}
)
DEFAULT_FAILURE = "pipeline_error"
# The error's name as Step Functions gives it: an exception type, an AWS error code or a
# `States.*` name. Kept only if it matches; never its message or stack trace.
ERROR_TYPE = re.compile(r"[A-Za-z0-9_.]{1,64}")
# A task that could not start is tried again without limit (the execution's own timeout
# aside), but someone should hear about it: every this many tries, at ERROR.
REPORT_UNSTARTED_EVERY = 10
DEFAULT_MAX_WORKERS = 5
DEFAULT_GRANT_SECONDS = 3600


class Refused(Exception):
    """This document must not be processed (the reserved ID). Never retried."""


@dataclass(frozen=True)
class Deps:
    engine: Engine
    storage: StorageSource
    # The cap on one tenant's concurrent worker tasks (stack config workerMaxPerTenant).
    max_workers: int = DEFAULT_MAX_WORKERS
    # How long a grant lasts if nothing ends it: the task's timeout and a margin.
    grant_ttl: dt.timedelta = dt.timedelta(seconds=DEFAULT_GRANT_SECONDS)


def _ids(event: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.UUID(str(event["tenant_id"]))
    document_id = uuid.UUID(str(event["document_id"]))
    if keys.is_reserved(document_id):
        raise Refused("the reserved isolation-test document ID is never processed")
    return tenant_id, document_id


def _attempt(event: dict[str, Any]) -> int | None:
    """The attempt the execution was started for. None for an execution started before
    attempts were passed: it is taken to be the document's current one."""
    value = event.get("attempt")
    return None if value is None else int(value)


def _current(conn: Connection, document_id: uuid.UUID, attempt: int | None) -> Any | None:
    """The document's status and attempt, locked, if `attempt` is still its current one."""
    row = conn.execute(
        text("SELECT status, attempt FROM assay.documents WHERE id = :id FOR UPDATE"),
        {"id": document_id},
    ).one_or_none()
    if row is None or (attempt is not None and row.attempt != attempt):
        return None
    return row


def _done(conn: Connection, document_id: uuid.UUID) -> str | None:
    """The outcome a previous run of fingerprint recorded for the current attempt, if any."""
    row = conn.execute(
        text(
            "SELECT e.detail ->> 'outcome' FROM assay.document_events e "
            "JOIN assay.documents d ON d.tenant_id = e.tenant_id AND d.id = e.document_id "
            "WHERE e.document_id = :id AND e.step = 'fingerprint' AND e.attempt = d.attempt "
            "ORDER BY e.at DESC LIMIT 1"
        ),
        {"id": document_id},
    ).scalar_one_or_none()
    return None if row is None else str(row)


def has_pdf_header(path: Path) -> bool:
    """The file's first bytes hold `%PDF-`. A comparison of bytes: nothing is parsed."""
    with path.open("rb") as handle:
        return PDF_MAGIC in handle.read(HEADER_WINDOW)


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
    attempt = _attempt(event)
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    with tenant_tx(deps.engine, tenant_id) as conn:
        if _current(conn, document_id, attempt) is None:
            log.info("not the document's current attempt; nothing done", extra=fields)
            return {"outcome": STOPPED}
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
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "upload"
        digest = storage.objects.download(pdf.s3_key, pdf.version_id, path)
        size = path.stat().st_size
        is_pdf = has_pdf_header(path)

    with tenant_tx(deps.engine, tenant_id) as conn:
        if _current(conn, document_id, attempt) is None:
            log.info("superseded while fingerprinting; nothing written", extra=fields)
            return {"outcome": STOPPED}
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

        if not is_pdf:
            conn.execute(
                text(
                    "UPDATE assay.documents SET sha256 = :sha, status = 'failed', "
                    "error_code = 'not_a_pdf', completed_at = now() WHERE id = :id"
                ),
                {"id": document_id, "sha": digest},
            )
            _record(
                conn,
                tenant_id=tenant_id,
                document_id=document_id,
                outcome=FAILED,
                detail={"error": "not_a_pdf"},
            )
            log.info("not processed", extra=fields | {"error": "not_a_pdf"})
            return {"outcome": FAILED}

        conn.execute(
            text("UPDATE assay.documents SET sha256 = :sha, size_bytes = :size WHERE id = :id"),
            {"id": document_id, "sha": digest, "size": size},
        )
        _record(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            outcome=CONTINUE,
            detail={"companion": companion_file is not None},
        )
    log.info("fingerprinted", extra=fields)
    return {"outcome": CONTINUE}


# --- The worker task's grant ------------------------------------------------------------------


def claim(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """`claimed` with the attempt and the grant's token, `wait` when the tenant's worker
    slots are all taken, or `stopped` when the document is no longer to be processed.

    The token goes into the execution's state, from which Step Functions passes it to the
    task. It is never logged here."""
    tenant_id, document_id = _ids(event)
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    with tenant_tx(deps.engine, tenant_id) as conn:
        row = _current(conn, document_id, _attempt(event))
        if row is None or row.status != "processing":
            log.info("document is no longer processing; no worker", extra=fields)
            return {"outcome": STOPPED}
        token = grants.claim(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            attempt=row.attempt,
            max_per_tenant=deps.max_workers,
            ttl=deps.grant_ttl,
        )
    if token is None:
        log.info("no worker slot free for the tenant; waiting", extra=fields)
        return {"outcome": WAIT}
    log.info("worker slot claimed", extra=fields | {"attempt": row.attempt})
    return {"outcome": CLAIMED, "attempt": row.attempt, "token": token}


def release(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """End the document's grant, and report whether its task ever started."""
    tenant_id, document_id = _ids(event)
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    with tenant_tx(deps.engine, tenant_id) as conn:
        row = conn.execute(
            text(
                "SELECT (SELECT max(g.created_at) FROM assay.worker_grants g "
                "         WHERE g.document_id = :id) AS claimed_at, "
                "       (SELECT count(*) FROM assay.worker_grants g "
                "         WHERE g.document_id = :id) AS grants"
            ),
            {"id": document_id},
        ).one()
        started = False
        if row.claimed_at is not None:
            started = bool(
                conn.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM assay.document_events e "
                        "WHERE e.document_id = :id AND e.step = :step AND e.status = :running "
                        "AND e.at >= :since)"
                    ),
                    {
                        "id": document_id,
                        "step": progress.WORKER,
                        "running": progress.RUNNING,
                        "since": row.claimed_at,
                    },
                ).scalar_one()
            )
        ended = grants.end(conn, document_id=document_id, attempt=_attempt(event))
    if not started:
        # No capacity, an image that could not be pulled, a task that died before it bound
        # its session. The state machine tries again; this says so, loudly now and then.
        level = logging.ERROR if row.grants % REPORT_UNSTARTED_EVERY == 0 else logging.WARNING
        log.log(level, "the worker task did not start", extra=fields | {"tries": row.grants})
    log.info("worker grant released", extra=fields | {"ended": ended, "started": started})
    return {"worker_started": started}


# --- Failure ----------------------------------------------------------------------------------


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


def worker_failure(conn: Connection, document_id: uuid.UUID) -> tuple[str, str | None]:
    """Which of the worker task's steps was under way when it failed, and the exception's
    type if the task recorded one. From the task's own progress marks in the database, for
    the document's current attempt."""
    row = conn.execute(
        text(
            "SELECT e.step, e.status, e.detail ->> 'error_type' AS error_type "
            "FROM assay.document_events e "
            "JOIN assay.documents d ON d.tenant_id = e.tenant_id AND d.id = e.document_id "
            "WHERE e.document_id = :id AND e.step = ANY(:steps) AND e.attempt = d.attempt "
            "ORDER BY e.at DESC LIMIT 1"
        ),
        {"id": document_id, "steps": list(progress.WORKER_STEPS)},
    ).one_or_none()
    if row is None or row.status not in progress.MARKS:
        return progress.WORKER, None
    name = row.error_type if row.status == progress.ERROR else None
    if not (isinstance(name, str) and ERROR_TYPE.fullmatch(name)):
        name = None
    return str(row.step), name


def failed(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    tenant_id, document_id = _ids(event)
    step = event.get("failed_step")
    name = error_type(event)
    attempt = _attempt(event)
    with tenant_tx(deps.engine, tenant_id) as conn:
        if _current(conn, document_id, attempt) is None:
            # A newer attempt has taken over: its outcome is the document's, not this one's.
            grants.end(conn, document_id=document_id, attempt=attempt)
            log.warning(
                "a superseded attempt failed; the document is not changed",
                extra={"tenant_id": str(tenant_id), "document_id": str(document_id)},
            )
            return {"outcome": STOPPED}
        if step == progress.WORKER:
            step, recorded = worker_failure(conn, document_id)
            name = recorded or name
        code = failure_code(step)
        # Whatever failed, no grant outlives the attempt.
        grants.end(conn, document_id=document_id, attempt=attempt)
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


# --- Step 9 -----------------------------------------------------------------------------------


def companion(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """Store the companion file's values for this attempt (`companion_data.apply`)."""
    tenant_id, document_id = _ids(event)
    fields = {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    with tenant_tx(deps.engine, tenant_id) as conn:
        row = _current(conn, document_id, _attempt(event))
        if row is None or row.status != "processing":
            # Held, failed or a duplicate at an earlier step, or superseded: nothing to store.
            log.info("document is not processing; no companion step", extra=fields)
            return {"outcome": STOPPED}
        previous = conn.execute(
            text(
                "SELECT e.status FROM assay.document_events e WHERE e.document_id = :id "
                "AND e.step = :step AND e.attempt = :attempt ORDER BY e.at DESC LIMIT 1"
            ),
            {"id": document_id, "step": companion_data.STEP, "attempt": row.attempt},
        ).scalar_one_or_none()
        if previous is not None:
            log.info("companion step already done", extra=fields | {"outcome": previous})
            return {"outcome": str(previous)}
        storage = deps.storage.pipeline(tenant_id, SERVICE, f"companion-{document_id.hex[:12]}")
        applied = companion_data.apply(conn, storage, tenant_id=tenant_id, document_id=document_id)
    return {"outcome": applied.outcome}


STEPS = {
    "fingerprint": fingerprint,
    "claim": claim,
    "release": release,
    "companion": companion,
    "failed": failed,
}


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
            max_workers=int(os.environ.get("WORKER_MAX_PER_TENANT", DEFAULT_MAX_WORKERS)),
            grant_ttl=dt.timedelta(
                seconds=int(os.environ.get("WORKER_GRANT_SECONDS", DEFAULT_GRANT_SECONDS))
            ),
        )
    return _deps


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return handle(event, _state())
