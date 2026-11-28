"""Batches, their uploads and when a document may start (BUILD_SPEC.md sections 8 and 10).

Shared by the API (open, register, remove, close, discard) and the pipeline (scan results,
auto-close, scan timeouts, queueing again). Every function takes a connection as
`app_tenant` with `app.tenant_id` already set, so row-level security limits it to one
tenant; no function takes a tenant ID it could use to reach another.

The rules, in one place:

- A PDF starts only when its batch is closed and its scan, and its companion's scan if it
  has one, came back clean with the registered size and checksum (section 8).
- A batch may close only when every live file has a scan result, every companion is valid
  and matches a PDF by base name, and it holds at least one PDF (section 10). A batch still
  open after 24 hours is closed anyway: problem files are dropped or their PDF fails
  (owner decision 5, build step 6).
- Nothing here reads an object or lists a bucket; it works from rows only.
- Object keys are built by the caller, through a TSS storage context, and passed in.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Row, text

PDF = "pdf"
COMPANION = "companion"
# A batch upload, or a companion file attached or replaced after ingestion (migration 0011).
UPLOAD = "upload"
REPLACEMENT = "companion_replacement"

# Limits (section 10, and build step 6 condition a).
PDF_MAX_BYTES = 100 * 1024 * 1024
COMPANION_MAX_BYTES = 64 * 1024
MAX_PDFS_PER_BATCH = 200
# At most one companion per PDF.
MAX_COMPANIONS_PER_BATCH = MAX_PDFS_PER_BATCH
MAX_FILES_PER_REQUEST = 50
MAX_FILE_NAME = 255
MAX_BATCH_NAME = 200

# Timing (section 10, and build step 6 condition d).
OPEN_LIMIT = dt.timedelta(hours=24)
# An upload link lasts 15 minutes; a scan result usually follows within a minute or two.
SCAN_TIMEOUT = dt.timedelta(minutes=30)
# An eligible document with no execution started after this long is queued again.
REQUEUE_AFTER = dt.timedelta(minutes=15)

CONTENT_TYPES = {PDF: "application/pdf", COMPANION: "application/json"}
EXTENSIONS = {".pdf": PDF, ".json": COMPANION}

# File statuses that are final for the file (it will not change again on its own).
SCANNED = ("clean", "invalid", "mismatch", "infected", "unsupported", "scan_timeout")


class BatchError(Exception):
    """A refused request. `code` is a short code, safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Problem:
    """Why a batch cannot close yet. `file_id` is None for the batch as a whole."""

    file_id: uuid.UUID | None
    code: str


class BatchNotReady(BatchError):
    def __init__(self, problems: Sequence[Problem]) -> None:
        super().__init__("batch_not_ready")
        self.problems = list(problems)


@dataclass(frozen=True)
class NewFile:
    id: uuid.UUID
    kind: str
    file_name: str
    base_name: str
    size_bytes: int
    sha256: str
    s3_key: str

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES[self.kind]


