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
  spins, and one that ignores the kernel's warning), the wall-clock timeout (one that only
  waits), the memory limit and the output limit, each tried where only that limit can be
  what stops it, and each reported by its own name; a child that crashes is reported as an
  unexpected signal, never as a limit. The parent has exactly
  CAP_SETUID, CAP_SETGID and CAP_KILL, and a read-only root filesystem.
- **Inspection.** The real `inspect` job, in that sandbox, on four synthetic files
  (`probe_files`): a plain PDF opens with its three pages; each of two password-protected
  PDFs (40-bit and 128-bit) is reported `pdf_encrypted`, which is what puts a document on
  hold; a file with a PDF header and nothing else is `not_a_pdf`. GuardDuty holds a
  password-protected upload at the malware scan when it recognises one, so an upload may
  never reach this step: this is where the step's own check is shown to work in AWS.

- **The text step (stage 2).**
  - *Storage.* The document's own text object is read; another document's and the other
    tenant's are refused, to read and to write. Writing the document's stored companion file
    is refused: that key left the worker's policy (owner decision F, 2026-11-27).
  - *Database.* A bound session writes a `pages` row and an `ai_usage` row for its own
    attempt. A row for another document, another tenant or another attempt is refused by row
    security, and reading either table is refused. It sets the content pointer for its bound
    attempt, and for no other; and it can update no column of `documents` beyond its eight
    (every column of the table is tried).
  - *The page channel.* The real `text` and `render` jobs run in the sandbox on a synthetic
    PDF; a child that floods the channel is stopped at the output limit, and one that sends
    a record nobody asked for, or something that is not a record, is refused.
  - *Textract.* With the task's own role, through the VPC's endpoint: `DetectDocumentText`
    reads the page the sandbox drew (the positive control: a synthetic page, never a
    tenant's). `AnalyzeDocument` and `StartDocumentTextDetection` are refused. Pointing
    `DetectDocumentText` at an object in a tenant's bucket is refused by Textract itself: the
    task's role can read no bucket. The credentials the TSS issued for the document are
    refused Textract altogether.

Nothing is written to a bucket: each write tried here must be refused. In the database, for
the bound document only: one `document_events` row, one `pages` row, the content pointer, and
one `ai_usage` row for the probe's one Textract call (every AI call writes one).
"""

from __future__ import annotations

import functools
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

from assay_db.models import Document
from assay_tss import keys, naming
from assay_tss.client import TssClient, TssRequestError
from assay_tss.models import REQUESTABLE_ACCESS, StorageContext, source_identity
from assay_workers.sandbox import runner
from assay_workers.sandbox.confine import Limits
from assay_workers.worker import db, ocr, probe_files
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
# The columns of `documents` the worker may update: six since migration 0010, the content
# pointer's two since 0012. Every other column of the table must be refused.
WORKER_DOCUMENT_UPDATES = frozenset(
    {
        "status",
        "error_code",
        "s3_key",
        "size_bytes",
        "page_count",
        "completed_at",
        "content_attempt",
        "text_version_id",
    }
)
# PostgreSQL's own words for each refusal: which mechanism it was is part of the answer.
ROW_SECURITY = "refused:row_security"
PERMISSION_DENIED = "refused:permission_denied"
NOT_THE_BOUND_ATTEMPT = "refused:not_the_bound_attempt"
# What the probe writes into the content pointer of its own fixture document.
PROBE_VERSION = "isolation-probe"
# Textract's refusals, by its own error names.
TEXTRACT_DENIED = "refused:AccessDeniedException"
# Textract's answer when it is pointed at an object the caller's role cannot read.
TEXTRACT_CANNOT_READ_THE_OBJECT = "refused:InvalidS3ObjectException"


def sql_attempt(connection: psycopg.Connection, statement: str, parameters: tuple[Any, ...]) -> str:
    """`allowed`, or which of the database's refusals it was, in PostgreSQL's own words."""
    try:
        connection.execute(statement, parameters)
        connection.commit()
    except psycopg.errors.InsufficientPrivilege as exc:
        connection.rollback()
        message = str(exc)
        if "row-level security" in message:
            return ROW_SECURITY
        if "permission denied" in message:
            return PERMISSION_DENIED
        if "bound attempt" in message:
            return NOT_THE_BOUND_ATTEMPT
        return "refused:other"
    except psycopg.Error as exc:
        connection.rollback()
        return f"error:{exc.sqlstate}"
    return ALLOWED


def textract_attempt(call: Callable[[], object]) -> str:
    """`allowed`, `refused:<Textract's error code>`, or `error:<type>` when no answer came:
    an endpoint that cannot be reached must never look like a refusal."""
    try:
        call()
    except ClientError as exc:
        return f"refused:{exc.response.get('Error', {}).get('Code', '')}"
    except BotoCoreError as exc:
        return f"error:{type(exc).__name__}"
    return ALLOWED


def _textract_client(region: str, ctx: StorageContext | None = None) -> Any:
    """Textract with the task's own role, or (given a context) with the credentials the TSS
    issued for the document."""
    import boto3

    session: Any = boto3 if ctx is None else ctx.session()
    return session.client("textract", region_name=region, config=CLIENT_CONFIG)


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
        region: str = "",
        # Textract for the task's own role (no argument) and for a TSS-issued context.
        textract: Callable[..., Any] | None = None,
    ) -> None:
        self.cases = cases
        self.tss = tss
        self.connect = connect
        self.name_prefix = name_prefix
        self.sandbox = sandbox
        self.sleep = sleep
        self.s3 = s3 or _s3_client
        self.work_dir = work_dir or Path(DEFAULT_WORK_DIR)
        self.region = region
        self.textract = textract or (lambda ctx=None: _textract_client(region, ctx))
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

        # The text step's object (stage 2): its own to read; no other document's, to read or
        # to write. Each one exists: the isolation Lambda wrote it.
        own_text = ctx.text_key(self.own.document_id)
        sibling_text = ctx.text_key(self.sibling)
        other_text = ctx.text_key(self.other)
        for name, expected, call in (
            ("reads its own document's text", ALLOWED, get(ctx.bucket, own_text)),
            ("another document's text, same tenant", DENIED, get(ctx.bucket, sibling_text)),
            ("writing another document's text", DENIED, put(ctx.bucket, sibling_text)),
            ("the other tenant's document's text", DENIED, get(other_bucket, other_text)),
            ("writing the other tenant's text", DENIED, put(other_bucket, other_text)),
            # Owner decision F: the stored companion file is the companion step's to write.
            (
                "writing its own document's stored companion file",
                DENIED,
                put(ctx.bucket, ctx.companion_key(self.own.document_id)),
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

            self.text_row_checks(bound)

        self.grant_ends_mid_session()

    def text_row_checks(self, bound: psycopg.Connection) -> None:
        """What the text step writes (migration 0012), as the bound session. Each refusal is
        named: row security for a row that is not its own attempt's, permission for anything
        it was not granted, and the guard on the content pointer."""
        own, attempt = self.own.document_id, self.own.attempt
        # A page number no earlier run used: the fixture document keeps its rows.
        page_no = uuid.uuid4().int % 2_000_000_000 + 1
        page = (
            "INSERT INTO assay.pages (tenant_id, document_id, attempt, page_no, text_source, "
            "quality_score, char_count) VALUES (%s, %s, %s, %s, 'native', 1.0, 0)"
        )
        usage = (
            "INSERT INTO assay.ai_usage (tenant_id, job, service, operation, document_id, "
            "attempt, page_no, input_tokens, output_tokens, units, unit, unit_price, cost) "
            "VALUES (%s, 'ingest', 'textract', 'DetectDocumentText', %s, %s, %s, 0, 0, 1, "
            "'page', 0, 0)"
        )
        for table, statement in (("pages", page), ("ai_usage", usage)):
            for name, values, expected in (
                ("its own attempt", (self.tenant_a, own, attempt, page_no), ALLOWED),
                (
                    "its tenant's other document",
                    (self.tenant_a, self.sibling, attempt, page_no),
                    ROW_SECURITY,
                ),
                (
                    "the other tenant's document",
                    (self.tenant_b, self.other, attempt, page_no),
                    ROW_SECURITY,
                ),
                (
                    "another attempt of its own document",
                    (self.tenant_a, own, attempt + 1, page_no),
                    ROW_SECURITY,
                ),
            ):
                # The usage row for its own attempt is written with the Textract call it
                # records (`textract_checks`), not here.
                if table == "ai_usage" and expected == ALLOWED:
                    continue
                self.check(
                    f"database: a {table} row for {name}",
                    expected,
                    sql_attempt(bound, statement, values),
                )
            self.check(
                f"database: a bound session cannot read {table}",
                PERMISSION_DENIED,
                sql_attempt(bound, f"SELECT count(*) FROM assay.{table}", ()),  # noqa: S608  # nosec B608
            )

        pointer = (
            "UPDATE assay.documents SET content_attempt = %s, text_version_id = %s WHERE id = %s"
        )
        settings: tuple[tuple[str, tuple[Any, ...], str], ...] = (
            ("a later attempt", (attempt + 1, PROBE_VERSION, own), NOT_THE_BOUND_ATTEMPT),
            ("an earlier attempt", (attempt - 1, PROBE_VERSION, own), NOT_THE_BOUND_ATTEMPT),
            ("no attempt", (None, PROBE_VERSION, own), NOT_THE_BOUND_ATTEMPT),
            ("its bound attempt with no version", (attempt, "", own), NOT_THE_BOUND_ATTEMPT),
            ("its bound attempt", (attempt, PROBE_VERSION, own), ALLOWED),
        )
        for name, setting, expected in settings:
            self.check(
                f"database: the content pointer set for {name}",
                expected,
                sql_attempt(bound, pointer, setting),
            )
        # Every other column of the table, from the model: each must be refused.
        got_through = [
            column
            for column in sorted(Document.__table__.columns.keys())
            if column not in WORKER_DOCUMENT_UPDATES
            and not self._update_is_refused(bound, column, own)
        ]
        self.check(
            "database: no other column of documents can be updated",
            "none",
            ",".join(got_through) or "none",
        )

    @staticmethod
    def _update_is_refused(bound: psycopg.Connection, column: str, document: uuid.UUID) -> bool:
        """Whether the session lacks the privilege to update this column. The column is set
        to its default, which reads nothing (setting it to itself would be refused for the
        missing SELECT alone and prove nothing about UPDATE). Whatever happens is rolled
        back: only a refusal for lack of permission counts."""
        try:
            bound.execute(
                f"UPDATE assay.documents SET {column} = DEFAULT WHERE id = %s",  # noqa: S608  # nosec B608
                (document,),
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            return "permission denied" in str(exc)
        except psycopg.Error:
            return False
        finally:
            bound.rollback()
        return False

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
        # Each ending is reported by its own name: a limit is `limit:<which>`, and a child
        # that died some other way is `unexpected:<how>`, never the name of a limit.
        for name, probe, limits, expected in (
            (
                "that spins is stopped by the CPU limit",
                "spin",
                CPU_LIMITED,
                f"limit:{runner.CPU_LIMIT}",
            ),
            (
                "that ignores the CPU warning is still stopped by the CPU limit",
                "stubborn",
                CPU_LIMITED,
                f"limit:{runner.CPU_LIMIT}",
            ),
            (
                "that never finishes is stopped at the wall-clock timeout",
                "sleep",
                WALL_LIMITED,
                f"limit:{runner.WALL_CLOCK_TIMEOUT}",
            ),
            (
                "that tries to exhaust memory is stopped",
                "memory",
                SHORT,
                f"limit:{runner.MEMORY_LIMIT}",
            ),
            (
                "that floods its output is stopped",
                "flood",
                SHORT,
                f"limit:{runner.OUTPUT_LIMIT}",
            ),
            (
                "that crashes is reported as an unexpected signal, not a limit",
                "crash",
                SHORT,
                "unexpected:signal_SIGABRT",
            ),
        ):
            try:
                self.sandbox.selftest(probe, limits=limits)
                outcome = "returned"
            except runner.SandboxLimit as exc:
                outcome = f"limit:{exc.reason}"
            except runner.SandboxError as exc:
                outcome = f"unexpected:{exc.reason}"
            self.check(f"sandbox: a child {name}", expected, outcome)
        # The page channel (stage 2): a flood is stopped at its limit, and anything that is
        # not exactly the record that was asked for is refused.
        for name, probe, expected in (
            ("that floods the page channel is stopped", "flood", f"limit:{runner.OUTPUT_LIMIT}"),
            (
                "that sends a page nobody asked for is refused",
                "wrong_page",
                f"unexpected:{runner.BAD_OUTPUT}",
            ),
            (
                "that sends something that is not a record is refused",
                "not_a_record",
                f"unexpected:{runner.BAD_OUTPUT}",
            ),
        ):
            try:
                self.sandbox.pageprobe(probe, limits=SHORT)
                outcome = "returned"
            except runner.SandboxLimit as exc:
                outcome = f"limit:{exc.reason}"
            except runner.SandboxError as exc:
                outcome = f"unexpected:{exc.reason}"
            self.check(f"sandbox: a child {name}", expected, outcome)

    def page_checks(self) -> bytes | None:
        """The real `text` and `render` jobs, in the sandbox, on the synthetic plain PDF.
        Returns the picture of its first page, for Textract."""
        picture: bytes | None = None
        with tempfile.TemporaryDirectory(dir=self.work_dir) as work:
            Path(work).chmod(0o755)  # nosec B103
            path = Path(work) / "document.pdf"
            path.write_bytes(probe_files.plain())
            path.chmod(0o444)
            records: list[runner.PageRecord] = []
            try:
                self.sandbox.read_text(path, 1, 3, records.append)
                good = len(records) == 3 and all(r.text.strip() and r.words for r in records)
                outcome = "pages=3,text" if good else f"pages={len(records)}"
            except runner.SandboxError as exc:
                outcome = f"error:{exc.reason}"
            self.check("pages: the text job reads each page's text layer", "pages=3,text", outcome)
            images: list[runner.PageImage] = []
            try:
                self.sandbox.render(path, [1], images.append)
                outcome = "png" if len(images) == 1 else f"images={len(images)}"
            except runner.SandboxError as exc:
                outcome = f"error:{exc.reason}"
            self.check("pages: the render job draws a page as a PNG", "png", outcome)
            if images:
                picture = images[0].data
        return picture

    def textract_checks(self, ctx: StorageContext | None, picture: bytes | None) -> None:
        """Textract, as the worker task's own role and as the document's storage session."""
        if picture is None:
            self.check("textract: reads a synthetic page (the task's role)", ALLOWED, "no_picture")
            return
        client = self.textract()
        read: list[ocr.OcrPage] = []

        def detect() -> None:
            read.append(ocr.parse(client.detect_document_text(Document={"Bytes": picture})))

        outcome = textract_attempt(detect)
        if outcome == ALLOWED and not (read and read[0].words):
            outcome = "no_text"
        # The positive control, and the whole path on Fargate: the sandbox drew the page,
        # the task's role sent it through the VPC's endpoint, Textract read it.
        self.check("textract: reads a synthetic page (the task's role)", ALLOWED, outcome)
        if outcome == ALLOWED:
            self.usage_row(read[0])

        # The operations stage 2 does not use: forms and tables, and the asynchronous one
        # that keeps its results on AWS's side.
        self.check(
            "textract: AnalyzeDocument",
            TEXTRACT_DENIED,
            textract_attempt(
                lambda: client.analyze_document(Document={"Bytes": picture}, FeatureTypes=["FORMS"])
            ),
        )
        if ctx is None:
            return
        own = {"Bucket": ctx.bucket, "Name": ctx.original_key(self.own.document_id)}
        other = {
            "Bucket": naming.bucket_name(self.name_prefix, self.tenant_b),
            "Name": ctx.original_key(self.other),
        }
        self.check(
            "textract: StartDocumentTextDetection",
            TEXTRACT_DENIED,
            textract_attempt(
                lambda: client.start_document_text_detection(DocumentLocation={"S3Object": own})
            ),
        )
        # Textract pointed at a bucket reads it with the caller's own role, and the task's
        # role can read no bucket: no tenant's pages are extracted this way, its own
        # document's included.
        for name, location in (
            ("its own document in the bucket", own),
            ("another tenant's document in its bucket", other),
        ):
            self.check(
                f"textract: pointed at {name}",
                TEXTRACT_CANNOT_READ_THE_OBJECT,
                textract_attempt(
                    functools.partial(client.detect_document_text, Document={"S3Object": location})
                ),
            )
        # The credentials that can read the document cannot call Textract at all.
        session = self.textract(ctx)
        self.check(
            "textract: with the document's storage credentials",
            TEXTRACT_DENIED,
            textract_attempt(lambda: session.detect_document_text(Document={"Bytes": picture})),
        )

    def usage_row(self, page: ocr.OcrPage) -> None:
        """The probe's one Textract call, recorded as every AI call is. The price is not the
        probe's to know: the row records the call, at no price."""
        with self.connect() as bound:
            try:
                db.bind(bound, self.own)
            except GrantRefused:
                self.check("database: a usage row for its own Textract call", ALLOWED, REFUSED)
                return
            outcome = sql_attempt(
                bound,
                "INSERT INTO assay.ai_usage (tenant_id, job, service, operation, model_version, "
                "document_id, attempt, page_no, input_tokens, output_tokens, units, unit, "
                "unit_price, cost, request_id) VALUES (%s, 'ingest', 'textract', "
                "'DetectDocumentText', %s, %s, %s, 1, 0, 0, 1, 'page', 0, 0, %s)",
                (
                    self.tenant_a,
                    page.model_version,
                    self.own.document_id,
                    self.own.attempt,
                    page.request_id,
                ),
            )
            self.check("database: a usage row for its own Textract call", ALLOWED, outcome)

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
        self.textract_checks(ctx, self.page_checks())
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
        region=env["AWS_REGION"],
    )
    checks = probe.run()
    failed = [c for c in checks if not c.ok]
    log.info("probe finished", extra={"checks": len(checks), "failed": len(failed)})
    return 1 if failed or not checks else 0
