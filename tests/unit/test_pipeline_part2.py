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


# --- A companion replacement is not the attempt's activity (owner, 2026-11-27) -------------------


def settle(w: World, doc_id: uuid.UUID) -> None:
    """The attempt ended long ago: its events are an hour old and it has no live grant."""
    w.db.execute(
        "UPDATE assay.document_events SET at = at - interval '1 hour' WHERE document_id = %s",
        doc_id,
    )
    w.db.execute(
        "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s "
        "AND ended_at IS NULL",
        doc_id,
    )


def replace_companion(w: World, b: Batch, doc_id: uuid.UUID, name: str, **values: Any) -> None:
    """A companion file attached or replaced after ingestion, through its scan."""
    replacement(w, b, doc_id, "a-admin", name, companion_body(b.tenant_id, **values))
    assert scanned(w, b, name, put(w, b, name)) == "clean"


def last_companion_event(w: World, doc_id: uuid.UUID) -> tuple[Any, ...]:
    row = w.db.row(
        "SELECT status, attempt, detail FROM assay.document_events "
        "WHERE document_id = %s AND step = 'companion' ORDER BY at DESC LIMIT 1",
        doc_id,
    )
    assert row is not None
    return row


def test_a_replacement_applied_by_the_scan_step_does_not_hold_the_document(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """What happened on 2026-11-27: a replacement took effect at 19:50:55, and "Process again"
    was refused until 19:55:55 with nothing running. The replacement's event is stamped with
    the current attempt, as every event is, and counted as that attempt's activity. It is
    now marked as written outside an attempt's run, and does not count."""
    tenant = w.db.tenants["a"]
    files = [("held.pdf", minimal_pdf(text="held")), ("held.json", companion_body(tenant, **FULL))]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    settle(w, doc_id)

    replace_companion(w, b, doc_id, "held.json", ApplicationId="APP-HELD", LoanTerm=120)
    status, attempt, detail = last_companion_event(w, doc_id)
    # The event is the replacement's, a moment old, stamped with the current attempt, marked.
    assert (status, attempt) == ("stored", 1)
    assert detail["replaced"] is True and detail[batches.OUTSIDE_ATTEMPT] is True
    assert companion_rows(w, doc_id)["version"] == 2

    # At once, with no wait: accepted.
    assert reprocess(w, tenant, doc_id, now()) == 2
    assert w.db.row("SELECT attempt, status FROM assay.documents WHERE id = %s", doc_id) == (
        2,
        "processing",
    )


def test_the_mark_is_what_frees_the_document_and_only_the_value_true(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """The same event without its mark holds the document, as before the fix; so does one
    whose mark is anything but true. Nothing but that key, with that value, is left out."""
    tenant = w.db.tenants["a"]
    files = [("mark.pdf", minimal_pdf(text="mark")), ("mark.json", companion_body(tenant, **FULL))]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    settle(w, doc_id)
    replace_companion(w, b, doc_id, "mark.json", ApplicationId="APP-MARK")

    def set_mark(value: str | None) -> None:
        change = (
            "detail - 'outside_attempt'"
            if value is None
            else (f"jsonb_set(detail, '{{outside_attempt}}', '{value}'::jsonb)")
        )
        w.db.execute(
            f"UPDATE assay.document_events SET detail = {change} "  # nosec B608
            "WHERE document_id = %s AND step = 'companion' AND at > now() - interval '5 minutes'",
            doc_id,
        )

    for not_true in (None, "false", '"true"', "1", "null"):
        set_mark(not_true)
        with pytest.raises(batches.BatchError, match="attempt_running"):
            reprocess(w, tenant, doc_id, now())
    set_mark("true")
    assert reprocess(w, tenant, doc_id, now()) == 2


def test_the_pipelines_own_companion_step_still_holds_the_document(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """The companion step ends every attempt. Its event is the attempt's activity: it carries
    no mark, also when the file it stores is a replacement that had not been applied."""
    tenant = w.db.tenants["a"]
    files = [("own.pdf", minimal_pdf(text="own")), ("own.json", companion_body(tenant, **FULL))]
    doc_id, _ = processed(w, "a", files, tmp_path, engines)
    status, attempt, detail = last_companion_event(w, doc_id)
    assert (status, attempt) == ("stored", 1)
    assert batches.OUTSIDE_ATTEMPT not in detail
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, doc_id, now())
    # A document with no companion file: the step's `none` event holds it just the same.
    bare, _ = processed(w, "a", [("own-bare.pdf", minimal_pdf(text="own-bare"))], tmp_path, engines)
    status, _, detail = last_companion_event(w, bare)
    assert status == "none" and not (detail or {}).get(batches.OUTSIDE_ATTEMPT)
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, bare, now())


def test_a_replacement_applied_while_an_attempt_runs_does_not_free_it(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """A marked event takes nothing away: an attempt that really is running is still held by
    its own events, and by its live grant however old those are."""
    tenant = w.db.tenants["a"]
    files = [("live.pdf", minimal_pdf(text="live")), ("live.json", companion_body(tenant, **FULL))]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    settle(w, doc_id)

    # Attempt 2 has just been queued: its own `queued` event is a moment old.
    assert reprocess(w, tenant, doc_id, now()) == 2
    replace_companion(w, b, doc_id, "live.json", ApplicationId="APP-LIVE-1")
    assert last_companion_event(w, doc_id)[2][batches.OUTSIDE_ATTEMPT] is True
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, doc_id, now())

    # Its task holds a grant, and every event of the attempt is an hour old but the
    # replacement's: the grant alone holds it.
    assert step(w, "fingerprint", tenant, doc_id, attempt=2) == {"outcome": "continue"}
    claimed(w, tenant, doc_id)
    w.db.execute(
        "UPDATE assay.document_events SET at = at - interval '1 hour' WHERE document_id = %s",
        doc_id,
    )
    replace_companion(w, b, doc_id, "live.json", ApplicationId="APP-LIVE-2")
    with pytest.raises(batches.BatchError, match="attempt_running"):
        reprocess(w, tenant, doc_id, now())
    assert w.db.row("SELECT attempt FROM assay.documents WHERE id = %s", doc_id) == (2,)

    # Once the grant has ended, the replacement's event does not hold it.
    step(w, "release", tenant, doc_id, attempt=2)
    assert reprocess(w, tenant, doc_id, now()) == 3


def test_a_file_attached_to_a_document_that_had_none_does_not_hold_it_either(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("att.pdf", minimal_pdf(text="att"))], tmp_path, engines)
    settle(w, doc_id)
    replace_companion(w, b, doc_id, "att.json", **FULL)
    assert last_companion_event(w, doc_id)[2][batches.OUTSIDE_ATTEMPT] is True
    assert reprocess(w, tenant, doc_id, now()) == 2


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


