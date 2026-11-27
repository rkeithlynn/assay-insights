"""Step 5, text (BUILD_SPEC.md section 8; build step 6, second half, stage 2).

For one document's attempt, in the worker task's main process:

1. **The text layer.** Sandbox children read it, 25 pages a child, and send one record a page
   down the page channel (`sandbox.runner.read_text`). Each record is scored as it arrives
   (`quality.score`) and its text goes to a file in the parent's private directory. The
   parent holds one record at a time and a few numbers a page, never the document.
2. **Textract.** Pages whose score is below the threshold are drawn by sandbox children, 8 a
   child (`sandbox.runner.render`). Each picture is written to the private directory as it
   arrives, then sent to Textract, two calls at a time (`ocr`). The parent never opens a
   picture. Every successful call writes its `ai_usage` row at once, so a page that was
   billed is recorded even if the step later fails.
3. **The text object.** `derived/text/{doc}.json`, written to a file page by page and
   uploaded from it with fresh credentials for this one document. Its S3 version ID goes
   into the step's event and into `documents.text_version_id`: readers use that version, so
   a late write by a superseded attempt is never read, and an older attempt's version can be
   found without listing (owner decision, 2026-11-27: every version is kept).
4. **The rows.** One `pages` row a page, for this attempt, and the content pointer
   (`documents.content_attempt`), in the transaction that writes the step's result.

A page is `native` (its text is the text layer's) or `ocr` (its text is what Textract read on
the page's picture, the typed parts included). When a page goes to Textract although its text
layer reads well (an image covers part of it, or annotations draw on it), the text layer's
text is kept too, as `text_layer`, marked `is_source: false` (owner, addition 1).

**Only a page's source text (`text`) is the page's text.** It alone may feed extraction,
search and embedding in the later stages. `text_layer` is there to compare with, and nothing
in this stage reads it.

All text here is content chosen by whoever made the PDF: it is data. It is never logged, and
never becomes a path, a key or SQL. No page text is written to the database at all.

What stops a document here, and its code:

| Case | Status | Code |
| --- | --- | --- |
| More pages than `textMaxPages` | `failed` | `too_many_pages` |
| A sandbox child did not end well | `failed` | `page_unreadable`, error type `sandbox.<ending>` |
| The text object would exceed 128 MB | `failed` | `text_too_large` |
| Textract busy for five minutes, or refusing the task | the step fails | `text_failed` |

A failed step's event keeps the error's own name (`ThrottlingException`, for one), never its
message. A page Textract will not take is recorded as unread (`problem` is Textract's error
name) and the document goes on; the step's event counts such pages.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import text as sql

from assay_workers.sandbox import runner
from assay_workers.sandbox.runner import (
    PageImage,
    PageRecord,
    SandboxError,
    SandboxLimit,
    SandboxNotStarted,
)
from assay_workers.worker import quality
from assay_workers.worker.context import (
    CONTINUE,
    FAILED,
    NOT_ELIGIBLE,
    SANDBOX,
    ChecksumMismatch,
    Deps,
    Document,
    Source,
    checksum_mismatch,
    event,
    finish,
)
from assay_workers.worker.ocr import OPERATION, SERVICE, OcrPage, PageRefused

log = logging.getLogger("assay.worker")

STEP = "text"
TOO_MANY_PAGES = "too_many_pages"
PAGE_UNREADABLE = "page_unreadable"
TEXT_TOO_LARGE = "text_too_large"
STOPPED_AT_A_LIMIT = "reading a page was stopped at a sandbox limit"
ENDED_UNEXPECTEDLY = "reading a page ended unexpectedly"

NATIVE, OCR = "native", "ocr"
SCHEMA_VERSION = 1
CONTENT_TYPE = "application/json"
MAX_TEXT_OBJECT_BYTES = 128 * 1024 * 1024
# Textract calls in flight from one task. The account allows 25 a second for everyone.
OCR_CONCURRENCY = 2
JOB = "ingest"
UNIT = "page"

USAGE = sql(
    "INSERT INTO assay.ai_usage (tenant_id, job, service, operation, model_version, "
    "document_id, attempt, page_no, input_tokens, output_tokens, units, unit, unit_price, "
    "cost, request_id) VALUES (:tenant, :job, :service, :operation, :version, :document, "
    ":attempt, :page, 0, 0, 1, :unit, :price, :price, :request)"
)
# No conflict target: naming one needs SELECT, which the worker does not have on `pages`.
PAGE_ROW = sql(
    "INSERT INTO assay.pages (tenant_id, document_id, attempt, page_no, text_source, "
    "quality_score, ocr_confidence, char_count) VALUES (:tenant, :document, :attempt, :page, "
    ":source, :quality, :confidence, :chars) ON CONFLICT DO NOTHING"
)


@dataclass
class PageFacts:
    """Numbers about one page. Its text is in a file, at `layer_at` and `ocr_at`."""

    page: int
    width: float
    height: float
    rotation: int
    image_coverage: float
    annotations: int
    quality: float
    layer_chars: int
    # The text layer reads well by itself; kept beside Textract's text if the page goes there.
    layer_usable: bool
    to_textract: bool
    layer_at: int
    ocr_at: int | None = None
    ocr_chars: int = 0
    confidence: float | None = None
    # Textract's error name, for a page it would not take.
    problem: str | None = None

    @property
    def source(self) -> str:
        return OCR if self.to_textract else NATIVE

    @property
    def unread(self) -> bool:
        return self.problem is not None


class Spool:
    """Lines of JSON in a file of the parent's own: written once, read back by position."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._out: BinaryIO = path.open("wb")
        self._at = 0

    def add(self, value: dict[str, Any]) -> int:
        at = self._at
        line = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
        self._out.write(line)
        self._at += len(line)
        return at

    def reader(self) -> BinaryIO:
        self._out.flush()
        return self._path.open("rb")

    def close(self) -> None:
        self._out.close()


