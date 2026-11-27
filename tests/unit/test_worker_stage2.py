"""Build step 6, second half, stage 2: the worker's text step (step 5 of the spec).

Against a real PostgreSQL 17, signed in as `app_worker` and bound with the grant's token, as
on Aurora. The page jobs run in this process and their output goes through the parent's own
channel and checks (`tests/unit/stage2.py`); Textract is a stand-in that never opens a
picture. The documents are the synthetic files `scripts/make_test_files.py` writes for the
owner's own checks.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from assay_tss import keys
from assay_workers import progress, steps
from assay_workers.sandbox import runner
from assay_workers.sandbox.runner import PageImage, PageRecord
from assay_workers.worker import run as worker_run
from assay_workers.worker import text as text_step
from assay_workers.worker.ocr import OcrFailed, PageRefused
from tests.unit.pgtest import requires_db
from tests.unit.stage2 import (
    PRICE,
    SETTINGS,
    FakeOcr,
    InProcessPages,
    NoOcr,
    ocr_page,
    test_files,
)
from tests.unit.step6 import Db, FakeQueue, FakeStorage
from tests.unit.test_pipeline_step6 import World, doc
from tests.unit.test_worker_step6 import claimed, deps_for, events, fingerprinted, step

pytestmark = requires_db


@pytest.fixture
def w(step6_db: Db) -> Iterator[World]:
    yield World(db=step6_db, storage=FakeStorage(), queue=FakeQueue())


@pytest.fixture
def engines() -> Iterator[list[Any]]:
    made: list[Any] = []
    yield made
    for engine in made:
        engine.dispose()


make = test_files()
# In every typed page of the synthetic package, and in none of its file names or IDs.
TYPED = "SYNTHETIC TEST DOCUMENT"
MARKER = "ZEBRAQUARTZ"


def unique() -> str:
    """A run ID: every test's PDFs are new documents (the duplicate check is per tenant)."""
    return uuid.uuid4().hex[:12]


def typed_pdf() -> bytes:
    body: bytes = make.pdf(make.package("Package T", "Jane Testperson", "TEST-T", unique()))
    return body


def start(w: World, body: bytes, tenant: str = "a") -> tuple[uuid.UUID, Any, Any]:
    doc_id, b = fingerprinted(w, tenant, f"{tenant}-admin", f"{unique()}.pdf", body)
    return doc_id, b, claimed(w, b.tenant_id, doc_id)


def text_object(w: World, b: Any, doc_id: uuid.UUID) -> dict[str, Any]:
    stored = w.storage.buckets.current(b.bucket, keys.text_key(doc_id))
    assert stored is not None
    value: dict[str, Any] = json.loads(stored.body)
    return value


