"""Build step 6, second half, part 1: the worker task and its grant.

Against a real PostgreSQL 17. The Lambda steps sign in as `app_tenant`; the worker signs in
as `app_worker` and binds its session with the grant's token, exactly as on Aurora. Storage is
the fake that keeps every object version. The sandbox child cannot run on the development
machine (it needs Linux and another user), so here the inspect job runs in this process;
`tests/unit/test_worker_image.py` runs the real child in the task's image.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from assay_tss import keys
from assay_tss.models import StorageAccess
from assay_workers import progress, steps
from assay_workers.sandbox import jobs
from assay_workers.sandbox import runner as sandbox_runner
from assay_workers.sandbox.runner import InspectResult
from assay_workers.worker import db as worker_db
from assay_workers.worker import run as worker_run
from assay_workers.worker.__main__ import job_from_env
from assay_workers.worker.db import GrantRefused, Job
from tests.unit.pgtest import requires_db
from tests.unit.step6 import Db, FakeQueue, FakeStorage
from tests.unit.test_pipeline_step6 import (
    World,
    close,
    companion_body,
    doc,
    eligible_document,
    file_row,
    open_batch,
    put,
    scanned,
    upload_and_scan,
)

pytestmark = requires_db

Inspect = Callable[[Path], InspectResult]


@pytest.fixture
def w(step6_db: Db) -> Iterator[World]:
    yield World(db=step6_db, storage=FakeStorage(), queue=FakeQueue())


def in_process_inspect(path: Path) -> InspectResult:
    """The sandbox job itself, validated as the parent validates a child's output."""
    return InspectResult.model_validate(jobs.inspect(path))


def step(
    w: World, name: str, tenant_id: uuid.UUID, doc_id: uuid.UUID, **extra: Any
) -> dict[str, Any]:
    return steps.handle(
        {"step": name, "tenant_id": str(tenant_id), "document_id": str(doc_id), **extra},
        w.steps_deps(),
    )


def fingerprinted(
    w: World, tenant: str, sub: str, name: str, body: bytes | None = None
) -> tuple[uuid.UUID, Any]:
    if body is None:
        doc_id, b = eligible_document(w, tenant, sub, name)
    else:
        b = open_batch(w, tenant, sub, [(name, body)])
        upload_and_scan(w, b, name)
        [doc_id] = close(w, b)
    assert step(w, "fingerprint", b.tenant_id, doc_id) == {"outcome": "continue"}
    return doc_id, b


def claimed(w: World, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> Job:
    result = step(w, "claim", tenant_id, doc_id)
    assert result["outcome"] == "claimed", result
    return Job(document_id=doc_id, attempt=result["attempt"], token=result["token"])


@pytest.fixture
def engines() -> Iterator[list[Any]]:
    made: list[Any] = []
    yield made
    for engine in made:
        engine.dispose()


def deps_for(
    w: World,
    job: Job,
    tenant_id: uuid.UUID,
    tmp_path: Path,
    engines: list[Any],
    inspect: Inspect = in_process_inspect,
) -> worker_run.Deps:
    engine = worker_db.bound_engine(w.db.worker_connection, job)
    engines.append(engine)
    return worker_run.Deps(
        engine=engine,
        storage=lambda: w.storage.worker(tenant_id, job.document_id),
        inspect=inspect,
        work_dir=tmp_path,
    )


def events(w: World, doc_id: uuid.UUID) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in w.db.rows(
            "SELECT step, status FROM assay.document_events WHERE document_id = %s ORDER BY at",
            doc_id,
        )
    ]


# --- claim and release (the Lambda steps) -----------------------------------------------------


def test_claim_creates_a_grant_and_never_logs_its_token(
    w: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "claim.pdf")
    caplog.set_level(logging.INFO)
    result = step(w, "claim", b.tenant_id, doc_id)
    assert result["outcome"] == "claimed" and result["attempt"] == 1
    token = result["token"]
    assert token and token not in caplog.text
    row = w.db.row(
        "SELECT tenant_id, attempt, token_hash, ended_at, expires_at > now() "
        "FROM assay.worker_grants WHERE document_id = %s",
        doc_id,
    )
    assert row is not None
    assert row[:2] == (b.tenant_id, 1) and row[3] is None and row[4] is True
    assert token not in row[2] and len(row[2]) == 64