def _line(reader: BinaryIO, at: int) -> dict[str, Any]:
    reader.seek(at)
    value: dict[str, Any] = json.loads(reader.readline())
    return value


def _chunks(pages: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(pages), size):
        yield pages[start : start + size]


def _usage(deps: Deps, doc: Document, page: int, result: OcrPage) -> None:
    """One Textract call, recorded as soon as it succeeded (owner decision C)."""
    price = deps.text.price_per_page
    with deps.engine.begin() as conn:
        conn.execute(
            USAGE,
            {
                "tenant": doc.tenant_id,
                "job": JOB,
                "service": SERVICE,
                "operation": OPERATION,
                "version": result.model_version,
                "document": doc.document_id,
                "attempt": doc.attempt,
                "page": page,
                "unit": UNIT,
                "price": price,
                "request": result.request_id,
            },
        )


def _read_one(deps: Deps, path: Path, stop: threading.Event) -> OcrPage | PageRefused | None:
    """In a worker thread: one picture to Textract. The bytes are read and passed on; they
    are never decoded here. None: the picture was not sent, because an earlier call had
    already failed the step (`stop`)."""
    try:
        if stop.is_set():
            return None
        return deps.ocr.read(path.read_bytes())
    except PageRefused as refused:
        return refused
    except BaseException:
        # The step is failing: no further page is sent. A call already under way finishes.
        stop.set()
        raise
    finally:
        path.unlink(missing_ok=True)


def _send_to_textract(
    deps: Deps,
    doc: Document,
    path: Path,
    pages: Sequence[int],
    facts: dict[int, PageFacts],
    spool: Spool,
    private: Path,
) -> None:
    """Draw `pages` in sandbox children, 8 a child, and have Textract read each picture."""
    for chunk in _chunks(pages, runner.RENDER_PAGES_PER_CHILD):
        pictures: dict[int, Path] = {}

        def keep(image: PageImage) -> None:
            target = private / f"page-{image.page}.png"
            target.write_bytes(image.data)
            pictures[image.page] = target  # noqa: B023  (called before the loop moves on)

        deps.pages.render(path, chunk, keep)
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=OCR_CONCURRENCY) as pool:
            calls: list[tuple[int, Future[OcrPage | PageRefused | None]]] = [
                (page, pool.submit(_read_one, deps, pictures[page], stop)) for page in chunk
            ]
            failure: Exception | None = None
            try:
                for page, call in calls:
                    # Once a call has failed, no page that was not yet under way is sent
                    # (`stop`). One that was under way is waited for: if it succeeded it was
                    # billed, and a page that was billed is recorded, whatever happened to
                    # its neighbour.
                    try:
                        result = call.result()
                    except Exception as exc:
                        if failure is None:
                            failure = exc
                        continue
                    if result is None:
                        continue
                    fact = facts[page]
                    if isinstance(result, PageRefused):
                        fact.problem = result.code
                        continue
                    _usage(deps, doc, page, result)
                    fact.ocr_at = spool.add({"text": result.text, "words": result.words})
                    fact.ocr_chars = quality.legibility(result.text)[0]
                    fact.confidence = result.confidence
                if failure is not None:
                    raise failure
            finally:
                for _page, call in calls:
                    call.cancel()
                for leftover in pictures.values():
                    leftover.unlink(missing_ok=True)