def classify(file_name: str) -> tuple[str, str]:
    """(kind, base name) from a file name, or BatchError. The name is never used in a key."""
    name = file_name.strip()
    if (
        not name
        or len(name) > MAX_FILE_NAME
        or any(ch in name for ch in "/\\")
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        raise BatchError("invalid_file_name")
    stem, dot, extension = name.rpartition(".")
    kind = EXTENSIONS.get(f"{dot}{extension}".lower())
    if kind is None:
        raise BatchError("unsupported_file_type")
    if not stem.strip():
        raise BatchError("invalid_file_name")
    return kind, stem.strip().lower()


def check_size(kind: str, size_bytes: int) -> None:
    limit = PDF_MAX_BYTES if kind == PDF else COMPANION_MAX_BYTES
    if size_bytes <= 0:
        raise BatchError("empty_file")
    if size_bytes > limit:
        raise BatchError("file_too_large")


# --- Events ----------------------------------------------------------------------------


def document_event(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    step: str,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """One `document_events` row, stamped with the document's current attempt. `detail`
    holds IDs, codes and counts, never content."""
    conn.execute(
        text(
            "INSERT INTO assay.document_events "
            "(tenant_id, document_id, step, status, at, detail, attempt) "
            "VALUES (:tenant_id, :document_id, :step, :status, clock_timestamp(), "
            "CAST(:detail AS jsonb), "
            "(SELECT d.attempt FROM assay.documents d WHERE d.id = :document_id))"
        ),
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "step": step,
            "status": status,
            "detail": None if detail is None else json.dumps(detail, default=str),
        },
    )


# --- Batches ---------------------------------------------------------------------------


def lock_batch(conn: Connection, batch_id: uuid.UUID) -> Row[Any] | None:
    """The batch, locked for this transaction. Every change to a batch's files takes it, so
    a close, a scan result and the sweeper never interleave on one batch."""
    return conn.execute(
        text(
            "SELECT id, tenant_id, name, status, opened_by, opened_at, closed_at "
            "FROM assay.batches WHERE id = :id FOR UPDATE"
        ),
        {"id": batch_id},
    ).one_or_none()


def open_batch(
    conn: Connection, *, tenant_id: uuid.UUID, user_id: uuid.UUID, name: str | None
) -> uuid.UUID:
    """A new open batch, or BatchError("batch_already_open") (one open batch per user)."""
    existing = conn.execute(
        text("SELECT id FROM assay.batches WHERE opened_by = :user AND status = 'open'"),
        {"user": user_id},
    ).first()
    if existing is not None:
        raise BatchError("batch_already_open")
    batch_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO assay.batches (id, tenant_id, name, opened_by) "
            "VALUES (:id, :tenant_id, :name, :user)"
        ),
        {"id": batch_id, "tenant_id": tenant_id, "name": name, "user": user_id},
    )
    return batch_id


def _live_files(conn: Connection, batch_id: uuid.UUID) -> list[Row[Any]]:
    return list(
        conn.execute(
            text(
                "SELECT id, kind, base_name, status, document_id FROM assay.batch_files "
                "WHERE batch_id = :batch AND status <> 'removed' AND purpose = 'upload' "
                "FOR UPDATE"
            ),
            {"batch": batch_id},
        ).all()
    )


def check_new_files(
    conn: Connection, batch_id: uuid.UUID, files: Sequence[tuple[str, str]]
) -> None:
    """`files` is (kind, base name) of each file to register. Limits and name clashes."""
    if not files:
        raise BatchError("no_files")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise BatchError("too_many_files_in_request")
    live = _live_files(conn, batch_id)
    taken = {(f.kind, f.base_name) for f in live}
    for item in files:
        if item in taken:
            raise BatchError("duplicate_file_name")
        taken.add(item)
    kinds = [f.kind for f in live] + [kind for kind, _ in files]
    if kinds.count(PDF) > MAX_PDFS_PER_BATCH:
        raise BatchError("too_many_pdfs")
    if kinds.count(COMPANION) > MAX_COMPANIONS_PER_BATCH:
        raise BatchError("too_many_companion_files")


def insert_files(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    batch_id: uuid.UUID,
    user_id: uuid.UUID,
    files: Sequence[NewFile],
) -> None:
    """One `batch_files` row each; a PDF also gets its `documents` row (status `uploaded`),
    whose ID is the file's ID. The caller has refused the reserved document ID."""
    for f in files:
        if f.kind == PDF:
            conn.execute(
                text(
                    "INSERT INTO assay.documents (id, tenant_id, batch_id, uploaded_by, "
                    "file_name, size_bytes, status) VALUES (:id, :tenant_id, :batch, :user, "
                    ":file_name, :size, 'uploaded')"
                ),
                {
                    "id": f.id,
                    "tenant_id": tenant_id,
                    "batch": batch_id,
                    "user": user_id,
                    "file_name": f.file_name,
                    "size": f.size_bytes,
                },
            )
        conn.execute(
            text(
                "INSERT INTO assay.batch_files (id, tenant_id, batch_id, kind, file_name, "
                "base_name, size_bytes, sha256, s3_key, content_type, document_id, "
                "registered_by) VALUES (:id, :tenant_id, :batch, :kind, :file_name, :base, "
                ":size, :sha256, :key, :content_type, :document, :user)"
            ),
            {
                "id": f.id,
                "tenant_id": tenant_id,
                "batch": batch_id,
                "kind": f.kind,
                "file_name": f.file_name,
                "base": f.base_name,
                "size": f.size_bytes,
                "sha256": f.sha256,
                "key": f.s3_key,
                "content_type": f.content_type,
                "document": f.id if f.kind == PDF else None,
                "user": user_id,
            },
        )


