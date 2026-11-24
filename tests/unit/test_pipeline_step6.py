"""Build step 6, first half: batches, scan results, the starter, fingerprint and the sweeper.

Against a real PostgreSQL 17, signed in as `app_tenant` exactly as on Aurora. Storage and the
queue are fakes (tests/unit/step6.py) that keep every object version, so the tests can show
which version the pipeline read and copied.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from assay_db import batches
from assay_schemas.pipeline import IngestJob
from assay_tss import keys, naming
from assay_tss.models import StorageAccess
from assay_workers import scan, starter, steps, sweeper
from assay_workers.runtime import tenant_tx
from tests.unit.pgtest import requires_db
from tests.unit.step6 import (
    PREFIX,
    Db,
    FakeQueue,
    FakeStorage,
    context,
    minimal_pdf,
    scan_event,
    sha256_hex,
)

pytestmark = requires_db


@dataclass
class World:
    db: Db
    storage: FakeStorage
    queue: FakeQueue

    def scan_deps(self) -> scan.Deps:
        return scan.Deps(
            engine=self.db.app_tenant, storage=self.storage, queue=self.queue, name_prefix=PREFIX
        )

    def steps_deps(self) -> steps.Deps:
        return steps.Deps(engine=self.db.app_tenant, storage=self.storage)

    def sweeper_deps(self) -> sweeper.Deps:
        return sweeper.Deps(engine=self.db.app_tenant, storage=self.storage, queue=self.queue)


@pytest.fixture
def w(step6_db: Db) -> Iterator[World]:
    yield World(db=step6_db, storage=FakeStorage(), queue=FakeQueue())


@dataclass
class Upload:
    id: uuid.UUID
    name: str
    key: str
    body: bytes


@dataclass
class Batch:
    tenant_id: uuid.UUID
    id: uuid.UUID
    bucket: str
    files: dict[str, Upload]


def open_batch(w: World, tenant: str, sub: str, files: Sequence[tuple[str, bytes]]) -> Batch:
    """As the API does it: open (discarding the user's last open batch), register, keys from
    the TSS context."""
    tenant_id, user_id = w.db.tenants[tenant], w.db.users[sub]
    ctx = context(tenant_id, StorageAccess.UPLOAD)
    with tenant_tx(w.db.app_tenant, tenant_id) as conn:
        for (old,) in conn.exec_driver_sql(
            "SELECT id FROM assay.batches WHERE status = 'open' AND opened_by = %(u)s",
            {"u": user_id},
        ).all():
            batches.discard(conn, old)
        batch_id = batches.open_batch(conn, tenant_id=tenant_id, user_id=user_id, name="t")
        new, uploads = [], {}
        for name, body in files:
            kind, base = batches.classify(name)
            file_id = uuid.uuid4()
            key = ctx.inbox_key(batch_id, file_id)
            new.append(
                batches.NewFile(
                    id=file_id,
                    kind=kind,
                    file_name=name,
                    base_name=base,
                    size_bytes=len(body),
                    sha256=sha256_hex(body),
                    s3_key=key,
                )
            )
            uploads[name] = Upload(id=file_id, name=name, key=key, body=body)
        batches.insert_files(
            conn, tenant_id=tenant_id, batch_id=batch_id, user_id=user_id, files=new
        )
    return Batch(tenant_id=tenant_id, id=batch_id, bucket=ctx.bucket, files=uploads)


def put(w: World, b: Batch, name: str, body: bytes | None = None) -> str:
    """The browser's PUT: a new version of the file's key."""
    u = b.files[name]
    return w.storage.buckets.put(b.bucket, u.key, u.body if body is None else body)


def scanned(
    w: World,
    b: Batch,
    name: str,
    version: str | None,
    result: str = "NO_THREATS_FOUND",
    reasons: Sequence[str] = (),
) -> str:
    return scan.handle(
        scan_event(b.bucket, b.files[name].key, version, result, reasons), w.scan_deps()
    )


def upload_and_scan(w: World, b: Batch, *names: str) -> None:
    for name in names:
        assert scanned(w, b, name, put(w, b, name)) in ("clean", "invalid")


def close(w: World, b: Batch) -> list[uuid.UUID]:
    with tenant_tx(w.db.app_tenant, b.tenant_id) as conn:
        return batches.close(conn, b.id)


def doc(w: World, doc_id: uuid.UUID) -> tuple[Any, ...]:
    row = w.db.row(
        "SELECT status, error_code, s3_key, sha256, duplicate_of, has_companion, page_count "
        "FROM assay.documents WHERE id = %s",
        doc_id,
    )
    assert row is not None
    return row


def file_row(w: World, file_id: uuid.UUID) -> tuple[Any, ...]:
    row = w.db.row(
        "SELECT status, error_code, version_id, error_detail FROM assay.batch_files WHERE id = %s",
        file_id,
    )
    assert row is not None
    return row


def companion_body(tenant_id: uuid.UUID, **extra: object) -> bytes:
    return json.dumps({"TenantID": str(tenant_id), "ApplicationId": "APP-1", **extra}).encode()


# --- Scan results --------------------------------------------------------------------------


def test_a_clean_pdf_waits_for_its_batch_then_is_queued_on_close(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("smith.pdf", minimal_pdf(text="a1"))])
    pdf = b.files["smith.pdf"]
    version = put(w, b, "smith.pdf")
    assert scanned(w, b, "smith.pdf", version) == "clean"
    assert file_row(w, pdf.id)[:3] == ("clean", None, version)
    assert doc(w, pdf.id)[0] == "uploaded"
    assert w.queue.sent == []
    assert close(w, b) == [pdf.id]
    assert doc(w, pdf.id)[0] == "processing"


def test_a_scan_result_for_a_closed_batch_queues_the_document(w: World) -> None:
    """Auto-close can leave a file waiting for its scan in a closed batch."""
    b = open_batch(w, "a", "a-admin", [("late.pdf", minimal_pdf(text="late"))])
    with tenant_tx(w.db.app_tenant, b.tenant_id) as conn:
        assert batches.auto_close(conn, b.id) == []
    assert doc(w, b.files["late.pdf"].id)[0] == "scanning"
    upload_and_scan(w, b, "late.pdf")
    assert [j.document_id for j in w.queue.sent] == [b.files["late.pdf"].id]
    assert w.queue.sent[0].tenant_id == b.tenant_id


def test_only_the_version_that_scanned_clean_is_ever_used(w: World) -> None:
    """Condition (b): a second PUT after the scan is never processed."""
    body = minimal_pdf(text="pinned")
    b = open_batch(w, "a", "a-admin", [("pin.pdf", body)])
    pdf = b.files["pin.pdf"]
    first = put(w, b, "pin.pdf")
    assert scanned(w, b, "pin.pdf", first) == "clean"
    # The same link, used again within its 15 minutes: a new version of the same key.
    second = put(w, b, "pin.pdf")
    assert second != first
    assert scanned(w, b, "pin.pdf", second) == "later_version_ignored"
    assert scanned(w, b, "pin.pdf", second, "THREATS_FOUND") == "later_version_ignored"
    assert file_row(w, pdf.id)[:3] == ("clean", None, first)

    [doc_id] = close(w, b)
    assert steps.handle(
        {"step": "fingerprint", "tenant_id": str(b.tenant_id), "document_id": str(doc_id)},
        w.steps_deps(),
    ) == {"outcome": "continue"}
    # Every read and copy named the first version.
    reads = [r for r in w.storage.buckets.reads if r[1] == pdf.key]
    assert reads and all(version == first for _, _, version in reads)
    [copy] = [c for c in w.storage.buckets.copies if c[1] == pdf.key]
    assert copy == (b.bucket, pdf.key, first, keys.original_key(doc_id))


def test_a_repeated_event_is_recorded_once(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("twice.pdf", minimal_pdf(text="twice"))])
    version = put(w, b, "twice.pdf")
    event = scan_event(b.bucket, b.files["twice.pdf"].key, version)
    assert scan.handle(event, w.scan_deps()) == "clean"
    assert scan.handle(event, w.scan_deps()) == "already_recorded"
    events = w.db.rows(
        "SELECT step FROM assay.document_events WHERE document_id = %s AND step = 'scan'",
        b.files["twice.pdf"].id,
    )
    assert len(events) == 1


@pytest.mark.parametrize(
    ("stored", "code"),
    [
        (b"%PDF-1.7 a different body of the same length.....", "checksum_mismatch"),
        (b"%PDF-1.7 shorter", "size_mismatch"),
    ],
)
def test_a_stored_object_that_differs_from_its_registration_fails(
    w: World, stored: bytes, code: str
) -> None:
    """Condition (a). The signed link stops this at S3; the scan step checks again."""
    registered = b"%PDF-1.7 the registered body, fifty bytes long...."
    stored = stored if code == "size_mismatch" else stored[: len(registered)].ljust(len(registered))
    b = open_batch(w, "a", "a-admin", [("odd.pdf", registered)])
    version = put(w, b, "odd.pdf", stored)
    assert scanned(w, b, "odd.pdf", version) == "mismatch"
    assert file_row(w, b.files["odd.pdf"].id)[:2] == ("mismatch", code)
    assert doc(w, b.files["odd.pdf"].id)[:2] == ("failed", code)


def test_an_infected_pdf_is_quarantined_at_its_version(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("eicar.pdf", b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR")])
    pdf = b.files["eicar.pdf"]
    version = put(w, b, "eicar.pdf")
    assert scanned(w, b, "eicar.pdf", version, "THREATS_FOUND") == "infected"
    assert w.storage.buckets.copies[-1] == (b.bucket, pdf.key, version, keys.quarantine_key(pdf.id))
    assert (b.bucket, pdf.key) in w.storage.buckets.deletes
    status, error, s3_key, *_ = doc(w, pdf.id)
    assert (status, error, s3_key) == ("quarantined", "threats_found", keys.quarantine_key(pdf.id))
    # It never becomes eligible, and closing the batch does not need it.
    assert close(w, b) == []


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ("UNSUPPORTED", "scan_unsupported"),
        ("FAILED", "scan_failed"),
        ("ACCESS_DENIED", "scan_access_denied"),
    ],
)
def test_an_unscannable_file_is_held(w: World, result: str, code: str) -> None:
    b = open_batch(w, "a", "a-admin", [("held.pdf", minimal_pdf(text=result))])
    version = put(w, b, "held.pdf")
    assert scanned(w, b, "held.pdf", version, result, ["PASSWORD_PROTECTED"]) == "unsupported"
    assert doc(w, b.files["held.pdf"].id)[:2] == ("on_hold", code)


def test_a_superseded_scan_waits_for_the_newer_version(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("new.pdf", minimal_pdf(text="new"))])
    first = put(w, b, "new.pdf")
    assert scanned(w, b, "new.pdf", first, "ACCESS_DENIED", ["OBJECT_E_TAG_CHANGED"]) == (
        "superseded"
    )
    assert file_row(w, b.files["new.pdf"].id)[0] == "registered"
    assert scanned(w, b, "new.pdf", put(w, b, "new.pdf")) == "clean"


def test_events_that_are_not_registered_uploads_are_ignored(
    w: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Condition (d): an unregistered inbox key is logged at ERROR (an alarm) and ignored."""
    tenant = w.db.tenants["a"]
    bucket = naming.bucket_name(PREFIX, tenant)
    deps = w.scan_deps()
    with caplog.at_level(logging.INFO):
        stray = keys.inbox_key(uuid.uuid4(), uuid.uuid4())
        assert scan.handle(scan_event(bucket, stray, "v1"), deps) == "unregistered"
        assert any(
            r.levelno == logging.ERROR and "no upload" in r.getMessage() for r in caplog.records
        )
        assert scan.handle(scan_event(bucket, keys.original_key(uuid.uuid4()), "v"), deps) == (
            "outside_inbox"
        )
        assert (
            scan.handle(scan_event(bucket, keys.isolation_fixture_quarantine_key(), "v"), deps)
            == "outside_inbox"
        )
        assert scan.handle({"source": "aws.s3", "detail-type": "Object Created"}, deps) == (
            "ignored_event"
        )
    assert w.storage.asked == []  # no storage context was even asked for


def test_an_event_naming_another_tenants_bucket_is_refused(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("x.pdf", minimal_pdf(text="x"))])
    version = put(w, b, "x.pdf")
    other_bucket = naming.bucket_name(PREFIX, w.db.tenants["b"])
    event = scan_event(other_bucket, b.files["x.pdf"].key, version)
    assert scan.handle(event, w.scan_deps()) == "wrong_bucket"
    assert file_row(w, b.files["x.pdf"].id)[0] == "registered"