def _page_object(fact: PageFacts, reader: BinaryIO) -> dict[str, Any]:
    layer = _line(reader, fact.layer_at)
    page: dict[str, Any] = {
        "page": fact.page,
        "source": fact.source,
        "quality_score": fact.quality,
        "ocr_confidence": fact.confidence,
        "width": fact.width,
        "height": fact.height,
        "rotation": fact.rotation,
        "image_coverage": fact.image_coverage,
        "annotations": fact.annotations,
    }
    if not fact.to_textract:
        return page | {
            "char_count": fact.layer_chars,
            "text": layer["text"],
            "words": layer["words"],
        }
    read = {"text": "", "words": []} if fact.ocr_at is None else _line(reader, fact.ocr_at)
    page |= {"char_count": fact.ocr_chars, "text": read["text"], "words": read["words"]}
    if fact.problem is not None:
        page["problem"] = fact.problem
    if fact.layer_usable:
        # Not the page's text: what the text layer said, kept to compare with (addition 1).
        page["text_layer"] = {"is_source": False, "text": layer["text"]}
    return page


def write_text_object(
    target: Path,
    *,
    document_id: uuid.UUID,
    attempt: int,
    threshold: float,
    facts: Sequence[PageFacts],
    reader: BinaryIO,
    limit: int = MAX_TEXT_OBJECT_BYTES,
) -> bool:
    """The text object, written page by page. False if it would exceed `limit`."""
    head = {
        "schema": SCHEMA_VERSION,
        "document_id": str(document_id),
        "attempt": attempt,
        "threshold": threshold,
    }
    written = 0
    with target.open("wb") as out:
        opening = json.dumps(head, separators=(",", ":")).encode("utf-8")[:-1] + b',"pages":['
        out.write(opening)
        written += len(opening)
        for index, fact in enumerate(facts):
            page = json.dumps(_page_object(fact, reader), separators=(",", ":")).encode("utf-8")
            piece = (b"," if index else b"") + page
            written += len(piece)
            if written + 2 > limit:
                return False
            out.write(piece)
        out.write(b"]}")
    return True