def remove_file(conn: Connection, batch_id: uuid.UUID, file_id: uuid.UUID) -> None:
    """Take a file out of an open batch. Its object, if any, is never read again."""
    row = conn.execute(
        text(
            "SELECT id, kind FROM assay.batch_files "
            "WHERE id = :id AND batch_id = :batch AND status <> 'removed' FOR UPDATE"
        ),
        {"id": file_id, "batch": batch_id},
    ).one_or_none()
    if row is None:
        raise BatchError("file_not_found")
    conn.execute(
        text("UPDATE assay.batch_files SET status = 'removed' WHERE id = :id"), {"id": file_id}
    )
    if row.kind == PDF:
        conn.execute(
            text(
                "UPDATE assay.documents SET status = 'discarded', error_code = 'removed' "
                "WHERE id = :id AND status IN ('uploaded', 'scanning')"
            ),
            {"id": file_id},
        )


def close_problems(conn: Connection, batch_id: uuid.UUID) -> list[Problem]:
    files = _live_files(conn, batch_id)
    pdf_names = {f.base_name for f in files if f.kind == PDF}
    problems: list[Problem] = []
    if not pdf_names:
        problems.append(Problem(None, "batch_empty"))
    for f in files:
        if f.status == "registered":
            problems.append(Problem(f.id, "awaiting_scan"))
        elif f.status == "not_uploaded":
            problems.append(Problem(f.id, "not_uploaded"))
        if f.kind == COMPANION:
            if f.status == "invalid":
                problems.append(Problem(f.id, "companion_invalid"))
            elif f.status == "mismatch":
                problems.append(Problem(f.id, "upload_mismatch"))
            if f.base_name not in pdf_names:
                problems.append(Problem(f.id, "companion_unmatched"))
    return problems


def _pair(conn: Connection, batch_id: uuid.UUID) -> None:
    """Link each live companion to the live PDF with its base name."""
    conn.execute(
        text(
            "UPDATE assay.batch_files c SET document_id = p.id FROM assay.batch_files p "
            "WHERE c.batch_id = :batch AND c.kind = 'companion' AND c.status <> 'removed' "
            "AND c.purpose = 'upload' "
            "AND p.batch_id = :batch AND p.kind = 'pdf' AND p.status <> 'removed' "
            "AND p.base_name = c.base_name"
        ),
        {"batch": batch_id},
    )
    conn.execute(
        text(
            "UPDATE assay.documents d SET has_companion = true FROM assay.batch_files c "
            "WHERE c.batch_id = :batch AND c.kind = 'companion' AND c.status <> 'removed' "
            "AND c.purpose = 'upload' AND c.document_id = d.id"
        ),
        {"batch": batch_id},
    )


def _mark_closed(conn: Connection, batch_id: uuid.UUID) -> None:
    conn.execute(
        text("UPDATE assay.batches SET status = 'closed', closed_at = now() WHERE id = :id"),
        {"id": batch_id},
    )


def close(conn: Connection, batch_id: uuid.UUID) -> list[uuid.UUID]:
    """Close an open batch. Returns the documents to queue, once this transaction commits."""
    batch = lock_batch(conn, batch_id)
    if batch is None:
        raise BatchError("batch_not_found")
    if batch.status != "open":
        raise BatchError("batch_not_open")
    problems = close_problems(conn, batch_id)
    if problems:
        raise BatchNotReady(problems)
    _pair(conn, batch_id)
    _mark_closed(conn, batch_id)
    return evaluate(conn, batch_id)