def page_rows(w: World, doc_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return w.db.rows(
        "SELECT attempt, page_no, text_source, quality_score, ocr_confidence, char_count, "
        "search_tsv FROM assay.pages WHERE document_id = %s ORDER BY attempt, page_no",
        doc_id,
    )


def usage_rows(w: World, doc_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return w.db.rows(
        "SELECT service, operation, model_id, model_version, attempt, page_no, input_tokens, "
        "output_tokens, units, unit, unit_price, cost, request_id, job, tenant_id "
        "FROM assay.ai_usage WHERE document_id = %s ORDER BY page_no",
        doc_id,
    )


def text_event(w: World, doc_id: uuid.UUID, status: str = "continue") -> dict[str, Any]:
    row = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'text' "
        "AND status = %s ORDER BY at DESC LIMIT 1",
        doc_id,
        status,
    )
    assert row is not None
    detail: dict[str, Any] = row[0]
    return detail


def pointer(w: World, doc_id: uuid.UUID) -> tuple[Any, ...]:
    row = w.db.row(
        "SELECT content_attempt, text_version_id FROM assay.documents WHERE id = %s", doc_id
    )
    assert row is not None
    return row


# --- Each kind of page --------------------------------------------------------------------------


def test_a_typed_document_is_read_from_its_text_layer_and_nothing_goes_to_textract(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    pages = InProcessPages()
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages, ocr=NoOcr())
    assert worker_run.run(job, deps) == "continue"

    stored = text_object(w, b, doc_id)
    assert stored["schema"] == 1 and stored["attempt"] == 1 and stored["threshold"] == 0.9
    assert stored["document_id"] == str(doc_id)
    assert [p["page"] for p in stored["pages"]] == [1, 2, 3]
    for page in stored["pages"]:
        assert page["source"] == "native" and page["quality_score"] == 1.0
        assert TYPED in page["text"] and page["ocr_confidence"] is None
        # Where each word is: ten-thousandths of the page, from its top left.
        assert page["words"][0][4] == "SYNTHETIC" and all(
            0 <= n <= 10000 for n in page["words"][0][:4]
        )
        # A page whose text is its text layer has no second text beside it.
        assert "text_layer" not in page and "problem" not in page
    assert [row[:3] for row in page_rows(w, doc_id)] == [
        (1, 1, "native"),
        (1, 2, "native"),
        (1, 3, "native"),
    ]
    assert all(row[3] == 1.0 and row[4] is None and row[5] > 100 for row in page_rows(w, doc_id))
    assert usage_rows(w, doc_id) == []
    assert pages.render_calls == []
    assert doc(w, doc_id)[0] == "processing"  # until stage 4 a document that passed stays so

    # The version that was written is in the event and is the one the document points at.
    [(bucket, key)] = w.storage.buckets.uploads
    assert (bucket, key) == (b.bucket, keys.text_key(doc_id))
    version = w.storage.buckets.current(b.bucket, key).version_id  # type: ignore[union-attr]
    detail = text_event(w, doc_id)
    assert detail == {
        "pages": 3,
        "pages_native": 3,
        "pages_ocr": 0,
        "pages_unread": 0,
        "threshold": 0.9,
        "version_id": version,
    }
    assert pointer(w, doc_id) == (1, version)


def test_a_scanned_document_is_read_by_textract_one_call_a_page_each_recorded(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    body = make.scanned("Package S", "Ann Testperson", "TEST-S", unique())
    doc_id, b, job = start(w, body)
    ocr = FakeOcr(answers=[ocr_page(f"PAGE {n} AS READ", 0.9 + n / 100) for n in (1, 2, 3)])
    pages = InProcessPages()
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages, ocr=ocr)
    assert worker_run.run(job, deps) == "continue"

    # Each picture is a PNG the parent passed on unopened.
    assert len(ocr.pictures) == 3
    assert all(p.startswith(b"\x89PNG\r\n\x1a\n") and len(p) < 5_000_000 for p in ocr.pictures)
    assert pages.render_calls == [[1, 2, 3]]
    stored = text_object(w, b, doc_id)
    assert [(p["source"], p["quality_score"]) for p in stored["pages"]] == [("ocr", 0.0)] * 3
    assert sorted(p["text"] for p in stored["pages"]) == [f"PAGE {n} AS READ" for n in (1, 2, 3)]
    for page in stored["pages"]:
        # left, top, width, height, the word, its confidence.
        assert len(page["words"]) == 4 and len(page["words"][0]) == 6
        # The page had no text layer to keep.
        assert "text_layer" not in page
    rows = page_rows(w, doc_id)
    assert [row[:3] for row in rows] == [(1, 1, "ocr"), (1, 2, "ocr"), (1, 3, "ocr")]
    assert all(0.9 < row[4] < 1.0 and row[5] == 11 for row in rows)

    usage = usage_rows(w, doc_id)
    assert len(usage) == 3
    for index, row in enumerate(usage, start=1):
        assert row == (
            "textract",
            "DetectDocumentText",
            None,  # not a catalog model
            "1.0",
            1,
            index,
            0,
            0,
            1,
            "page",
            PRICE,
            PRICE,
            "request-0001",
            "ingest",
            b.tenant_id,
        )
    assert text_event(w, doc_id)["pages_ocr"] == 3


def test_a_mixed_document_keeps_the_text_layer_beside_textracts_marked_as_not_the_source(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """Page 1 is typed; page 2 is typed with a picture over two fifths of it; page 3 is a
    picture with one typed line stamped on it (owner, addition 1)."""
    body = make.mixed("Package M", "Ray Testperson", "TEST-M", unique())
    doc_id, b, job = start(w, body)
    ocr = FakeOcr()
    pages = InProcessPages()
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages, ocr=ocr)
    assert worker_run.run(job, deps) == "continue"

    first, second, third = text_object(w, b, doc_id)["pages"]
    assert first["source"] == "native" and TYPED in first["text"] and "text_layer" not in first
    assert pages.render_calls == [[2, 3]] and len(ocr.pictures) == 2
    for page in (second, third):
        # The page's text is what Textract read on its picture, and only that.
        assert page["source"] == "ocr" and page["text"] == "READ BY TEXTRACT"
        assert page["text_layer"]["is_source"] is False
        assert set(page["text_layer"]) == {"is_source", "text"}  # no word positions
    assert "PAYSTUB" in second["text_layer"]["text"] and 0.5 < second["quality_score"] < 0.6
    assert second["image_coverage"] > 0.4
    assert third["text_layer"]["text"].startswith("Envelope ID:") and third["quality_score"] == 0.0
    assert [row[2] for row in page_rows(w, doc_id)] == ["native", "ocr", "ocr"]
    detail = text_event(w, doc_id)
    assert (detail["pages_native"], detail["pages_ocr"], detail["pages_unread"]) == (1, 2, 0)
    assert len(usage_rows(w, doc_id)) == 2


def test_a_filled_form_goes_to_textract_because_its_answers_are_not_in_the_text_layer(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    body = make.filled_form("Package F", "Joy Testperson", "TEST-F", unique())
    doc_id, b, job = start(w, body)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, ocr=FakeOcr())
    assert worker_run.run(job, deps) == "continue"
    [page] = text_object(w, b, doc_id)["pages"]
    assert page["source"] == "ocr" and page["annotations"] == 3 and page["quality_score"] == 0.0
    # The questions are in the text layer; the answers are in the fields.
    assert "Borrower name:" in page["text_layer"]["text"]
    assert "Joy Testperson" not in page["text_layer"]["text"]


# --- Textract's refusals and failures ---------------------------------------------------------


def test_a_page_textract_will_not_take_is_recorded_unread_and_the_document_goes_on(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    body = make.scanned("Package R", "Ann Testperson", "TEST-R", unique())
    doc_id, b, job = start(w, body)
    ocr = FakeOcr(
        answers=[ocr_page("ONE"), PageRefused("UnsupportedDocumentException"), ocr_page("THREE")]
    )
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, ocr=ocr)
    assert worker_run.run(job, deps) == "continue"

    unread = [p for p in text_object(w, b, doc_id)["pages"] if "problem" in p]
    assert len(unread) == 1
    assert unread[0]["problem"] == "UnsupportedDocumentException"
    assert (unread[0]["source"], unread[0]["text"], unread[0]["words"]) == ("ocr", "", [])
    detail = text_event(w, doc_id)
    assert (detail["pages"], detail["pages_ocr"], detail["pages_unread"]) == (3, 2, 1)
    # A page that was refused was not billed: it has no usage row.
    assert len(usage_rows(w, doc_id)) == 2
    assert doc(w, doc_id)[:2] == ("processing", None)
    assert [row[4] is None for row in page_rows(w, doc_id)].count(True) == 1


def test_textract_busy_to_the_end_fails_the_step_by_name_and_keeps_what_was_billed(
    w: World, tmp_path: Path, engines: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    body = make.scanned("Package B", "Ann Testperson", "TEST-B", unique())
    doc_id, b, job = start(w, body)
    ocr = FakeOcr(answers=[ocr_page("ONE"), OcrFailed("ThrottlingException"), ocr_page("THREE")])
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, ocr=ocr)
    with pytest.raises(OcrFailed):
        worker_run.run(job, deps)

    assert events(w, doc_id)[-1] == ("text", "error")
    assert text_event(w, doc_id, "error") == {"error_type": "ThrottlingException"}
    # The page that was read before the failure was billed, and is recorded.
    assert [row[5] for row in usage_rows(w, doc_id)] == [1]
    # Nothing else was written: no text object, no page rows, no pointer.
    assert w.storage.buckets.uploads == [] and page_rows(w, doc_id) == []
    assert pointer(w, doc_id) == (None, None)
    assert doc(w, doc_id)[0] == "processing"  # the state machine decides, not the task

    # The state machine's catch: the failure handler names the step and the error.
    assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": True}
    failed = step(
        w, "failed", b.tenant_id, doc_id, failed_step="worker", error_type="States.TaskFailed"
    )
    assert failed == {"outcome": "failed", "error": "text_failed"}
    assert doc(w, doc_id)[:2] == ("failed", "text_failed")
    row = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'failed'",
        doc_id,
    )
    assert row is not None and row[0] == {
        "error": "text_failed",
        "error_type": "ThrottlingException",
    }


