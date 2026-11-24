"""Provisioning one tenant, step by step (BUILD_SPEC.md section 6).

Started by the provisioning state machine as a Fargate task. The steps, in order:

1. `tenant`: make sure the tenant row exists with status `provisioning` (the API inserts
   it before starting the execution; `scripts/add_tenant.py` leaves that to this step).
2. `storage`: run the tenant's Pulumi stack: key, bucket, vector bucket and index, malware
   protection plan and face collection.
3. `storage_row`: write the `tenant_storage` row.
4. `invite_admin`: invite the first admin in Cognito, with `custom:tenant_id` taken from
   the tenant row just read back from the database.
5. `activate`: the admin's user row, status `active`, and the audit rows, in one
   transaction.

Every step can be repeated, so a retry is simply another run for the same tenant. On
failure the failed step is recorded in `audit_log` and the tenant becomes `failed`.
"""

from __future__ import annotations

import logging
import uuid
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from assay_db import tenants
from assay_tss import naming
from assay_tss.models import IsolationMode, TenantStorageProfile
from assay_tss.provision.invite import Inviter
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import StackOutputs

log = logging.getLogger(__name__)

# Recorded when the task itself died and the state machine noted the failure.
CRASH_STEP = "task"


class Step(StrEnum):
    TENANT = "tenant"
    STORAGE = "storage"
    STORAGE_ROW = "storage_row"
    INVITE_ADMIN = "invite_admin"
    ACTIVATE = "activate"


class Outcome(StrEnum):
    ACTIVE = "active"
    ALREADY_ACTIVE = "already_active"


class ActorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    user_id: uuid.UUID | None = None
    email: str | None = None
    principal: str | None = None

    def actor(self) -> tenants.Actor:
        return tenants.Actor(
            role=self.role, user_id=self.user_id, email=self.email, principal=self.principal
        )


class FirstAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    email: str


class ProvisionRequest(BaseModel):
    """The state machine's input: everything the run needs. It holds no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    name: str
    first_admin: FirstAdminModel
    actor: ActorModel
    retry: bool = False


class ProvisioningFailed(RuntimeError):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"provisioning failed at {step}: {reason}")
        self.step = step
        self.reason = reason


class Stacks(Protocol):
    def up(self, tenant_id: uuid.UUID, *, recover: bool = False) -> StackOutputs: ...


def describe(error: BaseException) -> str:
    """A short reason for the audit row: the error's type and its own message."""
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}"[: tenants.MAX_ERROR]