def auto_close(conn: Connection, batch_id: uuid.UUID) -> list[uuid.UUID]:
    """Close a batch left open for 24 hours, whatever it holds (owner decision 5).

    Files never uploaded and companions with no PDF are dropped; a PDF whose companion is
    invalid fails (`evaluate`); files still waiting for a scan wait on in the closed batch.
    """
    batch = lock_batch(conn, batch_id)
    if batch is None or batch.status != "open":
        return []
    files = _live_files(conn, batch_id)
    live_pdfs = {f.base_name for f in files if f.kind == PDF and f.status != "not_uploaded"}
    for f in files:
        code: str | None = None
        if f.status == "not_uploaded":
            code = "not_uploaded"
        elif f.kind == COMPANION and f.base_name not in live_pdfs:
            code = "companion_unmatched"
        if code is None:
            continue
        conn.execute(
            text(
                "UPDATE assay.batch_files SET status = 'removed', error_code = :code WHERE id = :id"
            ),
            {"id": f.id, "code": code},
        )
        if f.kind == PDF:
            conn.execute(
                text(
                    "UPDATE assay.documents SET status = 'discarded', error_code = :code "
                    "WHERE id = :id AND status IN ('uploaded', 'scanning')"
                ),
                {"id": f.id, "code": code},
            )
    _pair(conn, batch_id)
    _mark_closed(conn, batch_id)
    return evaluate(conn, batch_id)


def discard(conn: Connection, batch_id: uuid.UUID) -> None:
    """Discard an open batch. Its documents are kept as `discarded` (owner decision 15);
    a quarantined one keeps its status, so the record of an infected upload stays."""
    batch = lock_batch(conn, batch_id)
    if batch is None:
        raise BatchError("batch_not_found")
    if batch.status != "open":
        raise BatchError("batch_not_open")
    conn.execute(
        text("UPDATE assay.batches SET status = 'discarded', closed_at = now() WHERE id = :id"),
        {"id": batch_id},
    )
    conn.execute(
        text(
            "UPDATE assay.batch_files SET status = 'removed' "
            "WHERE batch_id = :batch AND status IN ('registered', 'not_uploaded')"
        ),
        {"batch": batch_id},
    )
    conn.execute(
        text(
            "UPDATE assay.documents SET status = 'discarded' "
            "WHERE batch_id = :batch AND status <> 'quarantined'"
        ),
        {"batch": batch_id},
    )


# --- When a document may start ---------------------------------------------------------

# A clean PDF's outcome by its companion's status. None: no companion.
_BY_COMPANION: dict[str | None, tuple[str, str | None] | None] = {
    None: None,
    "clean": None,
    "registered": ("scanning", None),
    "not_uploaded": ("scanning", None),
    "invalid": ("failed", "companion_invalid"),
    "mismatch": ("failed", "companion_mismatch"),
    "infected": ("quarantined", "companion_infected"),
    "unsupported": ("on_hold", "companion_not_scanned"),
    "scan_timeout": ("on_hold", "companion_not_scanned"),
}