# --- Limits ---------------------------------------------------------------------------------------


def test_a_document_over_the_page_limit_is_not_read_at_all(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    pages = InProcessPages()
    two = dataclasses.replace(SETTINGS, max_pages=2)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages, ocr=NoOcr(), text=two)
    assert worker_run.run(job, deps) == "failed"
    assert doc(w, doc_id)[:2] == ("failed", "too_many_pages")
    assert text_event(w, doc_id, "failed") == {"error": "too_many_pages"}
    assert pages.text_calls == [] and pages.render_calls == []
    assert w.storage.buckets.uploads == [] and page_rows(w, doc_id) == []


def test_a_document_at_the_page_limit_is_read(w: World, tmp_path: Path, engines: list[Any]) -> None:
    _doc_id, b, job = start(w, typed_pdf())
    three = dataclasses.replace(SETTINGS, max_pages=3)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, text=three)
    assert worker_run.run(job, deps) == "continue"


class Stopping(InProcessPages):
    """A sandbox child that does not end well, on the text job or on the render job."""

    def __init__(self, error: Exception, on: str) -> None:
        super().__init__()
        self._error, self._on = error, on

    def read_text(
        self, source: Path, first: int, last: int, handle: Callable[[PageRecord], None]
    ) -> None:
        if self._on == "text":
            raise self._error
        super().read_text(source, first, last, handle)

    def render(
        self, source: Path, pages: Sequence[int], handle: Callable[[PageImage], None]
    ) -> None:
        if self._on == "render":
            raise self._error
        super().render(source, pages, handle)