# --- A replacement that does not take effect leaves an audit row ---------------------------------

# Values a rejected file may hold. None of them may reach the audit row.
SECRET_VALUE = "SECRET-VALUE-771"
SECRET_KEY = "PrivateBranchLabel"


def ending(w: World, tenant_id: uuid.UUID, before: int) -> tuple[Any, ...]:
    """The one `document` audit row written since `before`."""
    [row] = audit_rows(w, tenant_id, "document")[before:]
    return row


def test_a_rejected_replacement_writes_an_audit_row_with_reason_codes_and_no_content(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """Owner, 2026-11-25: a rejected replacement left only its request in the trail."""
    tenant = w.db.tenants["a"]
    files = [
        ("rej.pdf", minimal_pdf(text="rejected")),
        ("rej.json", companion_body(tenant, **FULL)),
    ]
    doc_id, b = processed(w, "a", files, tmp_path, engines)
    old = b.files["rej.json"]
    old_version = file_row(w, old.id)[2]
    stored = companion_rows(w, doc_id)
    before = len(audit_rows(w, tenant, "document"))

    # A reserved key with the wrong type, a custom key with a value it may not have, and a
    # value that must never be repeated.
    body = companion_body(
        tenant, LoanInterestRate=SECRET_VALUE, ApplicationId=SECRET_VALUE, **{SECRET_KEY: None}
    )
    new = replacement(w, b, doc_id, "a-admin", "rej.json", body)
    version = put(w, b, "rej.json")
    assert scanned(w, b, "rej.json", version) == "invalid"

    assert companion_rows(w, doc_id) == stored
    action, role, email, target, detail = ending(w, tenant, before)
    assert (action, role, target) == ("companion_replace_rejected", "tenant-admin", str(doc_id))
    assert email is not None
    assert detail["reason"] == "companion_invalid"
    # A reserved key is named (fixed text, lower case); a custom key never is.
    assert sorted(detail["problems"], key=lambda p: p["error_code"]) == [
        {"error_code": "must_be_a_number", "key": "loaninterestrate"},
        {"error_code": "must_not_be_null", "key": None},
    ]
    assert detail["file"] == {"file_id": str(new.id), "version_id": version}
    assert detail["kept"] == {"file_id": str(old.id), "version_id": old_version}
    written = json.dumps(detail)
    for never in (SECRET_VALUE, SECRET_KEY, SECRET_KEY.lower(), "rej.json"):
        assert never not in written
    # Audit history shows the row as written: none of its keys is one the reader redacts.
    from assay_api.audit_read import redact

    assert redact(detail) == detail


def test_a_replacement_that_is_not_json_is_rejected_with_its_code(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("nj.pdf", minimal_pdf(text="not-json"))], tmp_path, engines)
    before = len(audit_rows(w, tenant, "document"))
    new = replacement(w, b, doc_id, "a-admin", "nj.json", f"{SECRET_VALUE} is not JSON".encode())
    assert scanned(w, b, "nj.json", put(w, b, "nj.json")) == "invalid"

    action, _, _, _, detail = ending(w, tenant, before)
    assert action == "companion_replace_rejected"
    assert detail["reason"] == "companion_invalid"
    assert detail["problems"] == [{"error_code": "not_json", "key": None}]
    assert detail["file"]["file_id"] == str(new.id)
    # The document had no companion file, and still has none.
    assert detail["kept"] is None
    assert SECRET_VALUE not in json.dumps(detail)
    assert companion_rows(w, doc_id)["version"] is None


