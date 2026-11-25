"""The worker task's isolation probe and its fixture, before they run in AWS.

`scripts/run_isolation.py` runs the probe as the worker task, against the real TSS, S3 and
Aurora. Here the same probe and the same fixture run against a real PostgreSQL 17 (signed in
as `app_tenant`, `tss` and `app_worker` themselves), the real TSS service code with a fake
STS, and an S3 stand-in that enforces the session policy the TSS issued. So every check the
probe makes is known to pass on a correct system, and the ones below it are known to fail
when the thing they guard is broken.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from assay_tss import keys, naming, service
from assay_tss.client import TssRequestError
from assay_tss.models import (
    ResolveRequest,
    StorageAccess,
    StorageContext,
    WorkerResolveRequest,
)
from assay_tss.service import CallerGrant, Resolver, ServiceSettings
from assay_workers.sandbox import jobs, runner
from assay_workers.sandbox.confine import Limits
from assay_workers.worker import probe as worker_probe
from assay_workers.worker import probe_files
from tests.isolation import worker_fixture
from tests.unit.pgtest import _conninfo, requires_db
from tests.unit.step6 import ACCOUNT, PREFIX, REGION, Db

pytestmark = requires_db

TENANT_ROLE = f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-tenant"
WORKER = CallerGrant(service="worker", access=frozenset({StorageAccess.WORKER}))
RUNNER = CallerGrant(
    service="isolation",
    access=frozenset({StorageAccess.PIPELINE, StorageAccess.READ, StorageAccess.UPLOAD}),
)
EXPIRATION = dt.datetime(2026, 11, 25, 12, 15, tzinfo=dt.UTC)


class FakeSts:
    """Returns credentials whose access key names the session policy it was given."""

    def __init__(self) -> None:
        self.policies: dict[str, dict[str, Any]] = {}

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        key = f"ASIA{len(self.policies):016d}"
        self.policies[key] = json.loads(kwargs["Policy"])
        return {
            "Credentials": {
                "AccessKeyId": key,
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": EXPIRATION,
            }
        }


class FakeTss:
    """The real resolver behind the client's two methods, as one caller role."""

    def __init__(self, resolver: Resolver, caller: CallerGrant) -> None:
        self._resolver = resolver
        self._caller = caller

    def resolve(self, tenant_id: uuid.UUID, access: StorageAccess, identity: str) -> StorageContext:
        request = ResolveRequest(tenant_id=tenant_id, access=access, source_identity=identity)
        try:
            return self._resolver.resolve(request, caller=self._caller)
        except service.TssError as exc:
            raise TssRequestError(exc.status, exc.code) from None

    def resolve_worker(self, document_id: uuid.UUID, attempt: int, token: str) -> StorageContext:
        try:
            request = WorkerResolveRequest.model_validate(
                {"document_id": str(document_id), "attempt": attempt, "token": token}
            )
            return self._resolver.resolve_worker(request, caller=self._caller)
        except ValueError:
            raise TssRequestError(400, "invalid_request") from None
        except service.TssError as exc:
            raise TssRequestError(exc.status, exc.code) from None


class PolicyS3:
    """S3 for one set of credentials: an action on an object is allowed only if the session
    policy issued with those credentials allows it on that exact object."""

    def __init__(self, policy: dict[str, Any], objects: set[tuple[str, str]]) -> None:
        self._policy = policy
        self._objects = objects

    def _check(self, action: str, bucket: str, key: str) -> None:
        resource = f"arn:aws:s3:::{bucket}/{key}"
        for statement in self._policy["Statement"]:
            resources = statement["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            if action in statement["Action"] and resource in resources:
                return
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, action)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._check("s3:GetObject", Bucket, Key)
        if (Bucket, Key) not in self._objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")

        class Body:
            def read(self, _n: int) -> bytes:
                return b"x"

        return {"Body": Body()}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:
        self._check("s3:PutObject", Bucket, Key)
        self._objects.add((Bucket, Key))
        return {}


