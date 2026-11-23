"""Adding and provisioning a tenant, against a real PostgreSQL 17 (BUILD_SPEC.md section 6).

The database side is real: the task signs in as `platform_writer`, the TSS reads the result
back as `tss`, and every audit row is checked. The tenant's Pulumi stack and the Cognito
invitation are fakes; `tests/unit/test_provision_image.py` runs the real stack program.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from assay_db import tenants
from assay_db.roles import PLATFORM_WRITER, TSS
from assay_tss import naming
from assay_tss.models import IsolationMode
from assay_tss.policies import Principals
from assay_tss.provision.invite import InviteError, InviteResult
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import StackError, StackOutputs
from assay_tss.provision.task import (
    CRASH_STEP,
    Outcome,
    Provisioner,
    ProvisioningFailed,
    ProvisionRequest,
    Step,
)
from assay_tss.service import DbStorageReader, Resolver, ServiceSettings
from tests.unit.pgtest import TestDb, _conninfo, requires_db

pytestmark = requires_db

REGION = "us-east-1"
ACCOUNT = "000000000000"
PREFIX = "assay-dev"
ROLE = f"arn:aws:iam::{ACCOUNT}:role"
TENANT_ROLE = f"{ROLE}/assay-dev-tenant"
OPERATOR = f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/someone"
SETTINGS = ProvisionSettings(
    region=REGION,
    account_id=ACCOUNT,
    name_prefix=PREFIX,
    principals=Principals(
        tenant_role_arn=TENANT_ROLE,
        break_glass_role_arn=f"{ROLE}/break-glass",
        provisioning_role_arn=f"{ROLE}/provisioning",
        scan_role_arn=f"{ROLE}/scan",
    ),
    state_bucket="state",
    state_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/state",
    invite_function="invite",
    vector_dimensions=1024,
    default_tags={"app": "assay-insights"},
    work_dir=Path("unused"),
)


def engine_as(db: TestDb, role: str) -> Engine:
    return create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(_conninfo(db.name, role)),
        poolclass=NullPool,
    )


@pytest.fixture
def writer(db: TestDb) -> Iterator[Engine]:
    engine = engine_as(db, PLATFORM_WRITER)
    yield engine
    engine.dispose()


class FakeStacks:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[uuid.UUID, bool]] = []
        self.before_return: Any = None

    def up(self, tenant_id: uuid.UUID, *, recover: bool = False) -> StackOutputs:
        self.calls.append((tenant_id, recover))
        if self.fail:
            raise StackError("creating S3 Bucket: BucketAlreadyExists")
        if self.before_return:
            self.before_return(tenant_id)
        return StackOutputs(
            bucket=naming.bucket_name(PREFIX, tenant_id),
            kms_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant_id}",
            vector_bucket=naming.vector_bucket_name(PREFIX, tenant_id),
            vector_index=naming.VECTOR_INDEX,
            face_collection=naming.face_collection_id(PREFIX, tenant_id),
        )


class FakeInviter:
    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def invite(self, *, tenant_id: uuid.UUID, email: str, name: str) -> InviteResult:
        self.calls.append({"tenant_id": tenant_id, "email": email, "name": name})
        if self.error:
            raise InviteError(self.error)
        return InviteResult(cognito_sub=f"sub-{tenant_id}", created=len(self.calls) == 1)


def request(tenant_id: uuid.UUID, *, retry: bool = False) -> ProvisionRequest:
    return ProvisionRequest.model_validate(
        {
            "tenant_id": str(tenant_id),
            "name": "  Example   Lending, Inc ",
            "first_admin": {"name": "Ada Admin", "email": f"ada-{tenant_id.hex[:8]}@example.test"},
            "actor": {"role": "operator", "principal": OPERATOR},
            "retry": retry,
        }
    )


def provisioner(
    writer: Engine, stacks: FakeStacks | None = None, inviter: FakeInviter | None = None
) -> tuple[Provisioner, FakeStacks, FakeInviter]:
    stacks = stacks or FakeStacks()
    inviter = inviter or FakeInviter()
    return Provisioner(SETTINGS, writer, stacks, inviter), stacks, inviter


def tenant_status(db: TestDb, tenant_id: uuid.UUID) -> str | None:
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            "SELECT status FROM assay.tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def audit(db: TestDb, tenant_id: uuid.UUID) -> list[tuple[str, str, str, str | None, Any]]:
    """(category, action, outcome, actor_role, detail), oldest first."""
    with psycopg.connect(db.url) as conn:
        return conn.execute(
            "SELECT category, action, outcome, actor_role, detail FROM assay.audit_log "
            "WHERE tenant_id = %s ORDER BY at, action",
            (tenant_id,),
        ).fetchall()


def actions(db: TestDb, tenant_id: uuid.UUID) -> list[str]:
    return sorted(f"{category}.{action}" for category, action, *_ in audit(db, tenant_id))


def count(db: TestDb, table: str, tenant_id: uuid.UUID) -> int:
    assert table in {"users", "tenant_storage"}
    with psycopg.connect(db.url) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM assay.{table} WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- The add-tenant path ----------------------------------------------------------------------


def test_add_tenant_inserts_a_provisioning_tenant_and_its_audit_row(
    db: TestDb, writer: Engine
) -> None:
    tenant_id = uuid.uuid4()
    actor = tenants.Actor(
        role="master-admin", user_id=db.seed.user_platform, email="m@example.test"
    )
    admin = tenants.FirstAdmin("Ada Admin", "ada@example.test")
    with writer.begin() as conn:
        assert tenants.add_tenant(
            conn, tenant_id=tenant_id, name="New Lender", first_admin=admin, actor=actor
        )
        row = tenants.get_tenant(conn, tenant_id)
    assert row == tenants.TenantRow(tenant_id, "New Lender", "business", "provisioning")
    [(category, action, outcome, actor_role, detail)] = audit(db, tenant_id)
    assert (category, action, outcome, actor_role) == (
        "tenant",
        "created",
        "success",
        "master-admin",
    )
    assert detail["after"] == {
        "name": "New Lender",
        "kind": "business",
        "status": "provisioning",
        "first_admin": {"name": "Ada Admin", "email": "ada@example.test"},
    }


def test_add_tenant_twice_changes_nothing(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    admin = tenants.FirstAdmin("Ada Admin", "ada@example.test")
    actor = tenants.Actor(role="operator", principal=OPERATOR)
    for expected in (True, False):
        with writer.begin() as conn:
            inserted = tenants.add_tenant(
                conn, tenant_id=tenant_id, name="Lender", first_admin=admin, actor=actor
            )
        assert inserted is expected
    assert actions(db, tenant_id) == ["tenant.created"]


@pytest.mark.parametrize(
    ("name", "admin_name", "email"),
    [("", "Ada", "a@example.test"), ("Lender", " ", "a@example.test"), ("Lender", "Ada", "nope")],
)
def test_add_tenant_checks_its_input(
    db: TestDb, writer: Engine, name: str, admin_name: str, email: str
) -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(ValueError, match=r"must be|not valid"), writer.begin() as conn:
        tenants.add_tenant(
            conn,
            tenant_id=tenant_id,
            name=name,
            first_admin=tenants.FirstAdmin(admin_name, email),
            actor=tenants.SYSTEM,
        )
    assert tenant_status(db, tenant_id) is None


# --- A successful run -------------------------------------------------------------------------


def test_provisioning_makes_the_tenant_active(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    subject, stacks, inviter = provisioner(writer)

    assert subject.run(request(tenant_id)) is Outcome.ACTIVE

    assert tenant_status(db, tenant_id) == "active"
    assert stacks.calls == [(tenant_id, False)]
    # The tenant ID Cognito gets is the tenant row's own.
    [invitation] = inviter.calls
    assert invitation["tenant_id"] == tenant_id
    assert invitation["name"] == "Ada Admin"

    with psycopg.connect(db.url) as conn:
        tenant = conn.execute(
            "SELECT name, kind FROM assay.tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
        user = conn.execute(
            "SELECT id, cognito_sub, email, role, status, invited_by FROM assay.users "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    assert tenant == ("Example Lending, Inc", "business")
    assert user == (
        tenants.first_admin_user_id(tenant_id, f"sub-{tenant_id}"),
        f"sub-{tenant_id}",
        f"ada-{tenant_id.hex[:8]}@example.test",
        "tenant-admin",
        "invited",
        None,
    )


def test_provisioning_writes_every_audit_row(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    provisioner(writer)[0].run(request(tenant_id))
    rows = {
        f"{c}.{a}": (outcome, role, detail) for c, a, outcome, role, detail in audit(db, tenant_id)
    }
    assert sorted(rows) == ["tenant.created", "tenant.provisioning_succeeded", "user.invited"]
    assert rows["tenant.created"][1] == "operator"
    assert rows["tenant.created"][2]["started_by"] == OPERATOR
    assert rows["user.invited"][2]["after"]["role"] == "tenant-admin"
    outcome, role, detail = rows["tenant.provisioning_succeeded"]
    assert (outcome, role) == ("success", "system")
    assert detail == {"before": {"status": "provisioning"}, "after": {"status": "active"}}


def test_the_tss_reads_back_what_provisioning_wrote(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    provisioner(writer)[0].run(request(tenant_id))
    tss_engine = engine_as(db, TSS)
    try:
        reader = DbStorageReader(tss_engine)
        profile = reader.profile(tenant_id)
        assert profile is not None
        assert profile.mode is IsolationMode.DEDICATED
        assert profile.bucket == naming.bucket_name(PREFIX, tenant_id)
        assert profile.role_arn == TENANT_ROLE
        assert profile.region == REGION
        assert reader.profile(uuid.uuid4()) is None
        # And the row passes the TSS's own checks.
        settings = ServiceSettings(
            region=REGION, name_prefix=PREFIX, tenant_role_arn=TENANT_ROLE, callers={}
        )
        checked = Resolver(settings, reader, sts=None)._checked_profile(tenant_id)  # type: ignore[arg-type]
        assert checked == profile
    finally:
        tss_engine.dispose()


def test_running_again_for_an_active_tenant_does_nothing(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    provisioner(writer)[0].run(request(tenant_id))
    subject, stacks, inviter = provisioner(writer)
    assert subject.run(request(tenant_id)) is Outcome.ALREADY_ACTIVE
    assert stacks.calls == [] and inviter.calls == []
    assert len(audit(db, tenant_id)) == 3


# --- Failures: the failed step is recorded, the tenant is `failed`, a retry works -------------


def test_storage_failure_is_recorded(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    subject, _, inviter = provisioner(writer, stacks=FakeStacks(fail=True))
    with pytest.raises(ProvisioningFailed) as raised:
        subject.run(request(tenant_id))
    assert raised.value.step == Step.STORAGE
    assert tenant_status(db, tenant_id) == "failed"
    assert inviter.calls == []
    assert count(db, "tenant_storage", tenant_id) == 0
    failed = [row for row in audit(db, tenant_id) if row[1] == "provisioning_failed"]
    [(category, _, outcome, role, detail)] = failed
    assert (category, outcome, role) == ("tenant", "failure", "system")
    assert detail["step"] == "storage"
    assert "BucketAlreadyExists" in detail["error"]
    assert detail["after"] == {"status": "failed"}


def test_invitation_failure_is_recorded(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    subject, _, _ = provisioner(writer, inviter=FakeInviter(error="email_in_use"))
    with pytest.raises(ProvisioningFailed) as raised:
        subject.run(request(tenant_id))
    assert raised.value.step == Step.INVITE_ADMIN
    assert tenant_status(db, tenant_id) == "failed"
    assert count(db, "tenant_storage", tenant_id) == 1
    assert count(db, "users", tenant_id) == 0
    [detail] = [row[4] for row in audit(db, tenant_id) if row[1] == "provisioning_failed"]
    assert detail["step"] == "invite_admin"
    assert detail["error"] == "InviteError: email_in_use"


@pytest.mark.parametrize(
    ("function", "step"),
    [("record_storage", Step.STORAGE_ROW), ("activate", Step.ACTIVATE)],
)
def test_database_step_failure_is_recorded(
    db: TestDb, writer: Engine, monkeypatch: pytest.MonkeyPatch, function: str, step: Step
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database step failed")

    tenant_id = uuid.uuid4()
    monkeypatch.setattr(tenants, function, boom)
    with pytest.raises(ProvisioningFailed) as raised:
        provisioner(writer)[0].run(request(tenant_id))
    assert raised.value.step == step
    assert tenant_status(db, tenant_id) == "failed"
    [detail] = [row[4] for row in audit(db, tenant_id) if row[1] == "provisioning_failed"]
    assert detail["step"] == step.value


def test_retry_after_a_failure_finishes_without_duplicates(
    db: TestDb, writer: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = uuid.uuid4()
    # The first run fails last, after the storage row is written and the admin is invited.
    with monkeypatch.context() as patch:
        patch.setattr(tenants, "activate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(ProvisioningFailed):
            provisioner(writer)[0].run(request(tenant_id))
    assert tenant_status(db, tenant_id) == "failed"

    subject, stacks, inviter = provisioner(writer)
    assert subject.run(request(tenant_id, retry=True)) is Outcome.ACTIVE

    assert tenant_status(db, tenant_id) == "active"
    # A retry recovers the stack (stale lock, unfinished operations) before converging.
    assert stacks.calls == [(tenant_id, True)]
    assert len(inviter.calls) == 1
    assert count(db, "tenant_storage", tenant_id) == 1
    assert count(db, "users", tenant_id) == 1
    assert actions(db, tenant_id) == [
        "tenant.created",
        "tenant.provisioning_failed",
        "tenant.provisioning_retried",
        "tenant.provisioning_succeeded",
        "user.invited",
    ]


def test_a_failed_tenant_needs_an_explicit_retry(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(ProvisioningFailed):
        provisioner(writer, stacks=FakeStacks(fail=True))[0].run(request(tenant_id))
    before = audit(db, tenant_id)

    subject, stacks, _ = provisioner(writer)
    with pytest.raises(ProvisioningFailed) as raised:
        subject.run(request(tenant_id))
    assert raised.value.step == Step.TENANT
    assert stacks.calls == []
    assert tenant_status(db, tenant_id) == "failed"
    assert audit(db, tenant_id) == before


# --- The invitation uses the tenant row, and only while provisioning --------------------------


def test_no_invitation_unless_the_tenant_row_is_being_provisioned(
    db: TestDb, writer: Engine
) -> None:
    tenant_id = uuid.uuid4()
    stacks = FakeStacks()

    def suspend(tid: uuid.UUID) -> None:
        with psycopg.connect(db.url) as conn:
            conn.execute("UPDATE assay.tenants SET status = 'suspended' WHERE id = %s", (tid,))

    stacks.before_return = suspend
    subject, _, inviter = provisioner(writer, stacks=stacks)
    with pytest.raises(ProvisioningFailed) as raised:
        subject.run(request(tenant_id))
    assert raised.value.step == Step.INVITE_ADMIN
    assert inviter.calls == []
    # A tenant that is not `provisioning` is never overwritten with `failed`.
    assert tenant_status(db, tenant_id) == "suspended"


def test_the_platform_tenant_is_never_provisioned(db: TestDb, writer: Engine) -> None:
    subject, stacks, inviter = provisioner(writer)
    with pytest.raises(ProvisioningFailed) as raised:
        subject.run(request(db.seed.platform, retry=True))
    assert raised.value.step == Step.TENANT
    assert stacks.calls == [] and inviter.calls == []
    assert tenant_status(db, db.seed.platform) == "active"


def test_unexpected_stack_outputs_are_refused(db: TestDb, writer: Engine) -> None:
    class WrongBucket(FakeStacks):
        def up(self, tenant_id: uuid.UUID, *, recover: bool = False) -> StackOutputs:
            good = super().up(tenant_id, recover=recover)
            return StackOutputs(**{**good.__dict__, "bucket": "someone-elses-bucket"})

    tenant_id = uuid.uuid4()
    with pytest.raises(ProvisioningFailed) as raised:
        provisioner(writer, stacks=WrongBucket())[0].run(request(tenant_id))
    assert raised.value.step == Step.STORAGE
    assert count(db, "tenant_storage", tenant_id) == 0


# --- The state machine's catch ----------------------------------------------------------------


def test_record_crash_marks_a_provisioning_tenant_failed_once(db: TestDb, writer: Engine) -> None:
    tenant_id = uuid.uuid4()
    with writer.begin() as conn:
        tenants.add_tenant(
            conn,
            tenant_id=tenant_id,
            name="Lender",
            first_admin=tenants.FirstAdmin("Ada", "ada@example.test"),
            actor=tenants.SYSTEM,
        )
    subject, _, _ = provisioner(writer)
    assert subject.record_crash(tenant_id, "States.TaskFailed") is True
    assert subject.record_crash(tenant_id, "States.TaskFailed") is False
    assert tenant_status(db, tenant_id) == "failed"
    [detail] = [row[4] for row in audit(db, tenant_id) if row[1] == "provisioning_failed"]
    assert detail["step"] == CRASH_STEP
    assert detail["error"] == "States.TaskFailed"


def test_record_crash_ignores_a_tenant_that_was_never_inserted(writer: Engine) -> None:
    assert provisioner(writer)[0].record_crash(uuid.uuid4(), "States.Timeout") is False


# --- Migration 0003 ---------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "status"), [("pooled", "active"), ("dedicated", "deleted")])
def test_tenant_storage_values_are_constrained(db: TestDb, mode: str, status: str) -> None:
    tenant_id = uuid.uuid4()
    with psycopg.connect(db.url) as conn:
        conn.execute(
            "INSERT INTO assay.tenants (id, name, kind) VALUES (%s, 'T', 'business')", (tenant_id,)
        )
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO assay.tenant_storage (tenant_id, mode, bucket, kms_key_arn, "
                "role_arn, vector_bucket, vector_index, region, status) "
                "VALUES (%s, %s, 'b', 'k', 'r', 'vb', 'vi', 'region', %s)",
                (tenant_id, mode, status),
            )
        conn.rollback()