class Provisioner:
    def __init__(
        self, settings: ProvisionSettings, engine: Engine, stacks: Stacks, inviter: Inviter
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._stacks = stacks
        self._inviter = inviter

    # --- TenantStorageService.provision ----------------------------------------------------

    def provision(
        self, tenant_id: uuid.UUID, mode: IsolationMode, *, recover: bool = False
    ) -> TenantStorageProfile:
        """Create the tenant's storage and return where it lives."""
        if mode is not IsolationMode.DEDICATED:
            raise NotImplementedError(f"isolation mode {mode} is not built")
        s = self._settings
        outputs = self._stacks.up(tenant_id, recover=recover)
        expected = {
            "bucket": naming.bucket_name(s.name_prefix, tenant_id),
            "vector_bucket": naming.vector_bucket_name(s.name_prefix, tenant_id),
            "vector_index": naming.VECTOR_INDEX,
            "face_collection": naming.face_collection_id(s.name_prefix, tenant_id),
        }
        wrong = sorted(k for k, v in expected.items() if getattr(outputs, k) != v)
        if wrong:
            raise RuntimeError(f"tenant stack returned unexpected {', '.join(wrong)}")
        return TenantStorageProfile(
            tenant_id=tenant_id,
            mode=mode,
            bucket=outputs.bucket,
            kms_key_arn=outputs.kms_key_arn,
            role_arn=s.principals.tenant_role_arn,
            vector_bucket=outputs.vector_bucket,
            vector_index=outputs.vector_index,
            region=s.region,
        )

    # --- Steps ----------------------------------------------------------------------------

    def _ensure_tenant(self, request: ProvisionRequest) -> tenants.TenantRow:
        actor = request.actor.actor()
        with self._engine.begin() as conn:
            tenants.add_tenant(
                conn,
                tenant_id=request.tenant_id,
                name=request.name,
                first_admin=tenants.FirstAdmin(request.first_admin.name, request.first_admin.email),
                actor=actor,
            )
            tenant = tenants.lock_tenant(conn, request.tenant_id)
            if tenant is None:
                raise tenants.TenantStateError("tenant row is missing")
            if tenant.kind != tenants.KIND_BUSINESS:
                raise tenants.TenantStateError("only a business tenant has storage")
            if tenant.status == tenants.STATUS_FAILED:
                if not request.retry:
                    raise tenants.TenantStateError("tenant is failed; start a retry instead")
                tenant = tenants.begin_retry(conn, tenant, actor)
            elif tenant.status not in {tenants.STATUS_PROVISIONING, tenants.STATUS_ACTIVE}:
                raise tenants.TenantStateError(f"tenant is {tenant.status}")
            return tenant

    def _record_storage(self, profile: TenantStorageProfile) -> None:
        with self._engine.begin() as conn:
            tenants.record_storage(
                conn,
                tenant_id=profile.tenant_id,
                mode=profile.mode.value,
                bucket=profile.bucket,
                kms_key_arn=profile.kms_key_arn,
                role_arn=profile.role_arn,
                vector_bucket=profile.vector_bucket,
                vector_index=profile.vector_index,
                region=profile.region,
            )

    def _invite_admin(self, request: ProvisionRequest) -> str:
        # The tenant ID that goes into Cognito's immutable custom:tenant_id comes from the
        # tenant row, read again here, and only while that tenant is being provisioned.
        with self._engine.begin() as conn:
            tenant = tenants.get_tenant(conn, request.tenant_id)
        if (
            tenant is None
            or tenant.kind != tenants.KIND_BUSINESS
            or tenant.status != tenants.STATUS_PROVISIONING
        ):
            raise tenants.TenantStateError("tenant is not being provisioned; no invitation sent")
        result = self._inviter.invite(
            tenant_id=tenant.id,
            email=tenants.check_email(request.first_admin.email),
            name=tenants.check_name(request.first_admin.name, "admin name"),
        )
        return result.cognito_sub

    def _activate(self, request: ProvisionRequest, cognito_sub: str) -> None:
        with self._engine.begin() as conn:
            tenants.activate(
                conn,
                tenant_id=request.tenant_id,
                cognito_sub=cognito_sub,
                first_admin=tenants.FirstAdmin(request.first_admin.name, request.first_admin.email),
                actor=request.actor.actor(),
                default_models=self._settings.default_models,
            )

    # --- The run --------------------------------------------------------------------------

    def run(self, request: ProvisionRequest) -> Outcome:
        fields = {"tenant_id": str(request.tenant_id), "retry": request.retry}
        step = Step.TENANT
        try:
            log.info("step started", extra=fields | {"step": step})
            tenant = self._ensure_tenant(request)
            if tenant.status == tenants.STATUS_ACTIVE:
                log.info("tenant is already active; nothing to do", extra=fields)
                return Outcome.ALREADY_ACTIVE

            step = Step.STORAGE
            log.info("step started", extra=fields | {"step": step})
            profile = self.provision(
                request.tenant_id, IsolationMode.DEDICATED, recover=request.retry
            )

            step = Step.STORAGE_ROW
            log.info("step started", extra=fields | {"step": step})
            self._record_storage(profile)

            step = Step.INVITE_ADMIN
            log.info("step started", extra=fields | {"step": step})
            cognito_sub = self._invite_admin(request)

            step = Step.ACTIVATE
            log.info("step started", extra=fields | {"step": step})
            self._activate(request, cognito_sub)
        except Exception as exc:
            reason = describe(exc)
            log.exception("provisioning failed", extra=fields | {"step": step})
            self._record_failure(request.tenant_id, step.value, reason)
            raise ProvisioningFailed(step.value, reason) from exc
        log.info("tenant is active", extra=fields)
        return Outcome.ACTIVE

    def _record_failure(self, tenant_id: uuid.UUID, step: str, reason: str) -> bool:
        try:
            with self._engine.begin() as conn:
                return tenants.record_failure(conn, tenant_id=tenant_id, step=step, error=reason)
        except Exception:
            # The state machine runs `record_crash` next, which tries again.
            log.exception("could not record the failure", extra={"tenant_id": str(tenant_id)})
            return False

    def record_crash(self, tenant_id: uuid.UUID, error: str) -> bool:
        """Called by the state machine's catch: the task died without recording anything.

        Does nothing if the failure is already recorded.
        """
        with self._engine.begin() as conn:
            return tenants.record_failure(
                conn, tenant_id=tenant_id, step=CRASH_STEP, error=error or "task failed"
            )