# --- Companion files and closing -------------------------------------------------------------


def test_a_valid_companion_is_paired_and_copied_beside_its_pdf(w: World) -> None:
    tenant = w.db.tenants["a"]
    b = open_batch(
        w,
        "a",
        "a-admin",
        [("pair.pdf", minimal_pdf(text="pair")), ("PAIR.json", companion_body(tenant))],
    )
    upload_and_scan(w, b, "pair.pdf", "PAIR.json")
    [doc_id] = close(w, b)
    assert doc(w, doc_id)[5] is True  # has_companion
    companion_version = file_row(w, b.files["PAIR.json"].id)[2]
    steps.handle(
        {"step": "fingerprint", "tenant_id": str(tenant), "document_id": str(doc_id)},
        w.steps_deps(),
    )
    assert (b.bucket, b.files["PAIR.json"].key, companion_version, keys.companion_key(doc_id)) in (
        w.storage.buckets.copies
    )


def test_an_invalid_companion_blocks_the_batch_from_closing(w: World) -> None:
    other = w.db.tenants["b"]
    b = open_batch(
        w,
        "a",
        "a-admin",
        [("inv.pdf", minimal_pdf(text="inv")), ("inv.json", companion_body(other))],
    )
    upload_and_scan(w, b, "inv.pdf", "inv.json")
    status, error, _, detail = file_row(w, b.files["inv.json"].id)
    assert (status, error, detail) == (
        "invalid",
        "companion_invalid",
        [{"code": "tenant_mismatch", "key": "TenantID"}],
    )
    with pytest.raises(batches.BatchNotReady) as raised:
        close(w, b)
    assert batches.Problem(b.files["inv.json"].id, "companion_invalid") in raised.value.problems