def evaluate(conn: Connection, batch_id: uuid.UUID) -> list[uuid.UUID]:
    """In a closed batch, move each waiting PDF on; return those now eligible.

    An eligible document becomes `processing` with `queued_at` set, and gets a `queued`
    event. The caller sends one ingest job for each ID returned, after its commit.
    """
    docs = conn.execute(
        text(
            "SELECT d.id, d.tenant_id, d.status, p.status AS pdf_status, p.base_name "
            "FROM assay.documents d JOIN assay.batch_files p "
            "ON p.tenant_id = d.tenant_id AND p.id = d.id "
            "WHERE d.batch_id = :batch AND d.status IN ('uploaded', 'scanning') "
            "FOR UPDATE OF d"
        ),
        {"batch": batch_id},
    ).all()
    companions = {
        c.base_name: c.status
        for c in conn.execute(
            text(
                "SELECT base_name, status FROM assay.batch_files "
                "WHERE batch_id = :batch AND kind = 'companion' AND status <> 'removed' "
                "AND purpose = 'upload'"
            ),
            {"batch": batch_id},
        ).all()
    }
    eligible: list[uuid.UUID] = []
    for doc in docs:
        if doc.pdf_status == "registered":
            outcome: tuple[str, str | None] | None = ("scanning", None)
        elif doc.pdf_status == "clean":
            outcome = _BY_COMPANION.get(companions.get(doc.base_name), ("scanning", None))
        else:
            # The PDF's own scan already set the document's status.
            continue
        if outcome is None:
            conn.execute(
                text(
                    "UPDATE assay.documents SET status = 'processing', queued_at = now() "
                    "WHERE id = :id"
                ),
                {"id": doc.id},
            )
            document_event(
                conn,
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                step="queued",
                status="queued",
            )
            eligible.append(doc.id)
            continue
        status, error = outcome
        if status == doc.status and error is None:
            continue
        conn.execute(
            text(
                "UPDATE assay.documents SET status = :status, error_code = :error, "
                "completed_at = CASE WHEN :status = 'scanning' THEN NULL ELSE now() END "
                "WHERE id = :id"
            ),
            {"id": doc.id, "status": status, "error": error},
        )
        if error is not None:
            document_event(
                conn,
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                step="companion",
                status=status,
                detail={"error": error},
            )
    return eligible


# --- Scan results ----------------------------------------------------------------------


@dataclass(frozen=True)
class ScanOutcome:
    """What the scan step decided for one file. `error_detail` names keys, never values."""

    status: str  # one of SCANNED
    scan_result: str
    version_id: str | None
    error_code: str | None = None
    error_detail: list[dict[str, str | None]] | None = None
    # For an infected PDF: where it now is (the caller built the key through the TSS).
    quarantine_key: str | None = None


# A PDF's document by its file's scan outcome. None: the document waits for its batch.
_DOCUMENT_BY_FILE: dict[str, str | None] = {
    "clean": None,
    "mismatch": "failed",
    "infected": "quarantined",
    "unsupported": "on_hold",
    "scan_timeout": "on_hold",
}


def lock_file(conn: Connection, file_id: uuid.UUID) -> Row[Any] | None:
    return conn.execute(
        text(
            "SELECT id, tenant_id, batch_id, kind, size_bytes, sha256, s3_key, status, "
            "version_id, document_id, purpose, registered_by, notice_sent_at "
            "FROM assay.batch_files WHERE id = :id FOR UPDATE"
        ),
        {"id": file_id},
    ).one_or_none()


def record_scan(conn: Connection, file: Row[Any], outcome: ScanOutcome) -> list[uuid.UUID]:
    """Store one file's scan outcome; returns the documents now eligible (closed batch only).

    The caller holds the batch's lock and has checked that the file is still `registered`:
    the first decisive result wins, and a later version of the same key is never used.
    """
    conn.execute(
        text(
            "UPDATE assay.batch_files SET status = :status, scan_result = :result, "
            "version_id = :version, scanned_at = now(), error_code = :error, "
            "error_detail = CAST(:detail AS jsonb) WHERE id = :id"
        ),
        {
            "id": file.id,
            "status": outcome.status,
            "result": outcome.scan_result,
            "version": outcome.version_id,
            "error": outcome.error_code,
            "detail": None if outcome.error_detail is None else json.dumps(outcome.error_detail),
        },
    )
    if file.kind == PDF:
        document_status = _DOCUMENT_BY_FILE.get(outcome.status)
        detail: dict[str, Any] = {"result": outcome.scan_result}
        if outcome.error_code:
            detail["error"] = outcome.error_code
        document_event(
            conn,
            tenant_id=file.tenant_id,
            document_id=file.id,
            step="scan",
            status=outcome.status,
            detail=detail,
        )
        if document_status is not None:
            conn.execute(
                text(
                    "UPDATE assay.documents SET status = :status, "
                    "error_code = COALESCE(:error, :result), completed_at = now(), "
                    "s3_key = COALESCE(:quarantine, s3_key) "
                    "WHERE id = :id AND status IN ('uploaded', 'scanning')"
                ),
                {
                    "id": file.id,
                    "status": document_status,
                    "error": outcome.error_code,
                    "result": outcome.scan_result.lower(),
                    "quarantine": outcome.quarantine_key,
                },
            )
    return _evaluate_if_closed(conn, file.batch_id)