class GoodSandbox:
    """A sandbox whose child is confined as it should be."""

    report: dict[str, Any] = {  # noqa: RUF012
        "uid": runner.SANDBOX_UID,
        "gid": runner.SANDBOX_GID,
        "groups": [],
        "environment": ["LC_CTYPE"],
        "forbidden_modules": [],
        "socket": "PermissionError",
        "parent_environment": "PermissionError",
        "parent_memory_file": "PermissionError",
        "parent_memory_read": "PermissionError",
        "exec": "PermissionError",
        "write_work": "PermissionError",
        "write_tmp": "OSError",
        "no_new_privs": 1,
        "seccomp": 2,
    }
    # How each misbehaving child ends in a sandbox that works: by name.
    stopped: dict[str, runner.SandboxError] = {  # noqa: RUF012
        "spin": runner.SandboxLimit("cpu_limit"),
        "stubborn": runner.SandboxLimit("cpu_limit"),
        "sleep": runner.SandboxLimit("wall_clock_timeout"),
        "memory": runner.SandboxLimit("memory_limit"),
        "flood": runner.SandboxLimit("output_limit"),
        "crash": runner.SandboxKilled(6),
    }

    def selftest(self, name: str, *, limits: Limits | None = None) -> runner.SelfTestResult:
        if name in self.stopped:
            raise self.stopped[name]
        return runner.SelfTestResult.model_validate(self.report)

    def inspect(self, path: Path) -> runner.InspectResult:
        """The real job, in this process (the child cannot run on the development machine)."""
        return runner.InspectResult.model_validate(jobs.inspect(path))


class World:
    def __init__(self, db: Db, work_dir: Path) -> None:
        self.db = db
        self.work_dir = work_dir
        self.tenant_a, self.tenant_b = db.tenants["a"], db.tenants["b"]
        self.sts = FakeSts()
        self.objects: set[tuple[str, str]] = set()
        tss_engine = create_engine(
            "postgresql+psycopg://",
            creator=lambda: psycopg.connect(_conninfo(db.name, "tss")),
            poolclass=NullPool,
        )
        self.engines = [tss_engine]
        resolver = Resolver(
            ServiceSettings(
                region=REGION, name_prefix=PREFIX, tenant_role_arn=TENANT_ROLE, callers={}
            ),
            service.DbStorageReader(tss_engine),
            self.sts,  # type: ignore[arg-type]
        )
        self.worker_tss = FakeTss(resolver, WORKER)
        self.runner_tss = FakeTss(resolver, RUNNER)
        for tenant in (self.tenant_a, self.tenant_b):
            db.execute(
                "INSERT INTO assay.tenant_storage (tenant_id, mode, bucket, kms_key_arn, "
                "role_arn, vector_bucket, vector_index, region, status) VALUES "
                "(%s, 'dedicated', %s, %s, %s, %s, %s, %s, 'active') ON CONFLICT DO NOTHING",
                tenant,
                naming.bucket_name(PREFIX, tenant),
                f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant}",
                TENANT_ROLE,
                naming.vector_bucket_name(PREFIX, tenant),
                naming.VECTOR_INDEX,
                REGION,
            )

    def s3(self, ctx: StorageContext) -> PolicyS3:
        return PolicyS3(self.sts.policies[ctx.credentials.access_key_id], self.objects)

    def write(self, context: StorageContext, document: uuid.UUID) -> None:
        self.objects.add((context.bucket, context.original_key(document)))

    def prepare(self) -> dict[str, Any]:
        cases = worker_fixture.prepare(
            self.db.app_tenant,
            self.runner_tss,  # type: ignore[arg-type]
            "isolation",
            self.tenant_a,
            self.tenant_b,
            write=self.write,
        )
        # The reserved-ID fixtures the isolation tests keep in tenant A's bucket.
        bucket = naming.bucket_name(PREFIX, self.tenant_a)
        self.objects |= {
            (bucket, keys.isolation_fixture_quarantine_key()),
            (bucket, keys.isolation_fixture_text_key()),
        }
        return cases

    def probe(self, cases: dict[str, Any], **changes: Any) -> worker_probe.Probe:
        ended: list[bool] = []

        def sleep(_seconds: float) -> None:
            # As scripts/run_isolation.py does when it reads the probe's "ready" line.
            if not ended:
                document = uuid.UUID(cases["to_end"]["document_id"])
                worker_fixture.end_grant(self.db.app_tenant, self.tenant_a, document)
                ended.append(True)

        arguments: dict[str, Any] = {
            "tss": self.worker_tss,
            "connect": self.db.worker_connection,
            "name_prefix": PREFIX,
            "sandbox": GoodSandbox(),
            "sleep": sleep,
            "s3": self.s3,
            "work_dir": self.work_dir,
        }
        return worker_probe.Probe(cases, **(arguments | changes))

    def dispose(self) -> None:
        for engine in self.engines:
            engine.dispose()


