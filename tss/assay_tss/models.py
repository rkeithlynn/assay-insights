"""The Tenant Storage Service's types (BUILD_SPEC.md section 6).

`StorageContext` is what a caller gets from `resolve`: 15-minute credentials for one
tenant's bucket, key, vector index and face collection, plus the only functions that build
object keys. Nothing else in the codebase builds an S3 key or picks a KMS key.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from enum import StrEnum
from typing import Any, Protocol

import boto3
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from assay_tss import keys

# Credentials always last 15 minutes, the shortest AWS STS allows.
CREDENTIAL_SECONDS = 900

# AWS STS: a source identity is 2 to 64 of these characters.
_SOURCE_IDENTITY = re.compile(r"^[A-Za-z0-9_+=,.@-]{2,64}$")
_SERVICE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")


class IsolationMode(StrEnum):
    """How a tenant's data is kept apart. Pooled storage is out of scope for now."""

    DEDICATED = "dedicated"


class StorageStatus(StrEnum):
    ACTIVE = "active"
    OFFBOARDED = "offboarded"


class StorageAccess(StrEnum):
    """What the credentials may do. Each maps to one session policy (assay_tss.policies).

    No access may list a bucket: callers work from database rows and scan events, never from
    a listing. Only PIPELINE may read `inbox/` and `quarantine/` (unscanned and infected
    files), current or noncurrent versions.
    """

    # Read objects under originals/, derived/ and chat/, and query vectors.
    READ = "read"
    # Write new objects under inbox/ only. For presigned upload links.
    UPLOAD = "upload"
    # The ingestion pipeline: read and write objects and vectors, use the face collection.
    PIPELINE = "pipeline"
    # The pipeline's worker task, for one document's attempt (build step 6, second half):
    # that document's own keys and nothing else. Never asked for by name: issued only by
    # `/resolve-worker`, against a live attempt grant, for the tenant the grant's row names.
    WORKER = "worker"


# What a caller may ask `resolve` for by name. WORKER is not among them.
REQUESTABLE_ACCESS = (StorageAccess.READ, StorageAccess.UPLOAD, StorageAccess.PIPELINE)


def source_identity(service: str, job: str) -> str:
    """`<service>.<job>`: names the calling service and the job it is doing."""
    value = f"{service}.{job}"
    check_source_identity(value)
    return value


def check_source_identity(value: str) -> tuple[str, str]:
    """Return (service, job), or raise ValueError."""
    service, dot, job = value.partition(".")
    if not _SOURCE_IDENTITY.match(value) or not dot or not job or not _SERVICE.match(service):
        raise ValueError(
            "source identity must be <service>.<job>, 2 to 64 characters from A-Z a-z 0-9 _+=,.@-"
        )
    return service, job


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    access: StorageAccess
    source_identity: str = Field(min_length=2, max_length=64)

    @field_validator("source_identity")
    @classmethod
    def _valid_source_identity(cls, value: str) -> str:
        check_source_identity(value)
        return value


# A worker's token: `secrets.token_urlsafe` text. Never a colon (assay_db.grants).
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class WorkerResolveRequest(BaseModel):
    """What the worker task presents to `/resolve-worker`. It names no tenant: the TSS takes
    the tenant from the grant's row. The token is never logged or echoed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: uuid.UUID
    attempt: int = Field(ge=1, le=100_000)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def _valid_token(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN.match(value.get_secret_value()):
            raise ValueError("not a worker token")
        return value


class Credentials(BaseModel):
    """Temporary credentials. The secret parts never appear in a repr or a log line."""

    model_config = ConfigDict(frozen=True)

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr
    expiration: dt.datetime


class TenantStorageProfile(BaseModel):
    """Where a tenant's data lives: the `tenant_storage` row."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    mode: IsolationMode
    bucket: str
    prefix: str | None = None
    kms_key_arn: str
    role_arn: str
    vector_bucket: str
    vector_index: str
    region: str
    version: int = 1
    status: StorageStatus = StorageStatus.ACTIVE

    @property
    def face_collection(self) -> str:
        """The Rekognition collection is named after the bucket."""
        return self.bucket

    def vector_index_arn(self, account_id: str) -> str:
        return (
            f"arn:aws:s3vectors:{self.region}:{account_id}:bucket/{self.vector_bucket}"
            f"/index/{self.vector_index}"
        )

    def face_collection_arn(self, account_id: str) -> str:
        return f"arn:aws:rekognition:{self.region}:{account_id}:collection/{self.face_collection}"