def test_a_replacement_whose_stored_bytes_differ_is_rejected(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("mm.pdf", minimal_pdf(text="mismatch"))], tmp_path, engines)
    before = len(audit_rows(w, tenant, "document"))
    new = replacement(w, b, doc_id, "a-admin", "mm.json", companion_body(tenant))
    # Something else was stored under the upload's key.
    other = companion_body(tenant, Branch="East")
    version = w.storage.buckets.put(b.bucket, new.key, other, "application/json")
    assert scanned(w, b, "mm.json", version) == "mismatch"

    # It was checked against its registration and refused: rejected, not expired.
    action, _, _, _, detail = ending(w, tenant, before)
    assert action == "companion_replace_rejected"
    assert detail["reason"] in ("size_mismatch", "checksum_mismatch")
    assert detail["problems"] == []


def test_only_a_file_that_was_checked_and_refused_is_rejected() -> None:
    """Owner, 2026-11-25: `rejected` only for a file that was checked and refused. A file
    that never arrived or was never scanned `expired`."""
    from assay_workers import companion_data

    assert {
        status: companion_data.ending_action(status)
        for status in (
            "invalid",
            "infected",
            "mismatch",
            "unsupported",
            "scan_timeout",
            "not_uploaded",
        )
    } == {
        "invalid": "companion_replace_rejected",
        "infected": "companion_replace_rejected",
        "mismatch": "companion_replace_rejected",
        "unsupported": "companion_replace_expired",
        "scan_timeout": "companion_replace_expired",
        "not_uploaded": "companion_replace_expired",
    }
    # A new file status has to be given one of the two here before this passes again.
    decided = {"invalid", "infected", "mismatch", "unsupported", "scan_timeout"}
    assert set(batches.SCANNED) - {"clean"} == decided