@pytest.mark.parametrize(
    ("error", "on", "ending", "message"),
    [
        (runner.SandboxLimit("cpu_limit"), "text", "cpu_limit", text_step.STOPPED_AT_A_LIMIT),
        (
            runner.SandboxLimit("output_limit"),
            "render",
            "output_limit",
            text_step.STOPPED_AT_A_LIMIT,
        ),
        (runner.SandboxKilled(11), "text", "signal_SIGSEGV", text_step.ENDED_UNEXPECTEDLY),
        (runner.SandboxBadOutput(), "render", "bad_output", text_step.ENDED_UNEXPECTEDLY),
    ],
)
def test_a_child_that_does_not_end_well_fails_the_document_and_is_named_for_master_admins(
    w: World,
    tmp_path: Path,
    engines: list[Any],
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    on: str,
    ending: str,
    message: str,
) -> None:
    body = make.scanned("Package E", "Ann Testperson", "TEST-E", unique())
    doc_id, b, job = start(w, body)
    deps = deps_for(
        w, job, b.tenant_id, tmp_path, engines, pages=Stopping(error, on), ocr=FakeOcr()
    )
    caplog.set_level(logging.INFO)
    assert worker_run.run(job, deps) == "failed"
    assert doc(w, doc_id)[:2] == ("failed", "page_unreadable")
    assert text_event(w, doc_id, "failed") == {
        "error": "page_unreadable",
        "error_type": f"sandbox.{ending}",
    }
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == message and record.__dict__["ending"] == ending
    assert w.storage.buckets.uploads == [] and page_rows(w, doc_id) == []


def test_a_child_that_could_not_be_started_is_the_tasks_failure_not_the_documents(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    pages = Stopping(runner.SandboxNotStarted(), "text")
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages)
    with pytest.raises(runner.SandboxNotStarted):
        worker_run.run(job, deps)
    assert doc(w, doc_id)[:2] == ("processing", None)
    assert text_event(w, doc_id, "error") == {"error_type": "SandboxNotStarted"}


def test_a_text_object_over_its_cap_fails_the_document(
    w: World, tmp_path: Path, engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    monkeypatch.setattr(text_step, "MAX_TEXT_OBJECT_BYTES", 2000)
    monkeypatch.setattr(
        text_step.write_text_object, "__kwdefaults__", {"limit": 2000}, raising=False
    )
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines)
    assert worker_run.run(job, deps) == "failed"
    assert doc(w, doc_id)[:2] == ("failed", "text_too_large")
    assert w.storage.buckets.uploads == [] and page_rows(w, doc_id) == []