def _evaluate_if_closed(conn: Connection, batch_id: uuid.UUID) -> list[uuid.UUID]:
    status = conn.execute(
        text("SELECT status FROM assay.batches WHERE id = :id"), {"id": batch_id}
    ).scalar_one()
    return evaluate(conn, batch_id) if status == "closed" else []


# --- The sweeper -----------------------------------------------------------------------


def stale_open_batches(conn: Connection, now: dt.datetime) -> list[uuid.UUID]:
    rows = conn.execute(
        text("SELECT id FROM assay.batches WHERE status = 'open' AND opened_at < :before"),
        {"before": now - OPEN_LIMIT},
    ).all()
    return [r.id for r in rows]


def overdue_files(conn: Connection, now: dt.datetime) -> list[Row[Any]]:
    """Registered files with no scan result after SCAN_TIMEOUT."""
    return list(
        conn.execute(
            text(
                "SELECT id, batch_id, kind, s3_key FROM assay.batch_files "
                "WHERE status = 'registered' AND registered_at < :before"
            ),
            {"before": now - SCAN_TIMEOUT},
        ).all()
    )


def mark_overdue(conn: Connection, file_id: uuid.UUID, *, uploaded: bool) -> list[uuid.UUID]:
    """An overdue file: `scan_timeout` (it is in the bucket; held, and alarmed by the
    caller) or `not_uploaded` (nothing arrived before its link expired)."""
    file = lock_file(conn, file_id)
    if file is None or file.status != "registered":
        return []
    if not uploaded:
        conn.execute(
            text(
                "UPDATE assay.batch_files SET status = 'not_uploaded', error_code = "
                "'not_uploaded' WHERE id = :id"
            ),
            {"id": file_id},
        )
        if file.kind == PDF:
            # In an open batch the admin removes it or the batch drops it at close. In a
            # closed one (auto-closed while it waited) it can never start.
            conn.execute(
                text(
                    "UPDATE assay.documents d SET status = 'discarded', error_code = "
                    "'not_uploaded' FROM assay.batches b WHERE d.id = :id AND b.id = "
                    "d.batch_id AND b.status = 'closed' AND d.status IN ('uploaded', 'scanning')"
                ),
                {"id": file_id},
            )
        return _evaluate_if_closed(conn, file.batch_id)
    return record_scan(
        conn,
        file,
        ScanOutcome(
            status="scan_timeout",
            scan_result="TIMEOUT",
            version_id=None,
            error_code="scan_timeout",
        ),
    )


def requeue_candidates(conn: Connection, now: dt.datetime) -> list[uuid.UUID]:
    """Eligible documents queued a while ago (or never) with no execution started. Marks
    them queued again now; the caller sends their jobs after its commit."""
    rows = conn.execute(
        text(
            "UPDATE assay.documents d SET queued_at = now() "
            "WHERE d.status = 'processing' AND (d.queued_at IS NULL OR d.queued_at < :before) "
            "AND NOT EXISTS (SELECT 1 FROM assay.document_events e "
            "WHERE e.tenant_id = d.tenant_id AND e.document_id = d.id AND e.step = 'start' "
            "AND e.attempt = d.attempt) "
            "RETURNING d.id"
        ),
        {"before": now - REQUEUE_AFTER},
    ).all()
    return [r.id for r in rows]


# --- After ingestion: process again, mark as reviewed, replace the companion file -------