def test_a_replacement_guardduty_could_not_scan_expires_with_its_reason(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """GuardDuty answered, but with no verdict: the file was never scanned."""
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("ns.pdf", minimal_pdf(text="no-scan"))], tmp_path, engines)
    before = len(audit_rows(w, tenant, "document"))
    new = replacement(w, b, doc_id, "a-admin", "ns.json", companion_body(tenant))
    version = put(w, b, "ns.json")
    assert scanned(w, b, "ns.json", version, "UNSUPPORTED") == "unsupported"

    action, role, _, target, detail = ending(w, tenant, before)
    assert (action, role, target) == ("companion_replace_expired", "tenant-admin", str(doc_id))
    assert detail["reason"] == "scan_unsupported"
    assert detail["file"] == {"file_id": str(new.id), "version_id": version}
    assert companion_rows(w, doc_id)["version"] is None


def test_every_companion_request_ends_in_exactly_one_row(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """Accepted or not, one row says how each attempt to attach a file ended; a repeated
    scan result adds none."""
    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("each.pdf", minimal_pdf(text="each"))], tmp_path, engines)
    before = len(audit_rows(w, tenant, "document"))

    replacement(w, b, doc_id, "a-admin", "bad.json", b"[]")
    bad = put(w, b, "bad.json")
    assert scanned(w, b, "bad.json", bad) == "invalid"
    assert scanned(w, b, "bad.json", bad) == "already_recorded"
    replacement(w, b, doc_id, "a-admin", "good.json", companion_body(tenant, **FULL))
    assert scanned(w, b, "good.json", put(w, b, "good.json")) == "clean"

    actions = [row[0] for row in audit_rows(w, tenant, "document")[before:]]
    assert actions == ["companion_replace_rejected", "companion_attached"]


def test_a_replacement_that_never_arrives_or_is_never_scanned_expires_with_the_reason_for_which(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    from assay_workers import sweeper

    tenant = w.db.tenants["a"]
    doc_id, b = processed(w, "a", [("late.pdf", minimal_pdf(text="late"))], tmp_path, engines)
    before = len(audit_rows(w, tenant, "document"))
    later = now() + batches.SCAN_TIMEOUT + LATER

    # Registered, and nothing was ever uploaded.
    never = replacement(w, b, doc_id, "a-admin", "never.json", companion_body(tenant))
    sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    action, role, _, target, detail = ending(w, tenant, before)
    assert (action, role, target) == ("companion_replace_expired", "tenant-admin", str(doc_id))
    assert detail["reason"] == "not_uploaded"
    assert detail["file"] == {"file_id": str(never.id), "version_id": None}

    # Uploaded, and its scan result never came.
    replacement(w, b, doc_id, "a-admin", "unscanned.json", companion_body(tenant))
    put(w, b, "unscanned.json")
    w.db.execute(
        "UPDATE assay.batch_files SET registered_at = registered_at - interval '1 hour' "
        "WHERE document_id = %s AND status = 'registered'",
        doc_id,
    )
    sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    [_, second] = audit_rows(w, tenant, "document")[before:]
    assert (second[0], second[4]["reason"]) == ("companion_replace_expired", "scan_timeout")
    # A second sweep finds nothing waiting and writes nothing.
    sweeper.sweep_tenant(tenant, w.sweeper_deps(), later)
    assert len(audit_rows(w, tenant, "document")[before:]) == 2
    assert companion_rows(w, doc_id)["version"] is None


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
    # Two rows: the security row for the quarantine, and how the request to replace ended.
    security = audit_rows(w, tenant, "security")[-1]
    assert (security[0], security[4]["file_id"]) == ("file_quarantined", str(new.id))
    rejected = audit_rows(w, tenant, "document")[-1]
    assert (rejected[0], rejected[4]["reason"]) == ("companion_replace_rejected", "threats_found")
    assert rejected[4]["file"]["file_id"] == str(new.id)


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