def test_an_unmatched_companion_or_a_missing_scan_blocks_closing(w: World) -> None:
    tenant = w.db.tenants["a"]
    b = open_batch(
        w,
        "a",
        "a-admin",
        [("one.pdf", minimal_pdf(text="one")), ("two.json", companion_body(tenant))],
    )
    with pytest.raises(batches.BatchNotReady) as raised:
        close(w, b)
    codes = {(p.file_id, p.code) for p in raised.value.problems}
    assert (b.files["one.pdf"].id, "awaiting_scan") in codes
    assert (b.files["two.json"].id, "companion_unmatched") in codes


@pytest.mark.parametrize(
    ("result", "status", "error"),
    [
        ("NO_THREATS_FOUND", "processing", None),
        ("THREATS_FOUND", "quarantined", "companion_infected"),
        ("UNSUPPORTED", "on_hold", "companion_not_scanned"),
    ],
)
def test_a_pdf_starts_only_when_its_companion_also_scanned_clean(
    w: World, result: str, status: str, error: str | None
) -> None:
    """Condition (e): the pipeline still requires both scans clean. The batch is closed (by
    the 24-hour rule) while the companion's scan is still due."""
    tenant = w.db.tenants["a"]
    name = f"both-{result.lower()}"
    b = open_batch(
        w,
        "a",
        "a-admin",
        [(f"{name}.pdf", minimal_pdf(text=name)), (f"{name}.json", companion_body(tenant))],
    )
    upload_and_scan(w, b, f"{name}.pdf")
    with tenant_tx(w.db.app_tenant, b.tenant_id) as conn:
        assert batches.auto_close(conn, b.id) == []
    pdf = b.files[f"{name}.pdf"].id
    assert doc(w, pdf)[0] == "scanning"  # the PDF is clean; its companion is not scanned yet
    assert w.queue.sent == []
    scanned(w, b, f"{name}.json", put(w, b, f"{name}.json"), result)
    assert doc(w, pdf)[:2] == (status, error)
    assert [j.document_id for j in w.queue.sent] == ([pdf] if status == "processing" else [])


