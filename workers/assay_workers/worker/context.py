"""What every step of the worker task works with: its document, its dependencies, the PDF.

Shared by `run` (the loop and the `inspect` step) and `text` (step 5), so neither imports
the other.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, text

from assay_db import batches
from assay_workers.runtime import TenantStorage
from assay_workers.sandbox.runner import InspectResult, PageImage, PageRecord
from assay_workers.worker.ocr import Ocr

log = logging.getLogger("assay.worker")

CONTINUE, ON_HOLD, FAILED, NOT_ELIGIBLE = "continue", "on_hold", "failed", "not_eligible"
CHECKSUM_MISMATCH = "checksum_mismatch"
# The prefix of an event's error type that names a sandbox ending: `sandbox.cpu_limit`.
SANDBOX = "sandbox"


class ChecksumMismatch(Exception):
    """The stored PDF is not the one that was registered and scanned."""


class Pages(Protocol):
    """The sandbox, as the text step uses it: each call is one confined child, and `handle`
    gets one checked record at a time."""

    def read_text(
        self, source: Path, first: int, last: int, handle: Callable[[PageRecord], None]
    ) -> None: ...

    def render(
        self, source: Path, pages: Sequence[int], handle: Callable[[PageImage], None]
    ) -> None: ...


@dataclass(frozen=True)
class TextSettings:
    """Step 5's settings, all from stack config (never from code)."""

    # A page whose quality score is below this goes to Textract (`textQualityThreshold`).
    threshold: float
    # A document with more pages than this is not read at all (`textMaxPages`).
    max_pages: int
    # What one Textract page costs, in US dollars (`textractPricePerPage`).
    price_per_page: Decimal

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("the quality threshold must be above 0 and at most 1")
        if self.max_pages < 1:
            raise ValueError("the page limit must be at least 1")
        if not self.price_per_page.is_finite() or self.price_per_page < 0:
            raise ValueError("the price of a page must be zero or more")


@dataclass(frozen=True)
class Deps:
    # Bound to the job's grant: it sees one document of one tenant.
    engine: Engine
    # The document's storage, from the TSS's /resolve-worker. Asked for when needed; each
    # call gives fresh credentials (they last 15 minutes).
    storage: Callable[[], TenantStorage]
    # Opens the PDF in a sandbox child.
    inspect: Callable[[Path], InspectResult]
    # The task's writable directory.
    work_dir: Path
    # Step 5: sandbox children that read and draw pages, Textract, and the step's settings.
    pages: Pages
    ocr: Ocr
    text: TextSettings


@dataclass(frozen=True)
class Document:
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    files: tuple[Any, ...]
    attempt: int = 1
    # Where the document's PDF is now: its `originals/` key once an attempt has copied it.
    s3_key: str | None = None

    def file(self, kind: str) -> Any | None:
        return next((f for f in self.files if f.kind == kind), None)

    @property
    def fields(self) -> dict[str, str]:
        return {"tenant_id": str(self.tenant_id), "document_id": str(self.document_id)}


@dataclass
class Source:
    """The document's PDF, downloaded once for all the task's steps.

    `shared` is the directory the sandbox user can read (and only read): it holds the PDF.
    `private` is the parent's own, closed to every other user: page pictures and text wait
    there between a child's answer and Textract or the bucket.
    """

    doc: Document
    deps: Deps
    shared: Path
    private: Path
    # The storage the PDF was downloaded with, for a step that goes on at once.
    storage: TenantStorage | None = field(default=None, repr=False)
    _path: Path | None = field(default=None, repr=False)

    def fetch(self) -> Path:
        """The PDF at exactly the version that scanned clean, its SHA-256 checked against
        the registration once more. Raises `ChecksumMismatch`."""
        if self._path is not None:
            return self._path
        pdf = self.doc.file(batches.PDF)
        if pdf is None or pdf.version_id is None:
            raise RuntimeError("the document has no clean PDF")
        path = self.shared / "document.pdf"
        self.storage = self.deps.storage()
        digest = self.storage.objects.download(pdf.s3_key, pdf.version_id, path)
        path.chmod(0o444)
        if digest != pdf.sha256:
            raise ChecksumMismatch
        self._path = path
        return path


def event(
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


def finish(
    deps: Deps,
    doc: Document,
    *,
    step: str,
    status: str,
    outcome: str,
    code: str,
    error_type: str | None = None,
    level: int = logging.INFO,
) -> str:
    """The document stops here: held or failed, with its code. `error_type` is a name for
    master admins (how a sandbox child ended); a tenant user sees the code's words only."""
    detail = {"error": code} if error_type is None else {"error": code, "error_type": error_type}
    with deps.engine.begin() as conn:
        event(conn, doc, step, outcome, detail)
        conn.execute(
            text(
                "UPDATE assay.documents SET status = :status, error_code = :code, "
                "completed_at = now() WHERE id = :id AND status = 'processing'"
            ),
            {"id": doc.document_id, "status": status, "code": code},
        )
    log.log(level, "not processed", extra=doc.fields | {"error": code, "step": step})
    return outcome


def checksum_mismatch(deps: Deps, doc: Document, step: str) -> str:
    """The scan step and the fingerprint step both checked this version; a difference now
    means the object changed under its version, which S3 does not allow. Fail loudly."""
    return finish(
        deps,
        doc,
        step=step,
        status="failed",
        outcome=FAILED,
        code=CHECKSUM_MISMATCH,
        level=logging.ERROR,
    )
