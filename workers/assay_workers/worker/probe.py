"""What the worker task can and cannot reach, tried for real, as the worker's own role.

Part of the isolation tests (`tests/isolation`), but it has to run as the worker task: only
that role may call `/resolve-worker` or sign in as `app_worker`, and only a Fargate task shows
whether the sandbox's confinement holds on Fargate. `scripts/run_isolation.py` starts the
task with `WORKER_COMMAND=probe` and `PROBE_CASES`, which the isolation Lambda prepared: three
fixture documents in the two test tenants (two in tenant A, one in tenant B), each with an
object at its `originals/` key, and grants in every state.

Checked (owner conditions b and c, 2026-11-24):

- **TSS.** `/resolve` refuses the worker for every access. `/resolve-worker` issues
  credentials for the live grant, for the tenant the grant names, and refuses an ended, an
  expired, a wrong-attempt and a wrong-token grant, and another document under this
  document's token.
- **Storage.** With the live grant's credentials: the document's own original is read (so a
  run in which everything fails cannot pass); another document of the same tenant, a
  document of the other tenant, and the tenant's `quarantine/` and `derived/` fixtures are
  refused, as is writing another document's key. Each refused object exists.
- **Database.** An `app_worker` session that is not bound sees no rows. Ended, expired and
  wrong-attempt grants do not bind. A bound session sees its one document, not its tenant's
  other document nor the other tenant's, and may not read `users`, the chat tables or
  `audit_log`, nor any file name. A session whose grant ends while it is connected sees
  nothing from then on.
- **Sandbox.** The child runs as the sandbox user with an empty environment and no AWS or
  database library loaded; it cannot open a socket, read the parent's environment or
  memory, start a program or write a file; it is stopped at the CPU limit (a child that
  spins), the wall-clock timeout (one that only waits), the memory limit and the output
  limit, each tried where only that limit can be what stops it. The parent has exactly
  CAP_SETUID, CAP_SETGID and CAP_KILL, and a read-only root filesystem.
- **Inspection.** The real `inspect` job, in that sandbox, on four synthetic files
  (`probe_files`): a plain PDF opens with its three pages; each of two password-protected
  PDFs (40-bit and 128-bit) is reported `pdf_encrypted`, which is what puts a document on
  hold; a file with a PDF header and nothing else is `not_a_pdf`. GuardDuty holds a
  password-protected upload at the malware scan when it recognises one, so an upload may
  never reach this step: this is where the step's own check is shown to work in AWS.

Nothing is written to a bucket: each write tried here must be refused. One `document_events`
row is written for the bound document (the positive control for the database).
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from assay_tss import keys, naming
from assay_tss.client import TssClient, TssRequestError
from assay_tss.models import REQUESTABLE_ACCESS, StorageContext, source_identity
from assay_workers.sandbox import runner
from assay_workers.sandbox.confine import Limits
from assay_workers.worker import db, probe_files
from assay_workers.worker.db import GrantRefused, Job

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

log = logging.getLogger("assay.worker.probe")

ALLOWED, DENIED, REFUSED = "allowed", "denied", "refused"
DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "403", "Forbidden"})
# Fail fast: an unreachable endpoint must not look like a refusal.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})
# CAP_KILL (5), CAP_SETGID (6), CAP_SETUID (7): the parent's whole capability set.
PARENT_CAPABILITIES = (1 << 5) | (1 << 6) | (1 << 7)
READY_FOR_END = "ready_for_end"
DEFAULT_WORK_DIR = "/work"
# A PDF header and then nothing a parser can use.
NOT_A_PDF = b"%PDF-1.7 " + bytes(range(256)) * 8
END_WAIT_SECONDS = 240
SHORT = Limits(wall_seconds=5.0, cpu_seconds=4, address_space_bytes=512 * 1024**2)
# Each of the two time limits is tried where only it can be the one that stops the child:
# a child that spins, with the wall clock far off, and one that waits, using no CPU time.
CPU_LIMITED = Limits(wall_seconds=30.0, cpu_seconds=2, address_space_bytes=512 * 1024**2)
WALL_LIMITED = Limits(wall_seconds=3.0, cpu_seconds=30, address_space_bytes=512 * 1024**2)
# How the kernel ends a process at its CPU limit: SIGKILL at the hard limit, which is also
# the soft one here (SIGXCPU, had the soft limit been lower).
CPU_LIMIT_SIGNALS = frozenset({"signal_SIGKILL", "signal_SIGXCPU"})
CPU_LIMIT = "cpu_limit"


@dataclass(frozen=True)
class Check:
    name: str
    expected: str
    outcome: str

    @property
    def ok(self) -> bool:
        return self.outcome == self.expected


def _job(case: Mapping[str, Any], **changes: Any) -> Job:
    values = {**case, **changes}
    return Job(
        document_id=uuid.UUID(str(values["document_id"])),
        attempt=int(values["attempt"]),
        token=str(values["token"]),
    )


def s3_attempt(call: Callable[[], object]) -> str:
    """`allowed`, `denied`, or `error:<code>` for anything that is neither."""
    try:
        call()
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return DENIED if code in DENIED_CODES else f"error:{code}"
    except BotoCoreError as exc:
        return f"error:{type(exc).__name__}"
    return ALLOWED


def _s3_client(ctx: StorageContext) -> S3Client:
    """S3, with the credentials the TSS issued for the grant, and nothing else."""
    return ctx.session().client("s3", region_name=ctx.region, config=CLIENT_CONFIG)


class Probe:
    def __init__(
        self,
        cases: Mapping[str, Any],
        *,
        tss: TssClient,
        connect: Callable[[], psycopg.Connection],
        name_prefix: str,
        sandbox: Any = runner,
        sleep: Callable[[float], None] = time.sleep,
        s3: Callable[[StorageContext], Any] | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.cases = cases
        self.tss = tss
        self.connect = connect
        self.name_prefix = name_prefix
        self.sandbox = sandbox
        self.sleep = sleep
        self.s3 = s3 or _s3_client
        self.work_dir = work_dir or Path(DEFAULT_WORK_DIR)
        self.checks: list[Check] = []
        self.own = _job(cases["own"])
        self.tenant_a = uuid.UUID(str(cases["tenant_a"]))
        self.tenant_b = uuid.UUID(str(cases["tenant_b"]))
        self.sibling = uuid.UUID(str(cases["sibling"]["document_id"]))
        self.other = uuid.UUID(str(cases["other_tenant"]["document_id"]))

    def check(self, name: str, expected: str, outcome: str) -> None:
        check = Check(name, expected, outcome)
        self.checks.append(check)
        log.info(
            "probe",
            extra={"check": name, "expected": expected, "outcome": outcome, "ok": check.ok},
        )

    # --- The Tenant Storage Service ---------------------------------------------------------

    def _resolve_worker(self, job: Job) -> str:
        try:
            self.tss.resolve_worker(job.document_id, job.attempt, job.token)
        except TssRequestError as exc:
            return (
                REFUSED
                if (exc.status, exc.code) == (403, "worker_grant_refused")
                else (f"error:{exc.status}:{exc.code}")
            )
        return ALLOWED

    def tss_checks(self) -> StorageContext | None:
        for access in REQUESTABLE_ACCESS:
            try:
                self.tss.resolve(self.tenant_a, access, source_identity("worker", "probe"))
                outcome = ALLOWED
            except TssRequestError as exc:
                # API Gateway's own refusal (the role's policy names only the worker's
                # route) or the service's: either way the worker gets nothing.
                outcome = REFUSED if exc.status == 403 else f"error:{exc.status}:{exc.code}"
            self.check(f"/resolve refuses the worker ({access.value})", REFUSED, outcome)

        for name, job in (
            ("an ended grant", _job(self.cases["ended"])),
            ("an expired grant", _job(self.cases["expired"])),
            ("the wrong attempt", _job(self.cases["own"], attempt=self.own.attempt + 1)),
            ("a wrong token", _job(self.cases["own"], token=self.own.token[::-1])),
            (
                "another document under this token",
                _job(self.cases["own"], document_id=self.sibling),
            ),
            (
                "another tenant's document under this token",
                _job(self.cases["own"], document_id=self.other),
            ),
        ):
            self.check(f"/resolve-worker refuses {name}", REFUSED, self._resolve_worker(job))

        try:
            ctx = self.tss.resolve_worker(self.own.document_id, self.own.attempt, self.own.token)
        except TssRequestError as exc:
            self.check(
                "/resolve-worker serves the live grant", ALLOWED, f"error:{exc.status}:{exc.code}"
            )
            return None
        self.check("/resolve-worker serves the live grant", ALLOWED, ALLOWED)
        self.check(
            "the credentials are for the tenant the grant names",
            str(self.tenant_a),
            str(ctx.tenant_id),
        )
        return ctx

    # --- Storage ----------------------------------------------------------------------------

    def storage_checks(self, ctx: StorageContext) -> None:
        s3 = self.s3(ctx)
        other_bucket = naming.bucket_name(self.name_prefix, self.tenant_b)

        def get(bucket: str, key: str) -> Callable[[], object]:
            return lambda: s3.get_object(Bucket=bucket, Key=key)["Body"].read(1)

        def put(bucket: str, key: str) -> Callable[[], object]:
            return lambda: s3.put_object(Bucket=bucket, Key=key, Body=b"probe")

        own_key = ctx.original_key(self.own.document_id)
        sibling_key = ctx.original_key(self.sibling)
        other_key = ctx.original_key(self.other)
        for name, expected, call in (
            ("reads its own document's original", ALLOWED, get(ctx.bucket, own_key)),
            ("another document of the same tenant", DENIED, get(ctx.bucket, sibling_key)),
            ("writing another document's key", DENIED, put(ctx.bucket, sibling_key)),
            ("the other tenant's document", DENIED, get(other_bucket, other_key)),
            ("writing into the other tenant's bucket", DENIED, put(other_bucket, other_key)),
            (
                "the tenant's quarantine fixture",
                DENIED,
                get(ctx.bucket, keys.isolation_fixture_quarantine_key()),
            ),
            (
                "the tenant's derived text fixture",
                DENIED,
                get(ctx.bucket, keys.isolation_fixture_text_key()),
            ),
        ):
            self.check(f"storage: {name}", expected, s3_attempt(call))

    # --- The database -------------------------------------------------------------------------

    @staticmethod
    def _count(connection: psycopg.Connection, table: str) -> str:
        """The number of rows the session sees, or `denied`."""
        try:
            row = connection.execute(f"SELECT count(*) FROM assay.{table}").fetchone()  # noqa: S608  # nosec B608
            connection.commit()
        except psycopg.errors.InsufficientPrivilege:
            connection.rollback()
            return DENIED
        return str(row[0]) if row else "error"

    @staticmethod
    def _documents(connection: psycopg.Connection) -> str:
        rows = connection.execute("SELECT id FROM assay.documents ORDER BY id").fetchall()
        connection.commit()
        return ",".join(str(r[0]) for r in rows) or "none"

    def _binds(self, job: Job) -> str:
        with self.connect() as connection:
            try:
                db.bind(connection, job)
            except GrantRefused:
                return REFUSED
            return ALLOWED

    def database_checks(self) -> None:
        with self.connect() as unbound:
            for table in ("documents", "batch_files", "document_events"):
                self.check(
                    f"database: an unbound session sees no {table}",
                    "0",
                    self._count(unbound, table),
                )

        for name, job in (
            ("an ended grant", _job(self.cases["ended"])),
            ("an expired grant", _job(self.cases["expired"])),
            ("the wrong attempt", _job(self.cases["own"], attempt=self.own.attempt + 1)),
            ("another document under this token", _job(self.cases["own"], document_id=self.other)),
        ):
            self.check(f"database: {name} does not bind", REFUSED, self._binds(job))

        with self.connect() as bound:
            try:
                tenant = db.bind(bound, self.own)
            except GrantRefused:
                self.check("database: the live grant binds", ALLOWED, REFUSED)
                return
            self.check("database: the live grant binds", ALLOWED, ALLOWED)
            self.check("database: bound to the grant's tenant", str(self.tenant_a), str(tenant))
            self.check(
                "database: a bound session sees its one document",
                str(self.own.document_id),
                self._documents(bound),
            )
            for table in ("users", "tenants", "audit_log", "chat_conversations", "chat_audit"):
                self.check(
                    f"database: a bound session cannot read {table}",
                    DENIED,
                    self._count(bound, table),
                )
            for table in ("documents", "batch_files"):
                try:
                    bound.execute(f"SELECT file_name FROM assay.{table}")  # noqa: S608  # nosec B608
                    outcome = ALLOWED
                except psycopg.errors.InsufficientPrivilege:
                    outcome = DENIED
                bound.rollback()
                self.check(f"database: no file name from {table}", DENIED, outcome)
            # The positive control: its own document takes an event; another's does not.
            for name, document, expected in (
                ("its own document", self.own.document_id, ALLOWED),
                ("its tenant's other document", self.sibling, DENIED),
            ):
                try:
                    bound.execute(
                        "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
                        "VALUES (%s, %s, 'isolation_probe', 'checked')",
                        (self.tenant_a, document),
                    )
                    bound.commit()
                    outcome = ALLOWED
                except psycopg.errors.InsufficientPrivilege:
                    bound.rollback()
                    outcome = DENIED
                self.check(f"database: an event for {name}", expected, outcome)

        self.grant_ends_mid_session()

    def grant_ends_mid_session(self) -> None:
        """Bind to a live grant, say so, and watch the document disappear when the isolation
        run ends that grant (scripts/run_isolation.py does, once it reads the line below)."""
        ending = _job(self.cases["to_end"])
        with self.connect() as session:
            try:
                db.bind(session, ending)
            except GrantRefused:
                self.check(
                    "database: a grant that ends mid-session", "saw_then_nothing", "not_bound"
                )
                return
            before = self._documents(session)
            log.info(
                "probe", extra={"signal": READY_FOR_END, "document_id": str(ending.document_id)}
            )
            deadline = time.monotonic() + END_WAIT_SECONDS
            after = before
            while time.monotonic() < deadline:
                after = self._documents(session)
                if after == "none":
                    break
                self.sleep(2.0)
            saw = before == str(ending.document_id)
            outcome = (
                "saw_then_nothing"
                if saw and after == "none"
                else f"before={saw},after={after != 'none'}"
            )
            self.check("database: a grant that ends mid-session", "saw_then_nothing", outcome)
            self.check(
                "database: an ended grant's session sees no batch_files",
                "0",
                self._count(session, "batch_files"),
            )

    # --- The sandbox ----------------------------------------------------------------------------

    def sandbox_checks(self) -> None:
        try:
            report = self.sandbox.selftest("report")
        except runner.SandboxError as exc:
            self.check("sandbox: the child runs and reports", ALLOWED, f"error:{exc.reason}")
            return
        self.check("sandbox: the child runs and reports", ALLOWED, ALLOWED)
        for name, expected, value in (
            ("runs as the sandbox user", str(runner.SANDBOX_UID), str(report.uid)),
            ("runs as the sandbox group", str(runner.SANDBOX_GID), str(report.gid)),
            (
                "has no other group",
                "[]",
                str([g for g in report.groups or [] if g != runner.SANDBOX_GID]),
            ),
            ("was given an empty environment", "[]", str(runner.inherited_environment(report))),
            ("has no AWS or database library loaded", "[]", str(report.forbidden_modules)),
            ("cannot open a socket", "PermissionError", str(report.socket)),
            (
                "cannot read the parent's environment",
                "PermissionError",
                str(report.parent_environment),
            ),
            ("cannot open the parent's memory", "PermissionError", str(report.parent_memory_file)),
            ("cannot read the parent's memory", "PermissionError", str(report.parent_memory_read)),
            ("cannot start a program", "PermissionError", str(report.exec)),
            ("can gain no privileges", "1", str(report.no_new_privs)),
            ("runs under a seccomp filter", "2", str(report.seccomp)),
        ):
            self.check(f"sandbox: the child {name}", expected, value)
        for name, value in (("/work", report.write_work), ("/tmp", report.write_tmp)):  # noqa: S108  # nosec B108
            self.check(
                f"sandbox: the child cannot write to {name}",
                DENIED,
                DENIED if value not in (None, "allowed") else str(value),
            )
        for name, probe, limits, expected in (
            ("that spins is stopped by the CPU limit", "spin", CPU_LIMITED, CPU_LIMIT),
            (
                "that never finishes is stopped at the wall-clock timeout",
                "sleep",
                WALL_LIMITED,
                "timeout",
            ),
            ("that tries to exhaust memory is stopped", "memory", SHORT, "out_of_memory"),
            ("that floods its output is stopped", "flood", SHORT, "too_much_output"),
        ):
            try:
                self.sandbox.selftest(probe, limits=limits)
                outcome = "returned"
            except runner.SandboxError as exc:
                outcome = exc.reason
            if expected == CPU_LIMIT and outcome in CPU_LIMIT_SIGNALS:
                # Killed by the kernel before the parent's wall clock: the CPU limit.
                outcome = CPU_LIMIT
            self.check(f"sandbox: a child {name}", expected, outcome)

    def inspect_checks(self) -> None:
        """The inspection job itself, in the sandbox, on files whose answers are known."""
        for name, body, expected in (
            ("opens a plain PDF and counts its pages", probe_files.plain(), "pages=3"),
            (
                "holds a password-protected PDF (40-bit)",
                probe_files.protected(),
                "problem=pdf_encrypted",
            ),
            (
                "holds a password-protected PDF (128-bit)",
                probe_files.protected_128(),
                "problem=pdf_encrypted",
            ),
            ("refuses a file that is not a PDF", NOT_A_PDF, "problem=not_a_pdf"),
        ):
            with tempfile.TemporaryDirectory(dir=self.work_dir) as work:
                # As the worker does it: the sandbox user may reach the file and read it.
                Path(work).chmod(0o755)  # nosec B103
                path = Path(work) / "document.pdf"
                path.write_bytes(body)
                path.chmod(0o444)
                try:
                    result = self.sandbox.inspect(path)
                    outcome = (
                        f"pages={result.pages}"
                        if result.problem is None
                        else f"problem={result.problem}"
                    )
                except runner.SandboxError as exc:
                    outcome = f"error:{exc.reason}"
            self.check(f"inspection: {name}", expected, outcome)

    def parent_checks(self) -> None:
        try:
            lines = Path("/proc/self/status").read_text().splitlines()
        except OSError:
            lines = []
        status = {
            line.split(":")[0]: line.split(":", 1)[1].strip() for line in lines if ":" in line
        }
        for field in ("CapEff", "CapPrm", "CapBnd"):
            self.check(
                f"parent: {field} is CAP_KILL, CAP_SETGID and CAP_SETUID only",
                f"{PARENT_CAPABILITIES:016x}",
                status.get(field, "missing"),
            )
        try:
            Path("/probe-write-test").write_bytes(b"x")
            outcome = ALLOWED
        except OSError:
            outcome = DENIED
        self.check("parent: the root filesystem is read-only", DENIED, outcome)

    def run(self) -> list[Check]:
        ctx = self.tss_checks()
        if ctx is not None:
            self.storage_checks(ctx)
        self.database_checks()
        self.sandbox_checks()
        self.inspect_checks()
        self.parent_checks()
        return self.checks


def main(env: Mapping[str, str]) -> int:
    from assay_db.connect import DbTarget, iam_connection

    cases = json.loads(env["PROBE_CASES"])
    target = DbTarget.from_env()
    probe = Probe(
        cases,
        tss=TssClient(env["TSS_ENDPOINT_URL"], region=env["AWS_REGION"]),
        connect=lambda: iam_connection(target, db.DB_USER, "assay-worker-probe"),
        name_prefix=env["NAME_PREFIX"],
        work_dir=Path(env.get("WORK_DIR", DEFAULT_WORK_DIR)),
    )
    checks = probe.run()
    failed = [c for c in checks if not c.ok]
    log.info("probe finished", extra={"checks": len(checks), "failed": len(failed)})
    return 1 if failed or not checks else 0