def test_the_text_objects_cap_is_128_megabytes_and_a_page_record_one() -> None:
    """The figures the owner accepted (addition 3)."""
    assert text_step.MAX_TEXT_OBJECT_BYTES == 128 * 1024 * 1024
    assert runner.MAX_PAGE_RECORD_BYTES == 1024 * 1024 and runner.MAX_PAGE_TEXT_BYTES == 256 * 1024
    assert runner.MAX_IMAGE_RECORD_BYTES == 8 * 1024 * 1024 and runner.MAX_IMAGE_BYTES == 5_000_000
    assert (runner.TEXT_PAGES_PER_CHILD, runner.RENDER_PAGES_PER_CHILD) == (25, 8)
    assert text_step.OCR_CONCURRENCY == 2


def test_pages_are_asked_for_25_a_child_and_drawn_8_a_child(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    body = make.scanned("Package L", "Max Testperson", "TEST-L", unique(), pages=60)
    doc_id, b, job = start(w, body)
    pages = InProcessPages()
    ocr = FakeOcr()
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages, ocr=ocr)
    assert worker_run.run(job, deps) == "continue"
    assert pages.text_calls == [(1, 25), (26, 50), (51, 60)]
    assert [len(chunk) for chunk in pages.render_calls] == [8] * 7 + [4]
    assert [page for chunk in pages.render_calls for page in chunk] == list(range(1, 61))
    assert len(ocr.pictures) == 60 and len(usage_rows(w, doc_id)) == 60
    assert text_event(w, doc_id)["pages_ocr"] == 60
    # Nothing of the parent's is left behind: no picture, no text.
    assert list(tmp_path.iterdir()) == []


# --- Attempts ---------------------------------------------------------------------------------


def test_a_repeat_of_the_task_reads_nothing_again(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"
    pages = InProcessPages()
    again = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=pages)
    assert worker_run.run(job, again) == "continue"
    assert pages.text_calls == [] and len(w.storage.buckets.uploads) == 1
    assert len(page_rows(w, doc_id)) == 3


class Superseding(InProcessPages):
    """ "Process again" arrives while the text is being read."""

    def __init__(self, world: World, document: uuid.UUID) -> None:
        super().__init__()
        self._world, self._document = world, document

    def read_text(
        self, source: Path, first: int, last: int, handle: Callable[[PageRecord], None]
    ) -> None:
        super().read_text(source, first, last, handle)
        self._world.db.execute(
            "UPDATE assay.documents SET attempt = attempt + 1 WHERE id = %s", self._document
        )


def test_an_attempt_superseded_while_it_reads_writes_nothing(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, pages=Superseding(w, doc_id))
    assert worker_run.run(job, deps) == "not_eligible"
    assert w.storage.buckets.uploads == [] and page_rows(w, doc_id) == []
    assert pointer(w, doc_id) == (None, None)
    assert ("text", "continue") not in events(w, doc_id)


def test_process_again_keeps_the_first_attempts_text_and_moves_the_pointer(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """Owner decision, 2026-11-27: every version of the text object is kept, and each
    attempt's version ID is in its text event, so an old one is found without listing."""
    doc_id, b, job = start(w, typed_pdf())
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"
    first = pointer(w, doc_id)
    assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": True}

    # "Process again": a new attempt, from fingerprint.
    w.db.execute("UPDATE assay.documents SET attempt = 2, sha256 = NULL WHERE id = %s", doc_id)
    assert step(w, "fingerprint", b.tenant_id, doc_id, attempt=2) == {"outcome": "continue"}
    second_job = claimed(w, b.tenant_id, doc_id)
    assert second_job.attempt == 2
    deps = deps_for(w, second_job, b.tenant_id, tmp_path, engines)
    assert worker_run.run(second_job, deps) == "continue"

    second = pointer(w, doc_id)
    assert first[0] == 1 and second[0] == 2 and first[1] != second[1]
    assert [row[:2] for row in page_rows(w, doc_id)] == [
        (1, 1),
        (1, 2),
        (1, 3),
        (2, 1),
        (2, 2),
        (2, 3),
    ]
    versions = w.db.rows(
        "SELECT attempt, detail ->> 'version_id' FROM assay.document_events "
        "WHERE document_id = %s AND step = 'text' AND status = 'continue' ORDER BY attempt",
        doc_id,
    )
    assert versions == [(1, first[1]), (2, second[1])]
    # Both versions are still in the bucket: nothing is deleted.
    kept = w.storage.buckets.objects[(b.bucket, keys.text_key(doc_id))]
    assert [v.version_id for v in kept] == [first[1], second[1]]
    assert json.loads(kept[0].body)["attempt"] == 1 and json.loads(kept[1].body)["attempt"] == 2


# --- No page text outside the tenant's bucket ---------------------------------------------------


def every_stored_value(w: World) -> str:
    """Every value of every column of every table, as text, read as a superuser."""
    found: list[str] = []
    with psycopg.connect(w.db.url) as conn:
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN "
            "('assay', 'platform', 'pipeline', 'auth_events')"
        ).fetchall()
        for schema, table in tables:
            rows = conn.execute(f'SELECT t::text FROM "{schema}"."{table}" t').fetchall()  # nosec B608
            found += [row[0] for row in rows]
        views = conn.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = 'platform'"
        ).fetchall()
        for (view,) in views:
            rows = conn.execute(f'SELECT v::text FROM platform."{view}" v').fetchall()  # nosec B608
            found += [row[0] for row in rows]
    return "\n".join(found)