def test_a_companions_content_is_never_logged(w: World, caplog: pytest.LogCaptureFixture) -> None:
    tenant = w.db.tenants["a"]
    secret = "SSN-987-65-4321"
    b = open_batch(
        w,
        "a",
        "a-admin",
        [
            ("log.pdf", minimal_pdf(text="log")),
            ("log.json", companion_body(tenant, Note=secret, LoanTerm=secret)),
        ],
    )
    with caplog.at_level(logging.DEBUG):
        upload_and_scan(w, b, "log.pdf", "log.json")
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert secret not in logged
    assert secret not in json.dumps(file_row(w, b.files["log.json"].id)[3])


def test_discarding_keeps_documents_as_discarded(w: World) -> None:
    b = open_batch(
        w, "a", "a-admin", [("d1.pdf", minimal_pdf(text="d1")), ("d2.pdf", b"X5O!P%@AP EICAR d2")]
    )
    version = put(w, b, "d2.pdf")
    scanned(w, b, "d2.pdf", version, "THREATS_FOUND")
    with tenant_tx(w.db.app_tenant, b.tenant_id) as conn:
        batches.discard(conn, b.id)
    assert doc(w, b.files["d1.pdf"].id)[0] == "discarded"
    # The record of an infected upload stays as it was.
    assert doc(w, b.files["d2.pdf"].id)[0] == "quarantined"