def test_claim_waits_when_the_tenants_slots_are_taken_and_other_tenants_are_unaffected(
    w: World,
) -> None:
    w.db.execute("UPDATE assay.worker_grants SET ended_at = now() WHERE ended_at IS NULL")
    one = steps.Deps(engine=w.db.app_tenant, storage=w.storage, max_workers=1)
    first, a = fingerprinted(w, "a", "a-admin", "slot-1.pdf")
    second, _ = fingerprinted(w, "a", "a-admin", "slot-2.pdf")
    other, b = fingerprinted(w, "b", "b-admin", "slot-b.pdf")

    def claim(tenant_id: uuid.UUID, document: uuid.UUID) -> str:
        event = {"step": "claim", "tenant_id": str(tenant_id), "document_id": str(document)}
        return str(steps.handle(event, one)["outcome"])

    assert claim(a.tenant_id, first) == "claimed"
    assert claim(a.tenant_id, second) == "wait"
    assert claim(b.tenant_id, other) == "claimed"
    # The first document's task ends: its slot is free again.
    assert step(w, "release", a.tenant_id, first) == {"worker_started": False}
    assert claim(a.tenant_id, second) == "claimed"


def test_claim_stops_for_a_document_that_is_no_longer_processing(w: World) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "gone.pdf")
    w.db.execute("UPDATE assay.documents SET status = 'discarded' WHERE id = %s", doc_id)
    assert step(w, "claim", b.tenant_id, doc_id) == {"outcome": "stopped"}
    assert w.db.row("SELECT count(*) FROM assay.worker_grants WHERE document_id = %s", doc_id) == (
        0,
    )


def test_release_ends_the_grant_and_says_whether_the_task_started(
    w: World,
    tmp_path: Path,
    engines: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "release.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    # The task never ran (no capacity, say): not the document's failure.
    caplog.set_level(logging.WARNING)
    assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": False}
    assert "the worker task did not start" in caplog.text
    assert doc(w, doc_id)[0] == "processing"
    # Its token is dead from that moment.
    with pytest.raises(GrantRefused), w.db.worker_connection() as connection:
        worker_db.bind(connection, job)

    job = claimed(w, b.tenant_id, doc_id)
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"
    assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": True}
    assert w.db.row(
        "SELECT count(*) FROM assay.worker_grants WHERE document_id = %s AND ended_at IS NULL",
        doc_id,
    ) == (0,)


def test_a_task_that_never_starts_is_reported_loudly_every_tenth_try(
    w: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "capacity.pdf")
    levels = []
    for _ in range(steps.REPORT_UNSTARTED_EVERY):
        claimed(w, b.tenant_id, doc_id)
        caplog.clear()
        caplog.set_level(logging.WARNING)
        assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": False}
        levels.append(
            next(r.levelname for r in caplog.records if "did not start" in r.getMessage())
        )
    assert levels == ["WARNING"] * (steps.REPORT_UNSTARTED_EVERY - 1) + ["ERROR"]
    # Never the document's failure, however often.
    assert doc(w, doc_id)[0] == "processing"


# --- The worker: inspect ------------------------------------------------------------------------


def test_the_worker_inspects_then_copies_the_pinned_version_to_originals(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "orig.pdf")
    pdf = b.files["orig.pdf"]
    version = file_row(w, pdf.id)[2]
    # A later PUT through the same link: a version that was never scanned.
    assert put(w, b, "orig.pdf", b"%PDF-1.7 something else entirely") != version
    job = claimed(w, b.tenant_id, doc_id)
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"

    status, error, s3_key, _, _, _, pages = doc(w, doc_id)
    assert (status, error, s3_key, pages) == ("processing", None, keys.original_key(doc_id), 2)
    # Read and copied at exactly the version that scanned clean.
    [copy] = [c for c in w.storage.buckets.copies if c[1] == pdf.key]
    assert copy == (b.bucket, pdf.key, version, keys.original_key(doc_id))
    assert (b.bucket, pdf.key, version) in w.storage.buckets.reads
    # It asked for a worker context for this document, never a pipeline one.
    assert w.storage.asked[-1][:2] == (b.tenant_id, StorageAccess.WORKER)
    assert events(w, doc_id)[-3:] == [
        ("worker", "running"),
        ("inspect", "running"),
        ("inspect", "continue"),
    ]
    # Nothing is left in the task's work directory.
    assert list(tmp_path.iterdir()) == []


def test_the_companion_is_copied_beside_the_pdf_at_its_own_clean_version(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    from tests.unit.step6 import minimal_pdf

    tenant = w.db.tenants["a"]
    b = open_batch(
        w,
        "a",
        "a-admin",
        [("pair.pdf", minimal_pdf(text="worker-pair")), ("PAIR.json", companion_body(tenant))],
    )
    upload_and_scan(w, b, "pair.pdf", "PAIR.json")
    [doc_id] = close(w, b)
    assert step(w, "fingerprint", tenant, doc_id) == {"outcome": "continue"}
    companion_version = file_row(w, b.files["PAIR.json"].id)[2]
    job = claimed(w, tenant, doc_id)
    assert worker_run.run(job, deps_for(w, job, tenant, tmp_path, engines)) == "continue"
    assert (
        b.bucket,
        b.files["PAIR.json"].key,
        companion_version,
        keys.companion_key(doc_id),
    ) in w.storage.buckets.copies
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'inspect' "
        "AND status = 'continue'",
        doc_id,
    )
    assert detail == ({"pages": 2, "companion": True},)


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (InspectResult(problem="pdf_encrypted", pages=None), "on_hold", "pdf_encrypted"),
        (InspectResult(problem="not_a_pdf", pages=None), "failed", "not_a_pdf"),
    ],
)
def test_a_pdf_that_does_not_open_is_held_or_fails_and_is_never_copied(
    w: World,
    tmp_path: Path,
    engines: list[Any],
    result: InspectResult,
    status: str,
    code: str,
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", f"{code}.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, inspect=lambda _path: result)
    assert worker_run.run(job, deps) == ("on_hold" if status == "on_hold" else "failed")
    assert doc(w, doc_id)[:3] == (status, code, None)
    assert w.storage.buckets.copies == []
    assert events(w, doc_id)[-1] == ("inspect", "on_hold" if status == "on_hold" else "failed")