def test_no_page_text_reaches_the_database_or_a_log_line(
    w: World, tmp_path: Path, engines: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A marker is planted in a typed page (the text layer) and in what Textract returns for
    a scanned page. It is in the tenant's text object, and nowhere else: no column of any
    table, no master view, no audit row, no log record."""
    caplog.set_level(logging.DEBUG)
    lines = make.package("Package Z", "Jane Testperson", "TEST-Z", unique())
    lines[0].append(f"Account reference {MARKER}-LAYER")
    typed_id, typed_b, typed_job = start(w, make.pdf(lines))
    deps = deps_for(w, typed_job, typed_b.tenant_id, tmp_path, engines)
    assert worker_run.run(typed_job, deps) == "continue"

    scanned = make.scanned("Package Z2", "Ann Testperson", "TEST-Z2", unique())
    scan_id, scan_b, scan_job = start(w, scanned)
    ocr = FakeOcr(default=lambda: ocr_page(f"READ {MARKER}-OCR"))
    deps = deps_for(w, scan_job, scan_b.tenant_id, tmp_path, engines, ocr=ocr)
    assert worker_run.run(scan_job, deps) == "continue"

    # It is where it belongs.
    assert f"{MARKER}-LAYER" in text_object(w, typed_b, typed_id)["pages"][0]["text"]
    assert f"{MARKER}-OCR" in text_object(w, scan_b, scan_id)["pages"][0]["text"]
    # And nowhere else.
    assert MARKER not in every_stored_value(w)
    assert TYPED not in every_stored_value(w)
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in repr(record.__dict__) and TYPED not in repr(record.__dict__)
    assert MARKER not in caplog.text


def test_the_pages_table_holds_numbers_and_a_source_and_no_search_text_yet(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    doc_id, b, job = start(w, typed_pdf())
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"
    assert all(row[6] is None for row in page_rows(w, doc_id))  # search_tsv: stage 4


# --- The steps name themselves ------------------------------------------------------------------


def test_text_is_a_worker_step_and_a_step_a_failure_can_name() -> None:
    assert progress.WORKER_STEPS == ("inspect", "text")
    assert text_step.STEP == "text" and "text" in steps.PIPELINE_STEPS
    assert steps.failure_code("text") == "text_failed"
    assert set(worker_run.STEPS) == set(progress.WORKER_STEPS)


def test_an_error_is_kept_as_a_name_only() -> None:
    assert worker_run.error_name(OcrFailed("ProvisionedThroughputExceededException")) == (
        "ProvisionedThroughputExceededException"
    )
    assert worker_run.error_name(ValueError("a message that quotes a document")) == "ValueError"
    # A carried name that is not a plain name is not kept.
    assert OcrFailed("not a name; DROP TABLE").error_type == "UnknownError"

    class Odd(Exception):
        error_type = "some text with spaces"

    assert worker_run.error_name(Odd()) == "Odd"


def test_the_price_of_a_page_comes_from_settings_and_is_frozen_in_the_row(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    body = make.scanned("Package P", "Ann Testperson", "TEST-P", unique())
    doc_id, b, job = start(w, body)
    dearer = dataclasses.replace(SETTINGS, price_per_page=Decimal("0.0025"))
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, ocr=FakeOcr(), text=dearer)
    assert worker_run.run(job, deps) == "continue"
    assert {(row[10], row[11]) for row in usage_rows(w, doc_id)} == {
        (Decimal("0.002500"), Decimal("0.002500"))
    }