# --- The starter -----------------------------------------------------------------------------


@dataclass
class FakeExecutions:
    started: list[IngestJob]

    def start(self, job: IngestJob) -> str:
        self.started.append(job)
        return f"arn:execution:{job.execution_name()}"


def eligible_document(w: World, tenant: str, sub: str, name: str) -> tuple[uuid.UUID, Batch]:
    b = open_batch(w, tenant, sub, [(name, minimal_pdf(text=f"{tenant}-{name}"))])
    upload_and_scan(w, b, name)
    [doc_id] = close(w, b)
    return doc_id, b


def test_the_starter_starts_each_document_once(w: World) -> None:
    doc_id, b = eligible_document(w, "a", "a-admin", "start.pdf")
    executions = FakeExecutions([])
    deps = starter.Deps(engine=w.db.app_tenant, executions=executions)
    job = IngestJob(tenant_id=b.tenant_id, document_id=doc_id)
    assert starter.start_one(job, deps) == "started"
    assert starter.start_one(job, deps) == "already_started"
    assert executions.started == [job]
    assert job.execution_name() == str(doc_id)


def test_a_suspended_tenants_documents_are_held_not_started(w: World) -> None:
    """Owner decision 19: no new execution while suspended; queued again once active."""
    tenant = w.db.tenants["b"]
    doc_id, _ = eligible_document(w, "b", "b-admin", "hold.pdf")
    w.db.execute("UPDATE assay.tenants SET status = 'suspended' WHERE id = %s", tenant)
    try:
        executions = FakeExecutions([])
        deps = starter.Deps(engine=w.db.app_tenant, executions=executions)
        assert starter.start_one(IngestJob(tenant_id=tenant, document_id=doc_id), deps) == "held"
        assert executions.started == []
        # The sweeper does not queue it again while the tenant is suspended...
        later = dt.datetime.now(dt.UTC) + batches.REQUEUE_AFTER + dt.timedelta(minutes=1)
        sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
        assert doc_id not in [j.document_id for j in w.queue.sent]
    finally:
        w.db.execute("UPDATE assay.tenants SET status = 'active' WHERE id = %s", tenant)
    # ...and does once it is active again.
    sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    assert doc_id in [j.document_id for j in w.queue.sent]


def test_the_starter_refuses_the_reserved_id_and_reads_nothing(w: World) -> None:
    class Exploding:
        def begin(self) -> Any:
            raise AssertionError("the database must not be touched")

    executions = FakeExecutions([])
    deps = starter.Deps(engine=Exploding(), executions=executions)  # type: ignore[arg-type]
    job = IngestJob(tenant_id=w.db.tenants["a"], document_id=keys.RESERVED_DOCUMENT_ID)
    assert starter.start_one(job, deps) == "refused_reserved"
    assert executions.started == []


def test_the_starter_drops_malformed_jobs(w: World) -> None:
    deps = starter.Deps(engine=w.db.app_tenant, executions=FakeExecutions([]))
    event = {
        "Records": [
            {
                "messageId": "m1",
                "body": '{"tenant_id": "x"}',
                "receiptHandle": "r",
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": "",
                "eventSource": "aws:sqs",
                "eventSourceARN": "arn",
                "awsRegion": "r",
            }
        ]
    }
    assert starter.handle(event, deps) == {"batchItemFailures": []}


# --- Fingerprint -----------------------------------------------------------------------------