def text(doc: Document, deps: Deps, source: Source) -> str:
    settings = deps.text
    with deps.engine.begin() as conn:
        row = conn.execute(
            sql("SELECT status, attempt, page_count FROM assay.documents WHERE id = :id"),
            {"id": doc.document_id},
        ).one_or_none()
    if row is None or row.status != "processing" or row.attempt != doc.attempt:
        log.warning("not this attempt's turn; no text read", extra=doc.fields)
        return NOT_ELIGIBLE
    total = row.page_count
    if total is None or total < 1:
        raise RuntimeError("the document has no page count")
    if total > settings.max_pages:
        return finish(deps, doc, step=STEP, status="failed", outcome=FAILED, code=TOO_MANY_PAGES)
    try:
        path = source.fetch()
    except ChecksumMismatch:
        return checksum_mismatch(deps, doc, STEP)

    spool = Spool(source.private / "pages.jsonl")
    facts: dict[int, PageFacts] = {}

    def scored(record: PageRecord) -> None:
        # A page with more text than a record holds is a page whose text layer cannot be
        # used: it has content, and none of it is in hand.
        result = quality.score(
            record.text,
            image_coverage=record.image_coverage,
            annotations=record.annotations,
            objects=max(record.objects, 1) if record.oversize else record.objects,
        )
        facts[record.page] = PageFacts(
            page=record.page,
            width=record.width,
            height=record.height,
            rotation=record.rotation,
            image_coverage=record.image_coverage,
            annotations=record.annotations,
            quality=result.quality,
            layer_chars=result.chars,
            layer_usable=result.layer_usable(settings.threshold),
            to_textract=result.fails(settings.threshold),
            layer_at=spool.add({"text": record.text, "words": record.words}),
        )

    try:
        try:
            for first in range(1, total + 1, runner.TEXT_PAGES_PER_CHILD):
                last = min(first + runner.TEXT_PAGES_PER_CHILD - 1, total)
                deps.pages.read_text(path, first, last, scored)
            failing = [page for page in sorted(facts) if facts[page].to_textract]
            _send_to_textract(deps, doc, path, failing, facts, spool, source.private)
        except SandboxNotStarted:
            # The task could not start the child as the sandbox user. Not the file's fault.
            raise
        except SandboxError as exc:
            at_a_limit = isinstance(exc, SandboxLimit)
            log.error(
                STOPPED_AT_A_LIMIT if at_a_limit else ENDED_UNEXPECTEDLY,
                extra=doc.fields | {"ending": exc.reason, "sandbox_limit": at_a_limit},
            )
            return finish(
                deps,
                doc,
                step=STEP,
                status="failed",
                outcome=FAILED,
                code=PAGE_UNREADABLE,
                error_type=f"{SANDBOX}.{exc.reason}",
            )

        ordered = [facts[page] for page in sorted(facts)]
        if [fact.page for fact in ordered] != list(range(1, total + 1)):
            raise RuntimeError("the text step did not get every page")
        target = source.private / "text.json"
        with spool.reader() as reader:
            fits = write_text_object(
                target,
                document_id=doc.document_id,
                attempt=doc.attempt,
                threshold=settings.threshold,
                facts=ordered,
                reader=reader,
            )
        if not fits:
            return finish(
                deps,
                doc,
                step=STEP,
                status="failed",
                outcome=FAILED,
                code=TEXT_TOO_LARGE,
                level=logging.WARNING,
            )
        return _store(doc, deps, target, ordered)
    finally:
        spool.close()


def _store(doc: Document, deps: Deps, target: Path, facts: Sequence[PageFacts]) -> str:
    """Upload the text object, then write the rows, the pointer and the step's result."""
    with deps.engine.begin() as conn:
        current = conn.execute(
            sql(
                "SELECT 1 FROM assay.documents WHERE id = :id AND status = 'processing' "
                "AND attempt = :attempt"
            ),
            {"id": doc.document_id, "attempt": doc.attempt},
        ).first()
    if current is None:
        # "Process again" started a newer attempt meanwhile: this one writes nothing.
        log.warning("superseded while reading text; nothing written", extra=doc.fields)
        return NOT_ELIGIBLE
    # Fresh credentials: the ones the PDF was downloaded with may have run out.
    storage = deps.storage()
    version = storage.objects.upload(
        storage.context.text_key(doc.document_id), target, CONTENT_TYPE
    )
    if not version:
        raise RuntimeError("the text object has no version ID")

    counts = {
        "pages": len(facts),
        "pages_native": sum(1 for f in facts if not f.to_textract),
        "pages_ocr": sum(1 for f in facts if f.to_textract and not f.unread),
        "pages_unread": sum(1 for f in facts if f.unread),
    }
    rows = [
        {
            "tenant": doc.tenant_id,
            "document": doc.document_id,
            "attempt": doc.attempt,
            "page": f.page,
            "source": f.source,
            "quality": f.quality,
            "confidence": f.confidence,
            "chars": f.ocr_chars if f.to_textract else f.layer_chars,
        }
        for f in facts
    ]
    with deps.engine.begin() as conn:
        conn.execute(PAGE_ROW, rows)
        moved = conn.execute(
            sql(
                "UPDATE assay.documents SET content_attempt = :attempt, "
                "text_version_id = :version WHERE id = :id AND status = 'processing' "
                "AND attempt = :attempt RETURNING id"
            ),
            {"id": doc.document_id, "attempt": doc.attempt, "version": version},
        ).first()
        if moved is None:
            raise RuntimeError("the document is no longer this attempt's to write")
        event(
            conn,
            doc,
            STEP,
            CONTINUE,
            counts | {"threshold": deps.text.threshold, "version_id": version},
        )
    log.info("text read", extra={**doc.fields, **counts})
    return CONTINUE
