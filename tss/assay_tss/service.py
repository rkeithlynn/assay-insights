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

`resolve_worker` (build step 6, second half) serves the pipeline's worker task, which opens
tenants' PDFs and so is trusted least. It names no tenant. It presents a document ID, an
attempt and the one-time token Step Functions gave that task; the tenant comes from the
grant's database row (`pipeline.worker_grant`, migration 0010), and only while the grant is
live: not ended, not expired, for the document's current attempt, with the document still
`processing`. The credentials reach that one document's keys and nothing else
(`policies.worker_session_policy`): in the inbox, the PDF's upload only. The grant's row also
lists the document's companion uploads, and they are left out of the policy
(`policies.worker_pdf_upload_keys`). `resolve` never serves a worker, and `resolve_worker`
serves no one else.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from botocore.exceptions import ClientError

from assay_tss import keys, naming, policies
from assay_tss.models import (
    CREDENTIAL_SECONDS,
    Credentials,
    IsolationMode,
    ResolveRequest,
    StorageAccess,
    StorageContext,
    StorageStatus,
    TenantStorageProfile,
    WorkerResolveRequest,
    check_source_identity,
    source_identity,
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


class TenantRoleRefused(TssError):
    """STS would not let the TSS assume the tenant role: a deployment fault, not a caller's."""

    status = 500
    code = "tenant_role_refused"


class AccessNotAllowed(TssError):
    status = 403
    code = "access_not_allowed"


class WorkerGrantRefused(TssError):
    """No live grant matches what the worker presented. Deliberately says no more than that:
    a wrong token, an ended or expired grant, another attempt and an unknown document all
    look the same to the caller."""

    status = 403
    code = "worker_grant_refused"


@dataclass(frozen=True)
class CallerGrant:
    """What one caller role may ask for: its service name and the accesses it may use."""

    service: str
    access: frozenset[StorageAccess]


def parse_callers(raw: str) -> dict[str, CallerGrant]:
    """`TSS_CALLERS`: {"<role name>": {"service": "<name>", "access": ["read", ...]}}."""
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("TSS_CALLERS must be a JSON object of role name to grant")
    callers: dict[str, CallerGrant] = {}
    for role, grant in document.items():
        if not (
            isinstance(role, str)
            and isinstance(grant, dict)
            and set(grant) == {"service", "access"}
            and isinstance(grant["service"], str)
            and isinstance(grant["access"], list)
            and grant["access"]
        ):
            raise ValueError(f"TSS_CALLERS entry for {role!r} must be {{service, access}}")
        callers[role] = CallerGrant(
            service=grant["service"],
            access=frozenset(StorageAccess(a) for a in grant["access"]),
        )
    return callers


@dataclass(frozen=True)
class ServiceSettings:
    region: str
    name_prefix: str
    tenant_role_arn: str
    # IAM role name of each allowed caller -> its service name and allowed accesses.
    callers: Mapping[str, CallerGrant]

    @property
    def account_id(self) -> str:
        # arn:aws:iam::<account>:role/<name>
        return self.tenant_role_arn.split(":")[4]

    @classmethod
    def from_env(cls) -> ServiceSettings:
        return cls(
            region=os.environ["AWS_REGION"],
            name_prefix=naming.check_prefix(os.environ["NAME_PREFIX"]),
            tenant_role_arn=os.environ["TENANT_ROLE_ARN"],
            callers=parse_callers(os.environ["TSS_CALLERS"]),
        )


@dataclass(frozen=True)
class WorkerGrant:
    """A live grant: the tenant its row names, and its document's clean inbox keys (the
    PDF's upload and any companion file's; the worker's policy takes the PDF's only)."""

    tenant_id: uuid.UUID
    inbox_keys: tuple[str, ...]


def token_hash(token: str) -> str:
    """As assay_db.grants.token_hash: the only form of a token the database holds."""
    return hashlib.sha256(token.encode()).hexdigest()


class StorageReader(Protocol):
    def profile(self, tenant_id: uuid.UUID) -> TenantStorageProfile | None: ...

    def worker_grant(
        self, document_id: uuid.UUID, attempt: int, token_sha256: str
    ) -> WorkerGrant | None: ...


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

    GRANT_QUERY = (
        "SELECT tenant_id, inbox_keys FROM pipeline.worker_grant(:document, :attempt, :hash)"
    )

    def worker_grant(
        self, document_id: uuid.UUID, attempt: int, token_sha256: str
    ) -> WorkerGrant | None:
        """The live grant for this document, attempt and token, or None. No tenant is set:
        the lookup answers with a tenant ID and inbox keys (IDs only) and nothing else."""
        from sqlalchemy import text

        with self._engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            row = conn.execute(
                text(self.GRANT_QUERY),
                {"document": document_id, "attempt": attempt, "hash": token_sha256},
            ).one_or_none()
        if row is None:
            return None
        return WorkerGrant(tenant_id=row.tenant_id, inbox_keys=tuple(row.inbox_keys or ()))


def caller_service(user_arn: str | None, callers: Mapping[str, CallerGrant]) -> CallerGrant:
    """The grant of the IAM role that signed the request, from API Gateway."""
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

    def resolve(self, request: ResolveRequest, *, caller: CallerGrant) -> StorageContext:
        """`caller` comes from the signing IAM role, never from the request."""
        claimed, _job = check_source_identity(request.source_identity)
        if claimed != caller.service:
            raise SourceIdentityMismatch("source identity must start with the caller's service")
        # A worker never names a tenant, and no one names worker access: `resolve_worker`.
        if StorageAccess.WORKER in caller.access or request.access is StorageAccess.WORKER:
            raise AccessNotAllowed("worker access is issued only against an attempt grant")
        if request.access not in caller.access:
            raise AccessNotAllowed(f"{caller.service} may not ask for {request.access.value}")
        profile = self._checked_profile(request.tenant_id)
        return self._issue(
            profile,
            access=request.access,
            identity=request.source_identity,
            policy=policies.session_policy_json(
                request.access, profile, account_id=self._settings.account_id
            ),
        )

    def resolve_worker(
        self, request: WorkerResolveRequest, *, caller: CallerGrant
    ) -> StorageContext:
        """Credentials for one document, for the tenant its live grant names.

        `caller` comes from the signing IAM role. The tenant comes from the grant's row.
        """
        if caller.access != frozenset({StorageAccess.WORKER}):
            raise AccessNotAllowed(f"{caller.service} is not the pipeline's worker")
        if keys.is_reserved(request.document_id):
            raise WorkerGrantRefused("the reserved isolation-test document ID is never processed")
        grant = self._reader.worker_grant(
            request.document_id, request.attempt, token_hash(request.token.get_secret_value())
        )
        if grant is None:
            raise WorkerGrantRefused("no live grant for this document, attempt and token")
        profile = self._checked_profile(grant.tenant_id)
        try:
            # The row lists every clean inbox file of the document. The worker gets the PDF's
            # upload only: a companion file's is left out (owner, 2026-11-27).
            policy = policies.worker_session_policy_json(
                profile,
                document_id=request.document_id,
                inbox_keys=policies.worker_pdf_upload_keys(request.document_id, grant.inbox_keys),
            )
        except ValueError as exc:
            # The grant's row names something that is not an upload key, or two uploads
            # under the document's own ID.
            raise StorageProfileInvalid(f"worker grant row: {exc}") from None
        return self._issue(
            profile,
            access=StorageAccess.WORKER,
            identity=source_identity(
                caller.service, f"{request.document_id.hex[:12]}-a{request.attempt}"
            ),
            policy=policy,
            document_id=request.document_id,
        )

    def _issue(
        self,
        profile: TenantStorageProfile,
        *,
        access: StorageAccess,
        identity: str,
        policy: str,
        document_id: uuid.UUID | None = None,
    ) -> StorageContext:
        try:
            response = self._sts.assume_role(
                RoleArn=profile.role_arn,
                RoleSessionName=f"t-{profile.tenant_id.hex[:8]}-{access.value}",
                DurationSeconds=CREDENTIAL_SECONDS,
                Tags=policies.session_tags(profile),  # type: ignore[arg-type]
                SourceIdentity=identity,
                Policy=policy,
            )
        except ClientError as exc:
            # STS's own words go to the log, where they name the refused action. The
            # caller gets only the code.
            reason = exc.response.get("Error", {})
            raise TenantRoleRefused(
                f"{reason.get('Code', 'unknown')}: {reason.get('Message', '')}"
            ) from exc
        creds: Any = response["Credentials"]
        return StorageContext(
            tenant_id=profile.tenant_id,
            access=access,
            source_identity=identity,
            document_id=document_id,
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