# "Process again" (owner decision 3, second half). `processing` is here for a document whose
# attempt has ended without a final status: every document until the last stage is built.
REPROCESSABLE = ("failed", "ready", "needs_review", "processing")
# A `processing` document is under way while it has a live worker grant, or anything
# happened to its current attempt this recently. Until stage 4 a document that passed its
# steps stays `processing`, so its status cannot say whether an attempt runs. Stage 4 sets
# the final status inside the worker task and replaces this rule (owner, 2026-11-27).
RUNNING_GRACE = dt.timedelta(minutes=5)
# Every event is stamped with its document's current attempt, also one written when no
# attempt runs. Such an event carries this key (true) in its detail and is not the attempt's
# activity. Today that is the scan step's event for a companion file attached or replaced
# after ingestion: the replacement is complete in the transaction that writes it, and it must
# not hold "Process again" for five minutes (owner, 2026-11-27). The pipeline's own companion
# step, which ends every attempt, writes its event without the key and still counts.
OUTSIDE_ATTEMPT = "outside_attempt"
# A companion file may be attached or replaced once the document has been through the
# pipeline's checks (owner decision A4).
COMPANION_REPLACEABLE = ("ready", "needs_review", "processing")


def _under_way(conn: Connection, document_id: uuid.UUID, now: dt.datetime) -> bool:
    row = conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM assay.worker_grants g WHERE g.document_id = d.id "
            "               AND g.ended_at IS NULL AND g.expires_at > now()) AS granted, "
            "       (SELECT max(e.at) FROM assay.document_events e "
            "         WHERE e.tenant_id = d.tenant_id AND e.document_id = d.id "
            "           AND e.attempt = d.attempt AND e.step <> 'viewed' "
            # Only the JSON value true takes an event out; anything else still counts.
            "           AND (e.detail -> :outside) IS DISTINCT FROM 'true'::jsonb) "
            "       AS last_event "
            "FROM assay.documents d WHERE d.id = :id"
        ),
        {"id": document_id, "outside": OUTSIDE_ATTEMPT},
    ).one()
    recent = row.last_event is not None and row.last_event > now - RUNNING_GRACE
    return bool(row.granted) or recent


def reprocess(
    conn: Connection, *, tenant_id: uuid.UUID, document_id: uuid.UUID, now: dt.datetime
) -> tuple[int, str]:
    """Start a new attempt of a document. Returns (the new attempt, the status it had).

    The document becomes `processing` and gets a `queued` event; the caller sends its ingest
    job after the commit. Everything restarts at fingerprint, from the upload at the version
    that scanned clean. A failed document gives up its hash first, so fingerprint checks it
    again: if the same file became an original in the meantime, it ends `duplicate`.
    Refused (BatchError): a document that never had a clean PDF, one that is quarantined, on
    hold, a duplicate or discarded, and one whose attempt is under way.
    """
    row = conn.execute(
        text(
            "SELECT d.status, d.attempt, EXISTS (SELECT 1 FROM assay.batch_files f "
            "WHERE f.tenant_id = d.tenant_id AND f.id = d.id AND f.status = 'clean' "
            "AND f.version_id IS NOT NULL) AS clean "
            "FROM assay.documents d WHERE d.id = :id FOR UPDATE OF d"
        ),
        {"id": document_id},
    ).one_or_none()
    if row is None:
        raise BatchError("document_not_found")
    if row.status not in REPROCESSABLE or not row.clean:
        raise BatchError("not_reprocessable")
    if row.status == "processing" and _under_way(conn, document_id, now):
        raise BatchError("attempt_running")
    attempt = int(row.attempt) + 1
    conn.execute(
        text(
            "UPDATE assay.worker_grants SET ended_at = now() "
            "WHERE document_id = :id AND ended_at IS NULL"
        ),
        {"id": document_id},
    )
    conn.execute(
        text(
            "UPDATE assay.documents SET attempt = :attempt, status = 'processing', "
            "error_code = NULL, completed_at = NULL, queued_at = now(), "
            "sha256 = CASE WHEN status = 'failed' THEN NULL ELSE sha256 END WHERE id = :id"
        ),
        {"id": document_id, "attempt": attempt},
    )
    document_event(
        conn,
        tenant_id=tenant_id,
        document_id=document_id,
        step="queued",
        status="queued",
        detail={"reprocess": True, "previous_status": row.status},
    )
    return attempt, str(row.status)