@pytest.fixture
def world(step6_db: Db, tmp_path: Path) -> Iterator[World]:
    made = World(step6_db, tmp_path)
    try:
        yield made
    finally:
        worker_fixture.cleanup(step6_db.app_tenant, made.tenant_a, made.tenant_b)
        made.dispose()


def failed(checks: list[worker_probe.Check]) -> list[str]:
    """Every failed check except the parent's capabilities, which exist only in the task."""
    return [
        f"{c.name}: expected {c.expected}, was {c.outcome}"
        for c in checks
        if not c.ok and not c.name.startswith("parent:")
    ]


# --- A correct system passes every check ---------------------------------------------------------


def test_every_check_passes_on_a_correct_system(world: World) -> None:
    checks = world.probe(world.prepare()).run()
    assert failed(checks) == []
    names = [c.name for c in checks]
    # Owner condition (b): each of these is tried, and held.
    for expected in (
        "/resolve refuses the worker (pipeline)",
        "/resolve refuses the worker (read)",
        "/resolve refuses the worker (upload)",
        "/resolve-worker refuses an ended grant",
        "/resolve-worker refuses an expired grant",
        "/resolve-worker refuses the wrong attempt",
        "/resolve-worker serves the live grant",
        "storage: reads its own document's original",
        "storage: another document of the same tenant",
        "storage: the other tenant's document",
        "database: an unbound session sees no documents",
        "database: a bound session sees its one document",
        "database: a bound session cannot read users",
        "database: a bound session cannot read chat_conversations",
        "database: a bound session cannot read audit_log",
        "database: a grant that ends mid-session",
        "sandbox: the child cannot open a socket",
        "sandbox: the child cannot read the parent's memory",
        "sandbox: a child that spins is stopped by the CPU limit",
        "sandbox: a child that ignores the CPU warning is still stopped by the CPU limit",
        "sandbox: a child that crashes is reported as an unexpected signal, not a limit",
        "sandbox: a child that never finishes is stopped at the wall-clock timeout",
        "sandbox: a child that tries to exhaust memory is stopped",
        "sandbox: a child that floods its output is stopped",
        "inspection: opens a plain PDF and counts its pages",
        "inspection: holds a password-protected PDF (40-bit)",
        "inspection: holds a password-protected PDF (128-bit)",
        "inspection: refuses a file that is not a PDF",
    ):
        assert expected in names, expected
    assert len(checks) > 50