def test_the_real_inspect_job_holds_a_password_protected_pdf(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    """Owner decision 13, with the synthetic protected file the browser checks use."""
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "make_test_files.py"
    spec = importlib.util.spec_from_file_location("make_test_files_for_worker", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = module.files(None)["03-password-protected.pdf"]

    doc_id, b = fingerprinted(w, "a", "a-admin", "locked.pdf", body)
    job = claimed(w, b.tenant_id, doc_id)
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "on_hold"
    assert doc(w, doc_id)[:3] == ("on_hold", "pdf_encrypted", None)


LIMIT_MESSAGE = "the PDF check was stopped at a sandbox limit"
UNEXPECTED_MESSAGE = "the PDF check ended unexpectedly"


@pytest.mark.parametrize(
    ("error", "ending", "message", "at_a_limit"),
    [
        (sandbox_runner.SandboxLimit("cpu_limit"), "cpu_limit", LIMIT_MESSAGE, True),
        (
            sandbox_runner.SandboxLimit("wall_clock_timeout"),
            "wall_clock_timeout",
            LIMIT_MESSAGE,
            True,
        ),
        (sandbox_runner.SandboxLimit("memory_limit"), "memory_limit", LIMIT_MESSAGE, True),
        (sandbox_runner.SandboxKilled(11), "signal_SIGSEGV", UNEXPECTED_MESSAGE, False),
        (sandbox_runner.SandboxKilled(9), "signal_SIGKILL", UNEXPECTED_MESSAGE, False),
        (sandbox_runner.SandboxBadOutput(), "bad_output", UNEXPECTED_MESSAGE, False),
    ],
)
def test_each_ending_is_named_in_the_log_and_in_the_event_for_master_admins(
    w: World,
    tmp_path: Path,
    engines: list[Any],
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    ending: str,
    message: str,
    at_a_limit: bool,
) -> None:
    """The tenant's code is the same for all of them (`pdf_unreadable`). How the child ended
    is in the log line and in the event's `error_type`, which only master admins are shown;
    a limit and an unexpected ending have different messages."""

    def stopped(_path: Path) -> InspectResult:
        raise error

    doc_id, b = fingerprinted(w, "a", "a-admin", f"stopped-{ending}.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    caplog.set_level(logging.INFO)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, inspect=stopped)
    assert worker_run.run(job, deps) == "failed"

    # The document and the tenant: one code, whatever the ending.
    assert doc(w, doc_id)[:3] == ("failed", "pdf_unreadable", None)
    assert w.storage.buckets.copies == []
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'inspect' "
        "AND status = 'failed'",
        doc_id,
    )
    assert detail == ({"error": "pdf_unreadable", "error_type": f"sandbox.{ending}"},)

    # Master admins: the stored code and the ending, through the view they read.
    with w.db.platform_reader.begin() as conn:
        shown = conn.exec_driver_sql(
            "SELECT error_code, error_type FROM platform.ingestion_events_v "
            "WHERE document_id = %(id)s AND step = 'inspect' AND status = 'failed'",
            {"id": doc_id},
        ).all()
    assert [tuple(row) for row in shown] == [("pdf_unreadable", f"sandbox.{ending}")]

    # The log: an ERROR line (the worker's alarm), with the ending and which kind it was.
    records = [r for r in caplog.records if r.getMessage() in (LIMIT_MESSAGE, UNEXPECTED_MESSAGE)]
    assert [r.getMessage() for r in records] == [message]
    record = records[0]
    assert record.levelname == "ERROR"
    assert record.ending == ending  # type: ignore[attr-defined]
    assert record.sandbox_limit is at_a_limit  # type: ignore[attr-defined]


def test_a_child_that_could_not_be_started_is_the_tasks_failure_not_the_documents(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """The task could not switch to the sandbox user: nothing was opened, so the file is not
    blamed. The step fails, and the state machine's failure handler names it."""

    def not_started(_path: Path) -> InspectResult:
        raise sandbox_runner.SandboxNotStarted

    doc_id, b = fingerprinted(w, "a", "a-admin", "not-started.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines, inspect=not_started)
    with pytest.raises(sandbox_runner.SandboxNotStarted):
        worker_run.run(job, deps)
    assert doc(w, doc_id)[:2] == ("processing", None)
    assert events(w, doc_id)[-1] == ("inspect", "error")
    result = step(
        w, "failed", b.tenant_id, doc_id, failed_step="worker", error_type="States.TaskFailed"
    )
    assert result == {"outcome": "failed", "error": "inspect_failed"}
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'failed'",
        doc_id,
    )
    assert detail == ({"error": "inspect_failed", "error_type": "SandboxNotStarted"},)


def test_an_ending_never_reaches_a_tenant_user(
    w: World, tmp_path: Path, engines: list[Any]
) -> None:
    """The tenant's document detail reads `detail.error` only (assay_api.routes.documents)."""
    import inspect as python_inspect

    from assay_api.routes import documents

    source = python_inspect.getsource(documents)
    assert "detail ->> 'error' AS error_code" in source
    assert "error_type" not in source


def test_a_repeat_of_the_task_does_nothing_again(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "again.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    assert worker_run.run(job, deps_for(w, job, b.tenant_id, tmp_path, engines)) == "continue"
    copies, reads = len(w.storage.buckets.copies), len(w.storage.buckets.reads)

    def must_not_run(_path: Path) -> InspectResult:
        raise AssertionError("the PDF was opened a second time")

    again = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, again, b.tenant_id, tmp_path, engines, inspect=must_not_run)
    assert worker_run.run(again, deps) == "continue"
    assert (len(w.storage.buckets.copies), len(w.storage.buckets.reads)) == (copies, reads)


def test_a_failure_that_is_not_the_documents_names_its_step_for_the_failure_handler(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    class StorageDown(Exception):
        pass

    def down() -> Any:
        raise StorageDown("no route to the TSS")

    doc_id, b = fingerprinted(w, "a", "a-admin", "infra.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines)
    broken = worker_run.Deps(
        engine=deps.engine, storage=down, inspect=deps.inspect, work_dir=tmp_path
    )
    with pytest.raises(StorageDown):
        worker_run.run(job, broken)
    assert doc(w, doc_id)[0] == "processing"  # the state machine decides, not the task
    assert events(w, doc_id)[-1] == ("inspect", "error")

    # The state machine's catch: release, then the failure handler, which reads the step and
    # the exception's type from the task's own marks.
    assert step(w, "release", b.tenant_id, doc_id) == {"worker_started": True}
    result = step(
        w, "failed", b.tenant_id, doc_id, failed_step="worker", error_type="States.TaskFailed"
    )
    assert result == {"outcome": "failed", "error": "inspect_failed"}
    assert doc(w, doc_id)[:2] == ("failed", "inspect_failed")
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'failed'",
        doc_id,
    )
    assert detail == ({"error": "inspect_failed", "error_type": "StorageDown"},)


def test_a_task_that_died_mid_step_is_named_by_its_open_mark(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    """Killed (out of memory, a timeout): no `error` row, only the step's `running` mark."""
    doc_id, b = fingerprinted(w, "a", "a-admin", "died.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines)
    with deps.engine.begin() as conn:
        document = worker_run.load(conn, job)
        assert document is not None
        worker_run._event(conn, document, progress.WORKER, progress.RUNNING)
        worker_run._event(conn, document, "inspect", progress.RUNNING)
    result = step(
        w, "failed", b.tenant_id, doc_id, failed_step="worker", error_type="States.Timeout"
    )
    assert result == {"outcome": "failed", "error": "inspect_failed"}
    detail = w.db.row(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'failed'",
        doc_id,
    )
    assert detail == ({"error": "inspect_failed", "error_type": "States.Timeout"},)
    # The failure handler leaves no grant behind.
    assert w.db.row(
        "SELECT count(*) FROM assay.worker_grants WHERE document_id = %s AND ended_at IS NULL",
        doc_id,
    ) == (0,)


def test_a_task_that_failed_before_any_step_is_the_workers_failure(w: World) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "early.pdf")
    result = step(
        w, "failed", b.tenant_id, doc_id, failed_step="worker", error_type="States.TaskFailed"
    )
    assert result == {"outcome": "failed", "error": "worker_failed"}


# --- The worker cannot step outside its grant -------------------------------------------------


def test_the_worker_refuses_the_reserved_document(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    job = Job(document_id=keys.RESERVED_DOCUMENT_ID, attempt=1, token="x" * 43)
    deps = worker_run.Deps(
        engine=w.db.app_tenant,  # never used: the refusal comes first
        storage=lambda: w.storage.worker(w.db.tenants["a"], job.document_id),
        inspect=in_process_inspect,
        work_dir=tmp_path,
    )
    with pytest.raises(worker_run.Refused):
        worker_run.run(job, deps)
    assert w.storage.asked == []


def test_a_wrong_token_never_gets_a_session(w: World, engines: list[Any]) -> None:
    doc_id, b = fingerprinted(w, "a", "a-admin", "wrong-token.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    forged = Job(document_id=doc_id, attempt=job.attempt, token=job.token[::-1])
    engine = worker_db.bound_engine(w.db.worker_connection, forged)
    engines.append(engine)
    with pytest.raises(GrantRefused), engine.begin():
        pass


def test_a_superseded_attempt_does_nothing(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    """The grant is for attempt 1; the document has moved on. The session sees nothing, so
    the task opens nothing and writes nothing."""
    doc_id, b = fingerprinted(w, "a", "a-admin", "superseded.pdf")
    job = claimed(w, b.tenant_id, doc_id)
    deps = deps_for(w, job, b.tenant_id, tmp_path, engines)
    with deps.engine.begin() as conn:
        assert worker_run.load(conn, job) is not None
    w.db.execute("UPDATE assay.documents SET attempt = 2 WHERE id = %s", doc_id)
    before = events(w, doc_id)
    assert worker_run.run(job, deps) == "not_eligible"
    assert events(w, doc_id) == before
    assert w.storage.asked[-1][1] is not StorageAccess.WORKER
    w.db.execute("UPDATE assay.documents SET attempt = 1 WHERE id = %s", doc_id)


def test_the_workers_session_sees_one_document_of_one_tenant(
    w: World,
    tmp_path: Path,
    engines: list[Any],
) -> None:
    mine, a = fingerprinted(w, "a", "a-admin", "mine.pdf")
    fingerprinted(w, "a", "a-admin", "sibling.pdf")
    fingerprinted(w, "b", "b-admin", "theirs.pdf")
    job = claimed(w, a.tenant_id, mine)
    deps = deps_for(w, job, a.tenant_id, tmp_path, engines)
    with deps.engine.begin() as conn:
        seen = [r[0] for r in conn.exec_driver_sql("SELECT id FROM assay.documents")]
        assert seen == [mine]
        assert {
            r[0] for r in conn.exec_driver_sql("SELECT document_id FROM assay.batch_files")
        } == {mine}
        assert conn.exec_driver_sql("SELECT pipeline.bound_tenant()").scalar_one() == a.tenant_id


# --- Small things ---------------------------------------------------------------------------------


def test_the_job_comes_from_three_values_and_hides_its_token() -> None:
    document = uuid.uuid4()
    job = job_from_env(
        {"DOCUMENT_ID": str(document), "ATTEMPT": "2", "WORKER_TOKEN": "s3cret-token"}
    )
    assert (job.document_id, job.attempt, job.token) == (document, 2, "s3cret-token")
    assert "s3cret-token" not in repr(job)
    for missing in ("DOCUMENT_ID", "ATTEMPT", "WORKER_TOKEN"):
        env = {"DOCUMENT_ID": str(document), "ATTEMPT": "2", "WORKER_TOKEN": "t"}
        del env[missing]
        with pytest.raises(KeyError):
            job_from_env(env)


def test_the_grant_outlives_the_tasks_timeout_only_by_its_margin() -> None:
    assert steps.Deps(engine=None, storage=None).grant_ttl == dt.timedelta(hours=1)  # type: ignore[arg-type]
    assert scanned is not None  # imported helpers stay in use
