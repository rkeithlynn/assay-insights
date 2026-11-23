"""`resolve`: one tenant's storage context, with 15-minute credentials for nothing else.

Runs in the TSS Lambda, the only principal the tenant role trusts. For each request it:

1. reads the tenant's `tenant_storage` row as database role `tss` (the only reader), with
   `app.tenant_id` set, so row-level security returns that tenant's row or nothing;
2. checks the row against what provisioning must have written (the configured tenant role,
   and the names derived from the tenant ID), so a changed row cannot point at another
   tenant's resources;
3. assumes the tenant role for 900 seconds with the session tags `tenant_id`, `bucket` and
   `vector_bucket`, the caller's source identity, and a session policy that names the exact
   bucket, key, index and face collection.

Tag values and resource names come from the row, never from the caller.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from assay_tss import naming, policies
from assay_tss.models import (
    CREDENTIAL_SECONDS,
    Credentials,
    IsolationMode,
    ResolveRequest,
    StorageContext,
    StorageStatus,
    TenantStorageProfile,
    check_source_identity,
)

if TYPE_CHECKING:
    from mypy_boto3_sts import STSClient
    from sqlalchemy import Engine


class TssError(Exception):
    """A refused request. `status` is the HTTP status; `code` is safe to return."""

    status = 500
    code = "internal_error"


class CallerNotAllowed(TssError):
    status = 403
    code = "caller_not_allowed"


class SourceIdentityMismatch(TssError):
    status = 403
    code = "source_identity_mismatch"


class UnknownTenant(TssError):
    status = 404
    code = "no_storage_for_tenant"


class StorageNotActive(TssError):
    status = 409
    code = "storage_not_active"


class StorageProfileInvalid(TssError):
    status = 500
    code = "storage_profile_invalid"


@dataclass(frozen=True)
class ServiceSettings:
    region: str
    name_prefix: str
    tenant_role_arn: str
    # IAM role name of each allowed caller -> the service name it must use.
    callers: Mapping[str, str]

    @property
    def account_id(self) -> str:
        # arn:aws:iam::<account>:role/<name>
        return self.tenant_role_arn.split(":")[4]

    @classmethod
    def from_env(cls) -> ServiceSettings:
        callers = json.loads(os.environ["TSS_CALLERS"])
        if not isinstance(callers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in callers.items()
        ):
            raise ValueError("TSS_CALLERS must be a JSON object of role name to service name")
        return cls(
            region=os.environ["AWS_REGION"],
            name_prefix=naming.check_prefix(os.environ["NAME_PREFIX"]),
            tenant_role_arn=os.environ["TENANT_ROLE_ARN"],
            callers=callers,
        )


class StorageReader(Protocol):
    def profile(self, tenant_id: uuid.UUID) -> TenantStorageProfile | None: ...


class DbStorageReader:
    """Reads `tenant_storage` as database role `tss`, one tenant per transaction."""

    QUERY = (
        "SELECT tenant_id, mode, bucket, prefix, kms_key_arn, role_arn, vector_bucket, "
        "vector_index, region, version, status FROM assay.tenant_storage WHERE tenant_id = :tid"
    )

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def profile(self, tenant_id: uuid.UUID) -> TenantStorageProfile | None:
        from sqlalchemy import text

        with self._engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(
                text("SELECT pg_catalog.set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            row = conn.execute(text(self.QUERY), {"tid": tenant_id}).mappings().one_or_none()
        if row is None:
            return None
        try:
            return TenantStorageProfile.model_validate(dict(row))
        except ValueError as exc:
            raise StorageProfileInvalid(str(exc)) from None


def caller_service(user_arn: str | None, callers: Mapping[str, str]) -> str:
    """The service name of the IAM role that signed the request, from API Gateway."""
    # arn:aws:sts::<account>:assumed-role/<role name>/<session name>
    parts = (user_arn or "").split(":", 5)
    if len(parts) == 6 and parts[2] == "sts" and parts[5].startswith("assumed-role/"):
        segments = parts[5].split("/")
        if len(segments) == 3 and segments[1] in callers:
            return callers[segments[1]]
    raise CallerNotAllowed("the calling role is not a known TSS caller")


class Resolver:
    def __init__(self, settings: ServiceSettings, reader: StorageReader, sts: STSClient) -> None:
        self._settings = settings
        self._reader = reader
        self._sts = sts

    def _checked_profile(self, tenant_id: uuid.UUID) -> TenantStorageProfile:
        s = self._settings
        profile = self._reader.profile(tenant_id)
        if profile is None:
            raise UnknownTenant("no storage for this tenant")
        if profile.status is not StorageStatus.ACTIVE:
            raise StorageNotActive("this tenant's storage is not active")
        expected = {
            "tenant_id": tenant_id,
            "mode": IsolationMode.DEDICATED,
            "role_arn": s.tenant_role_arn,
            "bucket": naming.bucket_name(s.name_prefix, tenant_id),
            "vector_bucket": naming.vector_bucket_name(s.name_prefix, tenant_id),
            "vector_index": naming.VECTOR_INDEX,
            "region": s.region,
        }
        wrong = sorted(k for k, v in expected.items() if getattr(profile, k) != v)
        key_prefix = f"arn:aws:kms:{s.region}:{s.account_id}:key/"
        if not profile.kms_key_arn.startswith(key_prefix):
            wrong.append("kms_key_arn")
        if wrong:
            raise StorageProfileInvalid(f"tenant_storage row has unexpected {', '.join(wrong)}")
        return profile

    def resolve(self, request: ResolveRequest, *, service: str) -> StorageContext:
        """`service` is the caller's name, taken from its IAM role, not from the request."""
        claimed, _job = check_source_identity(request.source_identity)
        if claimed != service:
            raise SourceIdentityMismatch("source identity must start with the caller's service")
        profile = self._checked_profile(request.tenant_id)
        account_id = self._settings.account_id
        response = self._sts.assume_role(
            RoleArn=profile.role_arn,
            RoleSessionName=f"t-{request.tenant_id.hex[:8]}-{request.access.value}",
            DurationSeconds=CREDENTIAL_SECONDS,
            Tags=policies.session_tags(profile),  # type: ignore[arg-type]
            SourceIdentity=request.source_identity,
            Policy=policies.session_policy_json(request.access, profile, account_id=account_id),
        )
        creds: Any = response["Credentials"]
        return StorageContext(
            tenant_id=request.tenant_id,
            access=request.access,
            source_identity=request.source_identity,
            region=profile.region,
            bucket=profile.bucket,
            kms_key_arn=profile.kms_key_arn,
            vector_bucket=profile.vector_bucket,
            vector_index=profile.vector_index,
            face_collection=profile.face_collection,
            credentials=Credentials(
                access_key_id=creds["AccessKeyId"],
                secret_access_key=creds["SecretAccessKey"],
                session_token=creds["SessionToken"],
                expiration=creds["Expiration"],
            ),
        )