class StorageContext(BaseModel):
    """One tenant's storage, with credentials that reach nothing else."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    access: StorageAccess
    source_identity: str
    region: str
    bucket: str
    kms_key_arn: str
    vector_bucket: str
    vector_index: str
    face_collection: str
    credentials: Credentials
    # A WORKER context only: the one document its credentials reach.
    document_id: uuid.UUID | None = None

    # --- AWS clients ---------------------------------------------------------------------

    def session(self) -> boto3.Session:
        creds = self.credentials
        return boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key.get_secret_value(),
            aws_session_token=creds.session_token.get_secret_value(),
            region_name=self.region,
        )

    def expired(self, now: dt.datetime | None = None, margin_seconds: int = 60) -> bool:
        now = now or dt.datetime.now(dt.UTC)
        return now + dt.timedelta(seconds=margin_seconds) >= self.credentials.expiration

    # --- Object keys: the only way to build one --------------------------------------------

    def inbox_key(self, batch_id: uuid.UUID, file_id: uuid.UUID) -> str:
        return keys.inbox_key(batch_id, file_id)

    def inbox_batch_prefix(self, batch_id: uuid.UUID) -> str:
        return keys.inbox_batch_prefix(batch_id)

    def original_key(self, doc_id: uuid.UUID) -> str:
        return keys.original_key(doc_id)

    def companion_key(self, doc_id: uuid.UUID) -> str:
        return keys.companion_key(doc_id)

    def attachment_key(self, doc_id: uuid.UUID, attachment_id: uuid.UUID, extension: str) -> str:
        return keys.attachment_key(doc_id, attachment_id, extension)

    def face_key(self, face_id: uuid.UUID) -> str:
        return keys.face_key(face_id)

    def quarantine_key(self, doc_id: uuid.UUID) -> str:
        return keys.quarantine_key(doc_id)

    def quarantine_companion_key(self, file_id: uuid.UUID) -> str:
        return keys.quarantine_companion_key(file_id)

    def text_key(self, doc_id: uuid.UUID) -> str:
        return keys.text_key(doc_id)

    def fields_key(self, doc_id: uuid.UUID) -> str:
        return keys.fields_key(doc_id)

    def chat_key(self, conversation_id: uuid.UUID, message_id: uuid.UUID) -> str:
        return keys.chat_key(conversation_id, message_id)

    # --- Wire format (the private API's response body) --------------------------------------

    def to_wire(self) -> dict[str, Any]:
        """JSON for the private API's response. The only place the secrets are written out."""
        body = self.model_dump(mode="json", exclude={"credentials"})
        creds = self.credentials
        body["credentials"] = {
            "access_key_id": creds.access_key_id,
            "secret_access_key": creds.secret_access_key.get_secret_value(),
            "session_token": creds.session_token.get_secret_value(),
            "expiration": creds.expiration.isoformat(),
        }
        return body

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> StorageContext:
        return cls.model_validate(body)


class TenantStorageService(Protocol):
    """Section 6's interface.

    `resolve` is served by the Lambda behind the private API (`assay_tss.client.TssClient`
    calls it). `provision` is done by the provisioning task (`assay_tss.provision`), the only
    code that creates tenant storage. `offboard` is not built in the first deployment.
    """

    def resolve(
        self, tenant_id: uuid.UUID, access: StorageAccess, source_identity: str
    ) -> StorageContext: ...

    def provision(self, tenant_id: uuid.UUID, mode: IsolationMode) -> TenantStorageProfile: ...

    def offboard(self, tenant_id: uuid.UUID) -> None: ...