def fingerprint(w: World, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> dict[str, Any]:
    return steps.handle(
        {"step": "fingerprint", "tenant_id": str(tenant_id), "document_id": str(doc_id)},
        w.steps_deps(),
    )


def test_fingerprint_copies_the_pdf_to_originals(w: World) -> None:
    doc_id, b = eligible_document(w, "a", "a-admin", "orig.pdf")
    assert fingerprint(w, b.tenant_id, doc_id) == {"outcome": "continue"}
    status, _, s3_key, sha, _, _, pages = doc(w, doc_id)
    assert (status, s3_key, pages) == ("processing", keys.original_key(doc_id), 2)
    assert sha == sha256_hex(b.files["orig.pdf"].body)
    # A repeat does nothing again.
    copies = len(w.storage.buckets.copies)
    assert fingerprint(w, b.tenant_id, doc_id) == {"outcome": "continue"}
    assert len(w.storage.buckets.copies) == copies


def test_a_repeated_upload_is_a_duplicate_within_its_tenant_only(w: World) -> None:
    """Condition (g): the same file in another tenant is never a duplicate."""
    body = minimal_pdf(text="same-everywhere")
    first_a = open_batch(w, "a", "a-admin", [("same.pdf", body)])
    upload_and_scan(w, first_a, "same.pdf")
    [a1] = close(w, first_a)
    assert fingerprint(w, first_a.tenant_id, a1) == {"outcome": "continue"}

    in_b = open_batch(w, "b", "b-admin", [("same.pdf", body)])
    upload_and_scan(w, in_b, "same.pdf")
    [b1] = close(w, in_b)
    assert fingerprint(w, in_b.tenant_id, b1) == {"outcome": "continue"}
    assert doc(w, b1)[4] is None

    again_a = open_batch(w, "a", "a-admin2", [("same-again.pdf", body)])
    upload_and_scan(w, again_a, "same-again.pdf")
    [a2] = close(w, again_a)
    assert fingerprint(w, again_a.tenant_id, a2) == {"outcome": "duplicate"}
    status, _, s3_key, _, duplicate_of, *_ = doc(w, a2)
    assert (status, duplicate_of) == ("duplicate", a1)
    assert s3_key is None  # never copied to originals


def test_a_failed_original_does_not_make_a_duplicate(w: World) -> None:
    body = minimal_pdf(text="failed-original")
    first = open_batch(w, "a", "a-admin", [("f.pdf", body)])
    upload_and_scan(w, first, "f.pdf")
    [d1] = close(w, first)
    fingerprint(w, first.tenant_id, d1)
    w.db.execute("UPDATE assay.documents SET status = 'failed' WHERE id = %s", d1)
    second = open_batch(w, "a", "a-admin", [("f.pdf", body)])
    upload_and_scan(w, second, "f.pdf")
    [d2] = close(w, second)
    assert fingerprint(w, second.tenant_id, d2) == {"outcome": "continue"}


def test_a_file_that_is_not_a_pdf_fails(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("fake.pdf", b"just some text, not a PDF")])
    upload_and_scan(w, b, "fake.pdf")
    [doc_id] = close(w, b)
    assert fingerprint(w, b.tenant_id, doc_id) == {"outcome": "failed"}
    assert doc(w, doc_id)[:2] == ("failed", "not_a_pdf")