def mark_reviewed(
    conn: Connection, *, tenant_id: uuid.UUID, document_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """`needs_review` becomes `ready` (owner decision 7). Refused for any other status."""
    changed = conn.execute(
        text(
            "UPDATE assay.documents SET status = 'ready' "
            "WHERE id = :id AND status = 'needs_review' RETURNING id"
        ),
        {"id": document_id},
    ).first()
    if changed is None:
        exists = conn.execute(
            text("SELECT 1 FROM assay.documents WHERE id = :id"), {"id": document_id}
        ).first()
        raise BatchError("not_needs_review" if exists else "document_not_found")
    document_event(
        conn,
        tenant_id=tenant_id,
        document_id=document_id,
        step="review",
        status="reviewed",
        detail={"user_id": str(user_id)},
    )


def replacement_target(conn: Connection, document_id: uuid.UUID) -> Row[Any]:
    """The document a companion file is to be attached to, locked: its batch (for the inbox
    key) and the companion file its current values came from. BatchError if it may not have
    one now, or one is already on its way."""
    row = conn.execute(
        text(
            "SELECT d.id, d.batch_id, d.status, d.companion_file_id, "
            "(SELECT f.version_id FROM assay.batch_files f WHERE f.tenant_id = d.tenant_id "
            "  AND f.id = d.companion_file_id) AS companion_version_id "
            "FROM assay.documents d WHERE d.id = :id FOR UPDATE OF d"
        ),
        {"id": document_id},
    ).one_or_none()
    if row is None:
        raise BatchError("document_not_found")
    if row.status not in COMPANION_REPLACEABLE:
        raise BatchError("companion_not_allowed")
    pending = conn.execute(
        text(
            "SELECT 1 FROM assay.batch_files WHERE document_id = :id "
            "AND purpose = 'companion_replacement' AND status = 'registered'"
        ),
        {"id": document_id},
    ).first()
    if pending is not None:
        raise BatchError("companion_pending")
    return row


def insert_replacement(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    file: NewFile,
) -> None:
    """Register a companion file against its document. It is uploaded to the inbox and
    scanned like any upload; the scan step applies it if it is clean and valid."""
    if file.kind != COMPANION:
        raise BatchError("unsupported_file_type")
    conn.execute(
        text(
            "INSERT INTO assay.batch_files (id, tenant_id, batch_id, kind, file_name, "
            "base_name, size_bytes, sha256, s3_key, content_type, document_id, "
            "registered_by, purpose) VALUES (:id, :tenant_id, :batch, :kind, :file_name, "
            ":base, :size, :sha256, :key, :content_type, :document, :user, :purpose)"
        ),
        {
            "id": file.id,
            "tenant_id": tenant_id,
            "batch": batch_id,
            "kind": file.kind,
            "file_name": file.file_name,
            "base": file.base_name,
            "size": file.size_bytes,
            "sha256": file.sha256,
            "key": file.s3_key,
            "content_type": file.content_type,
            "document": document_id,
            "user": user_id,
            "purpose": REPLACEMENT,
        },
    )


def tenants_with_work(conn: Connection, now: dt.datetime) -> list[uuid.UUID]:
    """Every tenant with sweeper work (pipeline.tenants_with_work, migration 0008)."""
    rows = conn.execute(
        text("SELECT pipeline.tenants_with_work(:open, :registered, :queued) AS tenant_id"),
        {
            "open": now - OPEN_LIMIT,
            "registered": now - SCAN_TIMEOUT,
            "queued": now - REQUEUE_AFTER,
        },
    ).all()
    return sorted(r.tenant_id for r in rows)


def tenant_for_upload(conn: Connection, key: str) -> uuid.UUID | None:
    """The tenant of the registered upload with this inbox key (pipeline.tenant_for_upload)."""
    value = conn.execute(
        text("SELECT pipeline.tenant_for_upload(:key)"), {"key": key}
    ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def tenant_is_active(conn: Connection, tenant_id: uuid.UUID) -> bool:
    status = conn.execute(
        text("SELECT status FROM assay.tenants WHERE id = :id"), {"id": tenant_id}
    ).scalar_one_or_none()
    return status == "active"
