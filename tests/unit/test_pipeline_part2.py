"""Build step 6, second half, stage 1 part 2, against a real PostgreSQL with fake storage:
attempts and "Process again", step 9 (the companion file's values, by version), a companion
file attached or replaced after ingestion, the quarantine notice, and "Mark as reviewed"."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from assay_db import batches
from assay_schemas.pipeline import IngestJob, execution_name
from assay_tss import keys
from assay_tss.models import StorageAccess
from assay_workers import scan, starter
from assay_workers.runtime import tenant_tx
from assay_workers.worker import run as worker_run
from tests.unit.pgtest import requires_db
from tests.unit.step6 import Db, FakeQueue, FakeStorage, context, minimal_pdf, sha256_hex
from tests.unit.test_pipeline_step6 import (
    Batch,
    FakeExecutions,
    Upload,
    World,
    close,
    companion_body,
    doc,
    file_row,
    open_batch,
    put,
    scanned,
    upload_and_scan,
)
from tests.unit.test_worker_step6 import claimed, deps_for, engines, events, step

pytestmark = requires_db
__all__ = ["engines"]  # the fixture, used by name below

LATER = dt.timedelta(minutes=10)


@pytest.fixture
def w(step6_db: Db) -> World:
    return World(db=step6_db, storage=FakeStorage(), queue=FakeQueue())


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def processed(
    w: World, tenant: str, files: list[tuple[str, bytes]], tmp_path: Path, made: list[Any]
) -> tuple[uuid.UUID, Batch]:
    """One document through the whole first attempt: fingerprint, task, release, companion."""
    b = open_batch(w, tenant, f"{tenant}-admin", files)
    upload_and_scan(w, b, *[name for name, _ in files])
    [doc_id] = close(w, b)
    run_attempt(w, b.tenant_id, doc_id, 1, tmp_path, made)
    return doc_id, b


def run_attempt(
    w: World, tenant_id: uuid.UUID, doc_id: uuid.UUID, attempt: int, tmp_path: Path, made: list[Any]
) -> None:
    assert step(w, "fingerprint", tenant_id, doc_id, attempt=attempt) == {"outcome": "continue"}
    job = claimed(w, tenant_id, doc_id)
    assert job.attempt == attempt
    assert worker_run.run(job, deps_for(w, job, tenant_id, tmp_path, made)) == "continue"
    step(w, "release", tenant_id, doc_id, attempt=attempt)
    step(w, "companion", tenant_id, doc_id, attempt=attempt)


def reprocess(w: World, tenant_id: uuid.UUID, doc_id: uuid.UUID, at: dt.datetime) -> int:
    with tenant_tx(w.db.app_tenant, tenant_id) as conn:
        attempt, _ = batches.reprocess(conn, tenant_id=tenant_id, document_id=doc_id, now=at)
    return attempt


def companion_rows(w: World, doc_id: uuid.UUID) -> dict[str, Any]:
    pointer = w.db.row(
        "SELECT companion_attempt, companion_file_id, application_ref, has_companion "
        "FROM assay.documents WHERE id = %s",
        doc_id,
    )
    assert pointer is not None
    return {
        "version": pointer[0],
        "file": pointer[1],
        "application_ref": pointer[2],
        "has_companion": pointer[3],
        "signers": w.db.rows(
            "SELECT attempt, name, location, source FROM assay.signers WHERE document_id = %s "
            "ORDER BY attempt, name",
            doc_id,
        ),
        "terms": w.db.rows(
            "SELECT attempt, term_months, rate::float FROM assay.companion_loan_terms "
            "WHERE document_id = %s ORDER BY attempt",
            doc_id,
        ),
        "labels": w.db.rows(
            "SELECT attempt, key, value FROM assay.document_labels WHERE document_id = %s "
            "ORDER BY attempt, key",
            doc_id,
        ),
    }


FULL = {
    "ApplicationId": "APP-TEST-004",
    "Signer1Name": "Jane Smith",
    "Signer1Location": "Atlanta, GA",
    "LoanTerm": 360,
    "LoanInterestRate": 6.125,
    "Branch": "Atlanta",
}


# --- Attempts and "Process again" ---------------------------------------------------------------


def test_process_again_starts_a_new_attempt_with_its_own_execution_and_events(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, _ = processed(w, "a", [("again.pdf", minimal_pdf(text="again"))], tmp_path, engines)
    first = events(w, doc_id)
    copies = len(w.storage.buckets.copies)

    assert reprocess(w, tenant, doc_id, now() + LATER) == 2
    status, error, *_ = doc(w, doc_id)
    assert (status, error) == ("processing", None)

    # The starter names the new execution after the attempt, and passes the attempt on.
    executions = FakeExecutions([])
    deps = starter.Deps(engine=w.db.app_tenant, executions=executions)
    job = IngestJob(tenant_id=tenant, document_id=doc_id)
    assert starter.start_one(job, deps) == "started"
    assert starter.start_one(job, deps) == "already_started"
    assert executions.names == [f"{doc_id}-2"]
    assert execution_name(doc_id, 1) == str(doc_id)

    # Every step runs again for attempt 2: the first attempt's results do not stand in.
    run_attempt(w, tenant, doc_id, 2, tmp_path, engines)
    by_attempt = w.db.rows(
        "SELECT attempt, step, status FROM assay.document_events WHERE document_id = %s "
        "AND step IN ('fingerprint', 'inspect', 'companion') AND status <> 'running' "
        "ORDER BY attempt, at",
        doc_id,
    )
    assert by_attempt == [
        (1, "fingerprint", "continue"),
        (1, "inspect", "continue"),
        (1, "companion", "none"),
        (2, "fingerprint", "continue"),
        (2, "inspect", "continue"),
        (2, "companion", "none"),
    ]
    assert len(events(w, doc_id)) > len(first)
    # The PDF is already in originals/: the same bytes are not written there again.
    assert len(w.storage.buckets.copies) == copies


def test_a_superseded_attempt_writes_nothing(w: World, tmp_path: Path, engines: list[Any]) -> None:
    tenant = w.db.tenants["a"]
    doc_id, _ = processed(w, "a", [("old.pdf", minimal_pdf(text="superseded"))], tmp_path, engines)
    assert reprocess(w, tenant, doc_id, now() + LATER) == 2
    before = events(w, doc_id)
    grants = w.db.row("SELECT count(*) FROM assay.worker_grants WHERE document_id = %s", doc_id)

    # Steps of the first attempt's execution, arriving late.
    assert step(w, "fingerprint", tenant, doc_id, attempt=1) == {"outcome": "stopped"}
    assert step(w, "claim", tenant, doc_id, attempt=1) == {"outcome": "stopped"}
    assert step(w, "companion", tenant, doc_id, attempt=1) == {"outcome": "stopped"}
    failed = step(w, "failed", tenant, doc_id, attempt=1, failed_step="worker", error_type="X")
    assert failed == {"outcome": "stopped"}

    assert events(w, doc_id) == before
    assert doc(w, doc_id)[0] == "processing"
    assert (
        w.db.row("SELECT count(*) FROM assay.worker_grants WHERE document_id = %s", doc_id)
        == grants
    )


def test_a_late_release_does_not_end_the_next_attempts_grant(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, _ = processed(w, "a", [("rel.pdf", minimal_pdf(text="release"))], tmp_path, engines)
    reprocess(w, tenant, doc_id, now() + LATER)
    assert step(w, "fingerprint", tenant, doc_id, attempt=2) == {"outcome": "continue"}
    claimed(w, tenant, doc_id)
    step(w, "release", tenant, doc_id, attempt=1)
    live = w.db.row(
        "SELECT count(*) FROM assay.worker_grants WHERE document_id = %s AND ended_at IS NULL",
        doc_id,
    )
    assert live == (1,)


@pytest.mark.parametrize("status", ["quarantined", "on_hold", "duplicate", "discarded", "uploaded"])
def test_process_again_is_refused_for_a_document_that_must_not_run(
    w: World, tmp_path: Path, engines: list[Any], status: str
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, _ = processed(w, "a", [(f"{status}.pdf", minimal_pdf(text=status))], tmp_path, engines)
    w.db.execute("UPDATE assay.documents SET status = %s WHERE id = %s", status, doc_id)
    with pytest.raises(batches.BatchError, match="not_reprocessable"):
        reprocess(w, tenant, doc_id, now() + LATER)
    assert w.db.row("SELECT attempt FROM assay.documents WHERE id = %s", doc_id) == (1,)


def test_process_again_is_refused_while_an_attempt_is_under_way(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, _ = processed(w, "a", [("busy.pdf", minimal_pdf(text="busy"))], tmp_path, engines)
    # Something happened to it a moment ago.
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, doc_id, now())
    # A live worker grant, however long ago its last event.
    step(w, "claim", tenant, doc_id)
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, doc_id, now() + dt.timedelta(hours=2))
    # Another tenant's document does not exist for this tenant.
    with (
        tenant_tx(w.db.app_tenant, w.db.tenants["b"]) as conn,
        pytest.raises(batches.BatchError, match="document_not_found"),
    ):
        batches.reprocess(conn, tenant_id=w.db.tenants["b"], document_id=doc_id, now=now() + LATER)


def test_a_failed_document_that_became_someone_elses_original_ends_a_duplicate(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """A failed original no longer counts, so the same file uploaded again is an original.
    "Process again" on the failed one then checks the hash again."""
    tenant = w.db.tenants["a"]
    body = minimal_pdf(text="failed-then-reuploaded")
    first, _ = processed(w, "a", [("first.pdf", body)], tmp_path, engines)
    w.db.execute(
        "UPDATE assay.documents SET status = 'failed', error_code = 'inspect_failed' WHERE id = %s",
        first,
    )
    second, _ = processed(w, "a", [("second.pdf", body)], tmp_path, engines)
    assert doc(w, second)[0] == "processing"

    assert reprocess(w, tenant, first, now() + LATER) == 2
    assert doc(w, first)[3] is None  # it gave up its hash, so the check runs again
    assert step(w, "fingerprint", tenant, first, attempt=2) == {"outcome": "duplicate"}
    status, _, _, _, duplicate_of, *_ = doc(w, first)
    assert (status, duplicate_of) == ("duplicate", second)


# --- Step 9: the companion file's values --------------------------------------------------------


def test_the_companion_step_stores_every_value_as_version_one(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    files = [
        ("loan.pdf", minimal_pdf(text="companion-1")),
        ("loan.json", companion_body(tenant, **FULL)),
    ]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    stored = companion_rows(w, doc_id)
    assert stored["version"] == 1
    assert stored["file"] == b.files["loan.json"].id
    assert (stored["application_ref"], stored["has_companion"]) == ("APP-TEST-004", True)
    assert stored["signers"] == [(1, "Jane Smith", "Atlanta, GA", "companion")]
    assert stored["terms"] == [(1, 360, 6.125)]
    assert stored["labels"] == [(1, "Branch", "Atlanta")]
    copy = (
        b.bucket,
        b.files["loan.json"].key,
        file_row(w, b.files["loan.json"].id)[2],
        keys.companion_key(doc_id),
    )
    assert w.storage.buckets.copies.count(copy) == 1
    # A repeat of the step (a retried state) stores nothing twice.
    assert step(w, "companion", tenant, doc_id, attempt=1) == {"outcome": "stored"}
    assert companion_rows(w, doc_id)["version"] == 1


def test_process_again_writes_a_new_version_and_keeps_the_old_rows(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    files = [
        ("keep.pdf", minimal_pdf(text="companion-keep")),
        ("keep.json", companion_body(tenant, **FULL)),
    ]
    doc_id, _ = processed(w, "a", files, tmp_path, engines)
    copies = len(w.storage.buckets.copies)
    reprocess(w, tenant, doc_id, now() + LATER)
    run_attempt(w, tenant, doc_id, 2, tmp_path, engines)
    stored = companion_rows(w, doc_id)
    assert stored["version"] == 2
    # Nothing is deleted: both versions are there, and the pointer says which is current.
    assert [row[0] for row in stored["signers"]] == [1, 2]
    assert [row[0] for row in stored["terms"]] == [1, 2]
    assert [row[0] for row in stored["labels"]] == [1, 2]
    # The same file is not copied beside the PDF a second time.
    assert len(w.storage.buckets.copies) == copies


def test_a_companions_values_are_never_logged_by_the_companion_step(
    w: World, tmp_path: Path, engines: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    tenant = w.db.tenants["a"]
    files = [
        ("quiet.pdf", minimal_pdf(text="companion-quiet")),
        ("quiet.json", companion_body(tenant, ApplicationId="SECRET-REF-9", Branch="Hidden")),
    ]
    with caplog.at_level(logging.DEBUG):
        doc_id, _ = processed(w, "a", files, tmp_path, engines)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "SECRET-REF-9" not in logged and "Hidden" not in logged
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'companion'",
        doc_id,
    )
    assert detail == ({"companion_version": 1, "signers": 0, "labels": 1, "replaced": False},)


# --- A companion file attached or replaced after ingestion ---------------------------------------


def replacement(w: World, b: Batch, doc_id: uuid.UUID, sub: str, name: str, body: bytes) -> Upload:
    """As the API does it: register against the document, key from the TSS context."""
    ctx = context(b.tenant_id, StorageAccess.UPLOAD)
    file_id = uuid.uuid4()
    with tenant_tx(w.db.app_tenant, b.tenant_id) as conn:
        target = batches.replacement_target(conn, doc_id)
        kind, base = batches.classify(name)
        key = ctx.inbox_key(target.batch_id, file_id)
        batches.insert_replacement(
            conn,
            tenant_id=b.tenant_id,
            batch_id=target.batch_id,
            document_id=doc_id,
            user_id=w.db.users[sub],
            file=batches.NewFile(
                id=file_id,
                kind=kind,
                file_name=name,
                base_name=base,
                size_bytes=len(body),
                sha256=sha256_hex(body),
                s3_key=key,
            ),
        )
    upload = Upload(id=file_id, name=name, key=key, body=body)
    b.files[name] = upload
    return upload


def audit_rows(w: World, tenant_id: uuid.UUID, category: str) -> list[tuple[Any, ...]]:
    return w.db.rows(
        "SELECT action, actor_role, actor_email, target_id, detail FROM assay.audit_log "
        "WHERE tenant_id = %s AND category = %s ORDER BY at",
        tenant_id,
        category,
    )


def test_a_replacement_takes_effect_at_its_scan_and_names_both_files(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    files = [("swap.pdf", minimal_pdf(text="swap")), ("swap.json", companion_body(tenant, **FULL))]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    old = b.files["swap.json"]
    old_version = file_row(w, old.id)[2]
    before_audit = len(audit_rows(w, tenant, "document"))
    before_events = len(events(w, doc_id))

    new = replacement(
        w,
        b,
        doc_id,
        "a-admin",
        "swap.json",
        companion_body(tenant, ApplicationId="APP-NEW", LoanTerm=120),
    )
    # Until it is scanned, readers see the first version.
    assert companion_rows(w, doc_id)["version"] == 1
    version = put(w, b, "swap.json")
    assert scanned(w, b, "swap.json", version) == "clean"

    stored = companion_rows(w, doc_id)
    assert (stored["version"], stored["file"], stored["application_ref"]) == (2, new.id, "APP-NEW")
    assert stored["terms"] == [(1, 360, 6.125), (2, 120, None)]
    # A key the new file lacks no longer has a current value; the old rows are still there.
    assert [row[0] for row in stored["signers"]] == [1]
    assert (b.bucket, new.key, version, keys.companion_key(doc_id)) in w.storage.buckets.copies
    # The document itself is untouched: no new attempt, no task, nothing queued.
    assert w.db.row("SELECT attempt, status FROM assay.documents WHERE id = %s", doc_id) == (
        1,
        "processing",
    )
    assert w.queue.sent == [] or doc_id not in [j.document_id for j in w.queue.sent[-1:]]
    assert len(events(w, doc_id)) == before_events + 1

    [row] = audit_rows(w, tenant, "document")[before_audit:]
    action, role, email, target, detail = row
    assert (action, role, target) == ("companion_replaced", "tenant-admin", str(doc_id))
    assert email is not None
    assert detail["before"] == {"file_id": str(old.id), "version_id": old_version}
    assert detail["after"]["file_id"] == str(new.id)
    assert detail["after"]["version_id"] == version
    assert detail["after"]["stored_version_id"] is not None
    assert "swap.json" not in json.dumps(detail)  # a file is named by its ID, never its name


def test_a_document_with_no_companion_can_have_one_attached(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("bare.pdf", minimal_pdf(text="bare"))], tmp_path, engines)
    assert companion_rows(w, doc_id)["version"] is None
    replacement(w, b, doc_id, "a-admin", "anything.json", companion_body(tenant, **FULL))
    assert scanned(w, b, "anything.json", put(w, b, "anything.json")) == "clean"
    stored = companion_rows(w, doc_id)
    assert (stored["version"], stored["has_companion"]) == (1, True)
    assert audit_rows(w, tenant, "document")[-1][0] == "companion_attached"


def test_an_invalid_replacement_changes_nothing(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    files = [
        ("inv.pdf", minimal_pdf(text="invalid-repl")),
        ("inv.json", companion_body(tenant, **FULL)),
    ]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    before = companion_rows(w, doc_id)
    other_tenant = companion_body(w.db.tenants["b"], LoanTerm="soon")
    new = replacement(w, b, doc_id, "a-admin", "inv.json", other_tenant)
    assert scanned(w, b, "inv.json", put(w, b, "inv.json")) == "invalid"
    assert companion_rows(w, doc_id) == before
    status, error, _, detail = file_row(w, new.id)
    assert (status, error) == ("invalid", "companion_invalid")
    assert {problem["key"] for problem in detail} == {"TenantID", "LoanTerm"}


def test_one_replacement_at_a_time_and_only_for_a_document_that_was_processed(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, b = processed(
        w, "a", [("one.pdf", minimal_pdf(text="one-at-a-time"))], tmp_path, engines
    )
    replacement(w, b, doc_id, "a-admin", "one.json", companion_body(tenant))
    with pytest.raises(batches.BatchError, match="companion_pending"):
        replacement(w, b, doc_id, "a-admin", "two.json", companion_body(tenant))
    w.db.execute("UPDATE assay.documents SET status = 'quarantined' WHERE id = %s", doc_id)
    with (
        tenant_tx(w.db.app_tenant, tenant) as conn,
        pytest.raises(batches.BatchError, match="companion_not_allowed"),
    ):
        batches.replacement_target(conn, doc_id)


def test_a_replacement_is_not_one_of_its_batchs_files(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """It may carry its PDF's name, and the batch's own rules never see it."""
    tenant = w.db.tenants["a"]
    files = [("same.pdf", minimal_pdf(text="same-name")), ("same.json", companion_body(tenant))]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    replacement(w, b, doc_id, "a-admin", "SAME.json", companion_body(tenant, Branch="West"))
    with tenant_tx(w.db.app_tenant, tenant) as conn:
        assert batches.close_problems(conn, b.id) == []
        assert batches.evaluate(conn, b.id) == []


# --- The quarantine notice ----------------------------------------------------------------------


@dataclass
class FakeNotices:
    sent: list[tuple[str, str]] = field(default_factory=list)
    failures: int = 0

    def publish(self, *, subject: str, message: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("SNS is unavailable")
        self.sent.append((subject, message))


def scan_deps(w: World, notices: FakeNotices | None) -> scan.Deps:
    base = w.scan_deps()
    return scan.Deps(
        engine=base.engine,
        storage=base.storage,
        queue=base.queue,
        name_prefix=base.name_prefix,
        notices=notices,
    )


def infected(
    w: World, b: Batch, name: str, notices: FakeNotices | None, threat: str | None
) -> tuple[str, dict[str, Any]]:
    from tests.unit.step6 import scan_event

    version = put(w, b, name)
    event = scan_event(b.bucket, b.files[name].key, version, "THREATS_FOUND", threat=threat)
    return version, event


def test_a_quarantine_writes_a_security_row_and_sends_a_notice_with_no_file_name(
    w: World, caplog: pytest.LogCaptureFixture
) -> None:
    tenant = w.db.tenants["a"]
    b = open_batch(w, "a", "a-admin", [("payroll-secret-name.pdf", b"not really malware")])
    pdf = b.files["payroll-secret-name.pdf"]
    notices = FakeNotices()
    version, event = infected(w, b, pdf.name, notices, "EICAR-Test-File (not a virus)")
    before = len(audit_rows(w, tenant, "security"))
    with caplog.at_level(logging.INFO):
        assert scan.handle(event, scan_deps(w, notices)) == "infected"

    [(action, role, _email, target, detail)] = audit_rows(w, tenant, "security")[before:]
    assert (action, role, target) == ("file_quarantined", "system", str(pdf.id))
    assert detail["threat"] == "EICAR-Test-File (not a virus)"
    assert detail["batch_id"] == str(b.id)
    assert detail["document_id"] == str(pdf.id)
    assert detail["quarantine_key"] == keys.quarantine_key(pdf.id)
    assert detail["source_version_id"] == version
    assert detail["uploaded_by"]["user_id"] == str(w.db.users["a-admin"])
    assert detail["uploaded_by"]["email"]

    [(subject, message)] = notices.sent
    assert subject == "Assay Insights security notice: malware quarantined"
    for expected in (
        "EICAR-Test-File (not a virus)",
        str(tenant),
        str(b.id),
        str(pdf.id),
        detail["uploaded_by"]["email"],
        "not a system failure",
    ):
        assert expected in message
    everything = (
        message
        + json.dumps(detail)
        + " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    )
    assert "payroll-secret-name" not in everything
    assert "not really malware" not in everything
    # A notice, not a failure: nothing at ERROR, so the scan function's alarm stays quiet.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    # The key and the version are in the log, so the copy can be confirmed from it.
    line = next(r for r in caplog.records if r.getMessage() == "file quarantined")
    assert line.__dict__["quarantine_key"] == keys.quarantine_key(pdf.id)
    assert line.__dict__["source_version_id"] == version
    sent_at = w.db.row("SELECT notice_sent_at FROM assay.batch_files WHERE id = %s", pdf.id)
    assert sent_at is not None and sent_at[0] is not None


def test_a_notice_that_could_not_be_sent_is_sent_by_the_retry_and_only_once(w: World) -> None:
    tenant = w.db.tenants["a"]
    b = open_batch(w, "a", "a-admin", [("retry.pdf", b"infected, for the test")])
    pdf = b.files["retry.pdf"]
    notices = FakeNotices(failures=1)
    _, event = infected(w, b, "retry.pdf", notices, "Test.Threat")
    before = len(audit_rows(w, tenant, "security"))
    # The publish fails: the function raises, so Lambda retries the event.
    with pytest.raises(RuntimeError):
        scan.handle(event, scan_deps(w, notices))
    # The result is recorded all the same, with its one audit row and no notice mark.
    assert doc(w, pdf.id)[0] == "quarantined"
    assert len(audit_rows(w, tenant, "security")) == before + 1
    assert w.db.row("SELECT notice_sent_at FROM assay.batch_files WHERE id = %s", pdf.id) == (None,)

    assert scan.handle(event, scan_deps(w, notices)) == "notice_sent"
    assert len(notices.sent) == 1 and "Test.Threat" in notices.sent[0][1]
    # A third delivery sends nothing and writes nothing.
    assert scan.handle(event, scan_deps(w, notices)) == "already_recorded"
    assert len(notices.sent) == 1
    assert len(audit_rows(w, tenant, "security")) == before + 1


@pytest.mark.parametrize("threat", [None, "bad\nname", "x" * 201, ""])
def test_a_threat_name_that_is_not_a_plain_name_is_never_passed_on(
    w: World, threat: str | None
) -> None:
    b = open_batch(w, "a", "a-admin", [("odd.pdf", b"infected, odd name")])
    notices = FakeNotices()
    _, event = infected(w, b, "odd.pdf", notices, threat)
    if threat is not None:
        event["detail"]["scanResultDetails"]["threats"] = [{"name": threat}]
    assert scan.handle(event, scan_deps(w, notices)) == "infected"
    assert "Threat (GuardDuty): not named in the scan result" in notices.sent[0][1]


def test_an_infected_replacement_is_quarantined_with_a_notice_and_changes_nothing(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    files = [
        ("inf.pdf", minimal_pdf(text="infected-repl")),
        ("inf.json", companion_body(tenant, **FULL)),
    ]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    before = companion_rows(w, doc_id)
    new = replacement(w, b, doc_id, "a-admin", "inf.json", companion_body(tenant))
    notices = FakeNotices()
    _, event = infected(w, b, "inf.json", notices, "Test.Threat")
    assert scan.handle(event, scan_deps(w, notices)) == "infected"
    assert companion_rows(w, doc_id) == before
    assert doc(w, doc_id)[0] == "processing"
    assert f"Document ID: {doc_id}" in notices.sent[0][1]
    assert "a companion file" in notices.sent[0][1]
    assert file_row(w, new.id)[0] == "infected"


# --- "Mark as reviewed" -------------------------------------------------------------------------


def test_mark_as_reviewed_moves_needs_review_to_ready_and_nothing_else(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant, user = w.db.tenants["a"], w.db.users["a-admin"]
    doc_id, _ = processed(w, "a", [("rev.pdf", minimal_pdf(text="review"))], tmp_path, engines)

    def mark(tenant_id: uuid.UUID = tenant) -> None:
        with tenant_tx(w.db.app_tenant, tenant_id) as conn:
            batches.mark_reviewed(conn, tenant_id=tenant_id, document_id=doc_id, user_id=user)

    with pytest.raises(batches.BatchError, match="not_needs_review"):
        mark()
    w.db.execute("UPDATE assay.documents SET status = 'needs_review' WHERE id = %s", doc_id)
    with pytest.raises(batches.BatchError, match="document_not_found"):
        mark(w.db.tenants["b"])
    mark()
    assert doc(w, doc_id)[0] == "ready"
    assert events(w, doc_id)[-1] == ("review", "reviewed")
    with pytest.raises(batches.BatchError, match="not_needs_review"):
        mark()
