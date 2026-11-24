"""`PROVISION_COMMAND=update` (build step 6): run every active tenant's stack again, changing
only what the program change is meant to change."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine

from assay_db.roles import PLATFORM_WRITER
from assay_tss import naming
from assay_tss.policies import Principals
from assay_tss.provision import update
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import PlannedChange, StackOutputs
from tests.unit.pgtest import TestDb, _conninfo, requires_db

PREFIX = "assay-test"
ROLE = "arn:aws:iam::000000000000:role"
SETTINGS = ProvisionSettings(
    region="us-east-1",
    account_id="000000000000",
    name_prefix=PREFIX,
    principals=Principals(f"{ROLE}/t", f"{ROLE}/b", f"{ROLE}/p", f"{ROLE}/s"),
    state_bucket="state",
    state_key_arn="arn:aws:kms:us-east-1:000000000000:key/state",
    invite_function="invite",
    vector_dimensions=1024,
    default_tags={"app": "assay-insights"},
    work_dir=Path("unused"),
    upload_origins=("https://portal.example.test",),
)


def change(name: str, op: str, *diffs: str, type_: str = "aws:x/y:Z") -> PlannedChange:
    return PlannedChange(
        urn=f"urn:pulumi:t::assay-tenant::{type_}::{name}",
        name=name,
        type=type_,
        op=op,
        diffs=tuple(diffs),
    )


# What build step 6's program change makes: the CORS rule, and the plan's prefix.
STEP_6 = [
    change("bucket-cors", "create"),
    change("malware-protection", "update", "protectedResource"),
]


class FakeStacks:
    def __init__(
        self,
        planned: dict[uuid.UUID, list[PlannedChange]] | None = None,
        default: list[PlannedChange] | None = None,
        wrong: set[uuid.UUID] | None = None,
    ) -> None:
        self.planned = planned or {}
        self.default = STEP_6 if default is None else default
        self.wrong = wrong or set()
        self.applied: dict[uuid.UUID, list[str]] = {}

    def plan(self, tenant_id: uuid.UUID) -> list[PlannedChange]:
        return self.planned.get(tenant_id, self.default)

    def apply(self, tenant_id: uuid.UUID, urns: Sequence[str]) -> StackOutputs:
        self.applied[tenant_id] = list(urns)
        bucket = naming.bucket_name(PREFIX, tenant_id)
        if tenant_id in self.wrong:
            bucket = "someone-else"
        return StackOutputs(
            bucket=bucket,
            kms_key_arn="arn:aws:kms:us-east-1:000000000000:key/k",
            vector_bucket=naming.vector_bucket_name(PREFIX, tenant_id),
            vector_index=naming.VECTOR_INDEX,
            face_collection=naming.face_collection_id(PREFIX, tenant_id),
        )


def test_parse_tenants_and_apply() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert update.parse_tenants(f" {a}, {b} ,") == (a, b)
    assert update.parse_tenants("") == ()
    with pytest.raises(ValueError):
        update.parse_tenants("not-an-id")
    assert update.parse_apply("") is True and update.parse_apply("false") is False
    with pytest.raises(ValueError):
        update.parse_apply("maybe")


def test_step_6_allows_exactly_the_cors_rule_and_the_plan_prefix() -> None:
    assert update.unexpected(STEP_6) == []
    for refused in (
        change("malware-protection", "replace", "protectedResource"),
        change("malware-protection", "update", "protectedResource", "role"),
        change("bucket-cors", "update", "corsRules"),
        change("bucket-policy", "update", "policy"),
        change("bucket", "delete"),
        change("key", "update", "policy"),
        change("vector-index", "replace"),
    ):
        assert update.unexpected([*STEP_6, refused]) == [refused], refused


def test_a_tenant_with_only_the_allowed_changes_is_updated_with_those_targets() -> None:
    tenant = uuid.uuid4()
    stacks = FakeStacks()
    assert update.update_tenant(SETTINGS, stacks, tenant, apply=True) is True
    assert stacks.applied[tenant] == [c.urn for c in STEP_6]


def test_any_other_change_refuses_the_tenant_and_applies_nothing() -> None:
    tenant = uuid.uuid4()
    stacks = FakeStacks(default=[*STEP_6, change("bucket-policy", "update", "policy")])
    assert update.update_tenant(SETTINGS, stacks, tenant, apply=True) is False
    assert stacks.applied == {}


def test_preview_applies_nothing_and_nothing_to_do_is_fine() -> None:
    tenant = uuid.uuid4()
    stacks = FakeStacks()
    assert update.update_tenant(SETTINGS, stacks, tenant, apply=False) is True
    assert stacks.applied == {}
    done = FakeStacks(default=[])
    assert update.update_tenant(SETTINGS, done, tenant, apply=True) is True
    assert done.applied == {}


def test_a_stack_that_returns_another_tenants_names_fails() -> None:
    tenant = uuid.uuid4()
    assert update.update_tenant(SETTINGS, FakeStacks(wrong={tenant}), tenant, apply=True) is False


@requires_db
def test_one_tenant_or_every_active_business_tenant(db: TestDb) -> None:
    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(_conninfo(db.name, PLATFORM_WRITER)),
    )
    try:
        active = update.active_tenants(engine)
        # The seed's two business tenants; never the platform tenant.
        assert db.seed.tenant_a in active and db.seed.tenant_b in active
        assert db.seed.platform not in active

        # One tenant only, as the owner runs it first.
        only = FakeStacks()
        assert update.run_update(SETTINGS, engine, only, [db.seed.tenant_a]) == 0
        assert set(only.applied) == {db.seed.tenant_a}

        everyone = FakeStacks()
        assert update.run_update(SETTINGS, engine, everyone) == 0
        assert set(everyone.applied) == set(active)

        # A tenant that is not an active business tenant is reported, and the run fails.
        assert update.run_update(SETTINGS, engine, FakeStacks(), [db.seed.platform]) == 1

        # A tenant with an unexpected change is refused; the others still run.
        mixed = FakeStacks(planned={db.seed.tenant_a: [change("bucket", "replace")]})
        assert update.run_update(SETTINGS, engine, mixed) == 1
        assert db.seed.tenant_a not in mixed.applied
        assert db.seed.tenant_b in mixed.applied

        previewed = FakeStacks()
        assert update.run_update(SETTINGS, engine, previewed, apply=False) == 0
        assert previewed.applied == {}
    finally:
        engine.dispose()


def test_upload_origins_are_https(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {
        "AWS_REGION": "us-east-1",
        "ACCOUNT_ID": "000000000000",
        "NAME_PREFIX": PREFIX,
        "TENANT_ROLE_ARN": f"{ROLE}/t",
        "BREAK_GLASS_ROLE_ARN": f"{ROLE}/b",
        "PROVISIONING_ROLE_ARN": f"{ROLE}/p",
        "SCAN_ROLE_ARN": f"{ROLE}/s",
        "STATE_BUCKET": "state",
        "STATE_KEY_ARN": "arn:aws:kms:us-east-1:000000000000:key/state",
        "INVITE_FUNCTION": "invite",
        "VECTOR_DIMENSIONS": "1024",
        "DEFAULT_TAGS": '{"app": "assay-insights"}',
        "WORK_DIR": "/work",
        "DEFAULT_INGEST_MODEL": "us.example.ingest",
        "DEFAULT_QUERY_MODEL": "us.example.query",
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UPLOAD_ORIGINS", '["https://portal.example.test"]')
    assert ProvisionSettings.from_env().upload_origins == ("https://portal.example.test",)
    for bad in ("[]", '["http://portal.example.test"]', '"https://x"', "[1]"):
        monkeypatch.setenv("UPLOAD_ORIGINS", bad)
        with pytest.raises(ValueError, match="UPLOAD_ORIGINS"):
            ProvisionSettings.from_env()