def test_the_fixture_documents_are_never_queued_and_are_cleared_afterwards(world: World) -> None:
    from assay_db import batches

    cases = world.prepare()
    db = world.db
    documents = [
        uuid.UUID(cases["own"]["document_id"]),
        uuid.UUID(cases["sibling"]["document_id"]),
        uuid.UUID(cases["other_tenant"]["document_id"]),
    ]
    assert db.rows("SELECT DISTINCT status FROM assay.documents WHERE id = ANY(%s)", documents) == [
        ("processing",)
    ]
    # Each has a `start` event, so the sweeper finds nothing to queue for either tenant.
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    for tenant in (world.tenant_a, world.tenant_b):
        with db.app_tenant.begin() as conn:
            conn.exec_driver_sql("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
            assert not set(batches.requeue_candidates(conn, later)) & set(documents)
    # A second run reuses the same documents and objects.
    again = world.prepare()
    assert again["own"]["document_id"] == cases["own"]["document_id"]
    assert again["own"]["token"] != cases["own"]["token"]
    assert db.row("SELECT count(*) FROM assay.documents WHERE id = ANY(%s)", documents) == (3,)

    worker_fixture.cleanup(db.app_tenant, world.tenant_a, world.tenant_b)
    assert db.rows("SELECT DISTINCT status FROM assay.documents WHERE id = ANY(%s)", documents) == [
        ("discarded",)
    ]
    assert db.row(
        "SELECT count(*) FROM assay.worker_grants WHERE document_id = ANY(%s) AND ended_at IS NULL",
        documents,
    ) == (0,)


def test_the_fixture_ids_are_fixed_per_tenant_and_never_the_reserved_one() -> None:
    tenant = uuid.uuid4()
    assert worker_fixture.document_id(tenant, 1) == worker_fixture.document_id(tenant, 1)
    assert worker_fixture.document_id(tenant, 1) != worker_fixture.document_id(tenant, 2)
    assert worker_fixture.document_id(tenant, 1) != worker_fixture.document_id(uuid.uuid4(), 1)
    assert not keys.is_reserved(worker_fixture.document_id(tenant, 1))
    assert keys.parse_inbox_key(
        f"inbox/{worker_fixture.batch_id(tenant)}/{worker_fixture.document_id(tenant, 1)}"
    )


# --- A broken system fails the check that guards it --------------------------------------------


def test_a_session_policy_that_reaches_the_whole_bucket_fails_the_storage_checks(
    world: World,
) -> None:
    cases = world.prepare()

    def wide(ctx: StorageContext) -> PolicyS3:
        policy = world.sts.policies[ctx.credentials.access_key_id]
        resources = [f"arn:aws:s3:::{bucket}/{key}" for bucket, key in world.objects]
        return PolicyS3(
            {
                "Statement": [
                    *policy["Statement"],
                    {"Action": ["s3:GetObject"], "Resource": resources},
                ]
            },
            world.objects,
        )

    problems = failed(world.probe(cases, s3=wide).run())
    assert any("another document of the same tenant" in p for p in problems)
    assert any("the other tenant's document" in p for p in problems)
    assert any("quarantine fixture" in p for p in problems)


def test_a_tss_that_serves_the_worker_on_resolve_fails_the_route_checks(world: World) -> None:
    cases = world.prepare()

    class Lax(FakeTss):
        def resolve(self, tenant_id: uuid.UUID, access: StorageAccess, identity: str) -> Any:
            return object()

    lax = Lax(world.worker_tss._resolver, WORKER)
    problems = failed(world.probe(cases, tss=lax).run())
    assert [p for p in problems if p.startswith("/resolve refuses the worker")]


def test_a_grant_that_never_ends_fails_the_mid_session_check(world: World) -> None:
    cases = world.prepare()
    worker_probe.END_WAIT_SECONDS, saved = 0.2, worker_probe.END_WAIT_SECONDS  # type: ignore[assignment]
    try:
        problems = failed(world.probe(cases, sleep=lambda _s: None).run())
    finally:
        worker_probe.END_WAIT_SECONDS = saved
    assert any("a grant that ends mid-session" in p for p in problems)


@pytest.mark.parametrize(
    ("field", "value", "check"),
    [
        ("socket", "allowed", "cannot open a socket"),
        ("parent_environment", "allowed", "cannot read the parent's environment"),
        ("parent_memory_read", "allowed", "cannot read the parent's memory"),
        ("exec", "allowed", "cannot start a program"),
        ("uid", 0, "runs as the sandbox user"),
        ("environment", ["AWS_SECRET_ACCESS_KEY", "LC_CTYPE"], "was given an empty environment"),
        ("forbidden_modules", ["boto3"], "has no AWS or database library loaded"),
        ("seccomp", 0, "runs under a seccomp filter"),
        ("write_work", "allowed", "cannot write to /work"),
    ],
)
def test_a_sandbox_that_is_not_confined_fails_its_check(
    world: World, field: str, value: Any, check: str
) -> None:
    class Leaky(GoodSandbox):
        report = GoodSandbox.report | {field: value}

    problems = failed(world.probe(world.prepare(), sandbox=Leaky()).run())
    assert any(check in p for p in problems), problems


def without(*names: str) -> dict[str, runner.SandboxError]:
    return {k: v for k, v in GoodSandbox.stopped.items() if k not in names}


def test_a_child_that_is_not_stopped_fails_its_check(world: World) -> None:
    class Unlimited(GoodSandbox):
        stopped = without("memory")

    problems = failed(world.probe(world.prepare(), sandbox=Unlimited()).run())
    assert any("tries to exhaust memory is stopped" in p for p in problems)


def test_each_time_limit_is_tried_where_only_it_can_stop_the_child(world: World) -> None:
    seen: dict[str, Limits | None] = {}

    class Watching(GoodSandbox):
        def selftest(self, name: str, *, limits: Limits | None = None) -> runner.SelfTestResult:
            seen[name] = limits
            return super().selftest(name, limits=limits)

    assert failed(world.probe(world.prepare(), sandbox=Watching()).run()) == []
    for spinning in ("spin", "stubborn"):
        limits = seen[spinning]
        assert limits is not None
        # The CPU limit long before the wall clock.
        assert limits.cpu_seconds * 5 <= limits.wall_seconds
    sleep = seen["sleep"]
    assert sleep is not None
    # It uses no CPU time, so only the wall clock can end it.
    assert sleep.wall_seconds * 5 <= sleep.cpu_seconds


@pytest.mark.parametrize(
    ("probe", "wrong", "check", "was"),
    [
        # The first run in AWS (2026-11-25): the kernel's kill at the CPU limit, reported
        # only as the signal. It must be named as the limit it is.
        (
            "spin",
            runner.SandboxKilled(9),
            "that spins is stopped by the CPU limit",
            "unexpected:signal_SIGKILL",
        ),
        # Without the CPU limit a spinning child lasts until the wall clock.
        (
            "spin",
            runner.SandboxLimit("wall_clock_timeout"),
            "that spins is stopped by the CPU limit",
            "limit:wall_clock_timeout",
        ),
        (
            "stubborn",
            runner.SandboxKilled(9),
            "that ignores the CPU warning is still stopped by the CPU limit",
            "unexpected:signal_SIGKILL",
        ),
        # A crash given a limit's name would hide a parser that died some other way.
        (
            "crash",
            runner.SandboxLimit("cpu_limit"),
            "that crashes is reported as an unexpected signal, not a limit",
            "limit:cpu_limit",
        ),
        (
            "sleep",
            runner.SandboxKilled(9),
            "that never finishes is stopped at the wall-clock timeout",
            "unexpected:signal_SIGKILL",
        ),
    ],
)
def test_an_ending_reported_under_the_wrong_name_fails_its_check(
    world: World, probe: str, wrong: runner.SandboxError, check: str, was: str
) -> None:
    class Misnamed(GoodSandbox):
        stopped = GoodSandbox.stopped | {probe: wrong}

    problems = failed(world.probe(world.prepare(), sandbox=Misnamed()).run())
    assert len(problems) == 1 and check in problems[0] and problems[0].endswith(f"was {was}")


def test_a_wall_clock_that_does_not_stop_a_waiting_child_fails_its_check(world: World) -> None:
    class NoWallClock(GoodSandbox):
        stopped = without("sleep")

    problems = failed(world.probe(world.prepare(), sandbox=NoWallClock()).run())
    assert any(
        "stopped at the wall-clock timeout: expected limit:wall_clock_timeout, was returned" in p
        for p in problems
    )


def test_the_probe_fails_when_nothing_can_be_reached(world: World) -> None:
    """A run in which everything is refused cannot pass: the positive controls fail."""
    cases = world.prepare()

    class Down:
        def resolve(self, *args: Any) -> Any:
            raise TssRequestError(503, "unavailable")

        def resolve_worker(self, *args: Any) -> Any:
            raise TssRequestError(503, "unavailable")

    problems = failed(world.probe(cases, tss=Down()).run())
    assert any("/resolve-worker serves the live grant" in p for p in problems)
    assert any("/resolve refuses the worker" in p for p in problems)


# --- The inspection checks ---------------------------------------------------------------------


def test_the_probes_pdfs_are_the_test_file_scripts_own() -> None:
    """The probe's three PDFs are constants in the image. They are exactly what
    scripts/make_test_files.py writes, so they are synthetic and cannot drift from it."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "make_test_files.py"
    spec = importlib.util.spec_from_file_location("make_test_files_for_probe", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    made = module.files(None)
    assert probe_files.plain() == made["01-normal.pdf"]
    assert probe_files.protected() == made["03-password-protected.pdf"]
    assert probe_files.protected_128() == made["07-password-protected-128.pdf"]


def test_an_inspection_that_lets_a_protected_pdf_through_fails_its_check(world: World) -> None:
    class Blind(GoodSandbox):
        def inspect(self, path: Path) -> runner.InspectResult:
            return runner.InspectResult(problem=None, pages=3)

    problems = failed(world.probe(world.prepare(), sandbox=Blind()).run())
    assert any("holds a password-protected PDF (40-bit)" in p for p in problems)
    assert any("holds a password-protected PDF (128-bit)" in p for p in problems)
    assert any("refuses a file that is not a PDF" in p for p in problems)


def test_an_inspection_whose_child_is_stopped_fails_its_check(world: World) -> None:
    class Crashing(GoodSandbox):
        def inspect(self, path: Path) -> runner.InspectResult:
            raise runner.SandboxKilled(11)

    problems = failed(world.probe(world.prepare(), sandbox=Crashing()).run())
    assert any("opens a plain PDF" in p and "error:signal_SIGSEGV" in p for p in problems)
    # Nothing is left in the work directory, whatever happened.
    assert list(world.work_dir.iterdir()) == []