def test_a_password_protected_pdf_is_held(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision 13. PDFium reports FPDF_ERR_PASSWORD for such a file."""
    import pypdfium2

    def locked(*args: Any, **kwargs: Any) -> Any:
        raise pypdfium2.PdfiumError(
            "Failed to load document (PDFium: Incorrect password error).",
            err_code=steps.PDFIUM_PASSWORD_ERROR,
        )

    b = open_batch(w, "a", "a-admin", [("locked.pdf", minimal_pdf(text="locked"))])
    upload_and_scan(w, b, "locked.pdf")
    [doc_id] = close(w, b)
    monkeypatch.setattr(pypdfium2, "PdfDocument", locked)
    assert fingerprint(w, b.tenant_id, doc_id) == {"outcome": "on_hold"}
    assert doc(w, doc_id)[:2] == ("on_hold", "pdf_encrypted")
    assert all(c[1] != b.files["locked.pdf"].key for c in w.storage.buckets.copies)


def test_the_steps_refuse_the_reserved_id_and_touch_nothing(w: World) -> None:
    before = w.db.row("SELECT count(*) FROM assay.document_events")
    for step in ("fingerprint", "failed"):
        result = steps.handle(
            {
                "step": step,
                "tenant_id": str(w.db.tenants["a"]),
                "document_id": str(keys.RESERVED_DOCUMENT_ID),
            },
            w.steps_deps(),
        )
        assert result == {"outcome": "refused"}
    assert w.storage.asked == [] and w.storage.buckets.reads == []
    assert w.db.row("SELECT count(*) FROM assay.document_events") == before
    assert w.db.row(
        "SELECT count(*) FROM assay.documents WHERE id = %s", keys.RESERVED_DOCUMENT_ID
    ) == (0,)


def test_the_failure_step_stores_a_code_never_the_error_text(w: World) -> None:
    doc_id, b = eligible_document(w, "a", "a-admin", "boom.pdf")
    result = steps.handle(
        {
            "step": "failed",
            "tenant_id": str(b.tenant_id),
            "document_id": str(doc_id),
            "error": {"Error": "States.Timeout", "Cause": "page text: Jane Smith, SSN ..."},
        },
        w.steps_deps(),
    )
    assert result == {"outcome": "failed", "error": "timeout"}
    assert doc(w, doc_id)[:2] == ("failed", "timeout")
    details = w.db.rows(
        "SELECT detail::text FROM assay.document_events WHERE document_id = %s", doc_id
    )
    assert all("Jane" not in (d or "") for (d,) in details)


# --- The sweeper -----------------------------------------------------------------------------


def test_an_overdue_scan_is_held_and_alarmed(w: World, caplog: pytest.LogCaptureFixture) -> None:
    """Condition (d)."""
    tenant = w.db.tenants["b"]
    b = open_batch(
        w,
        "b",
        "b-admin",
        [("slow.pdf", minimal_pdf(text="slow")), ("never.pdf", minimal_pdf(text="never"))],
    )
    put(w, b, "slow.pdf")  # uploaded, but no scan result arrives
    later = dt.datetime.now(dt.UTC) + batches.SCAN_TIMEOUT + dt.timedelta(minutes=1)
    with caplog.at_level(logging.INFO):
        result = sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    assert (result.timed_out, result.not_uploaded) == (1, 1)
    assert file_row(w, b.files["slow.pdf"].id)[:2] == ("scan_timeout", "scan_timeout")
    assert doc(w, b.files["slow.pdf"].id)[:2] == ("on_hold", "scan_timeout")
    assert file_row(w, b.files["never.pdf"].id)[0] == "not_uploaded"
    assert any(
        r.levelno == logging.ERROR and "never arrived" in r.getMessage() for r in caplog.records
    )


def test_a_batch_open_24_hours_is_closed_and_problem_files_handled(w: World) -> None:
    tenant = w.db.tenants["b"]
    b = open_batch(
        w,
        "b",
        "b-admin",
        [
            ("good.pdf", minimal_pdf(text="good")),
            ("bad.pdf", minimal_pdf(text="bad")),
            ("bad.json", b'{"LoanTerm": "long"}'),
            ("orphan.json", companion_body(tenant)),
        ],
    )
    upload_and_scan(w, b, "good.pdf", "bad.pdf", "bad.json", "orphan.json")
    later = dt.datetime.now(dt.UTC) + batches.OPEN_LIMIT + dt.timedelta(minutes=1)
    result = sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    assert result.closed >= 1
    assert w.db.row("SELECT status FROM assay.batches WHERE id = %s", b.id) == ("closed",)
    assert doc(w, b.files["good.pdf"].id)[0] == "processing"
    assert doc(w, b.files["bad.pdf"].id)[:2] == ("failed", "companion_invalid")
    assert file_row(w, b.files["orphan.json"].id)[:2] == ("removed", "companion_unmatched")
    assert b.files["good.pdf"].id in [j.document_id for j in w.queue.sent]


def test_tenants_with_work_answers_with_tenant_ids_only(w: World) -> None:
    b = open_batch(w, "a", "a-admin", [("work.pdf", minimal_pdf(text="work"))])
    later = dt.datetime.now(dt.UTC) + batches.OPEN_LIMIT + dt.timedelta(minutes=1)
    with w.db.app_tenant.begin() as conn:
        tenants = batches.tenants_with_work(conn, later)
    assert b.tenant_id in tenants
    assert set(tenants) <= set(w.db.tenants.values())
