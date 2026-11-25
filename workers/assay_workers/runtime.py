"""What every pipeline function needs: the database, tenant storage, the ingest queue.

- **Database.** Every function signs in as `app_tenant` (section 7). A tenant's work runs in
  `tenant_tx`, which sets `app.tenant_id` first, so row-level security shows that tenant and
  nothing else. Only the two lookups in schema `pipeline` (migration 0008) run before a
  tenant is known (`lookup_tx`), and they answer with tenant IDs only.
- **Storage.** A PIPELINE context from the TSS for one tenant, and `Objects`, the only S3
  calls the pipeline makes: by key (built through the context) and by version. There is no
  listing call here at all: the pipeline works from database rows and scan events.
- **Queue.** Ingest jobs, one per eligible PDF, sent after the transaction that made them
  eligible has committed.

Each part sits behind a protocol, so the handlers' tests use a real PostgreSQL with fake
storage and a fake queue.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import Connection, Engine, text

from assay_schemas.pipeline import IngestJob
from assay_tss.client import TssClient
from assay_tss.models import StorageAccess, StorageContext, source_identity

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sqs import SQSClient

DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT
AWS_CONFIG = Config(
    connect_timeout=5, read_timeout=60, retries={"max_attempts": 5, "mode": "adaptive"}
)
SQS_BATCH = 10
CHUNK = 1024 * 1024
# S3 refuses a missing key with 403, not 404, to a caller that may not list the bucket.
MISSING = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied"})


# --- Database ---------------------------------------------------------------------------


@contextmanager
def tenant_tx(engine: Engine, tenant_id: uuid.UUID) -> Iterator[Connection]:
    """One transaction as app_tenant, scoped to `tenant_id`. Commits on success."""
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_catalog.set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield conn


@contextmanager
def lookup_tx(engine: Engine) -> Iterator[Connection]:
    """A read-only transaction with no tenant: only the `pipeline` lookups answer in it."""
    with engine.begin() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


# --- Storage ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectInfo:
    size_bytes: int
    version_id: str | None
    # Base64 SHA-256 as S3 stores it (the upload link signs it), if the object has one.
    checksum_sha256: str | None


class Objects(Protocol):
    """One tenant's bucket, by key and version. No listing."""

    def head(self, key: str, version_id: str | None = None) -> ObjectInfo | None: ...

    def read(self, key: str, version_id: str, max_bytes: int) -> bytes: ...

    def download(self, key: str, version_id: str, path: Path) -> str: ...

    def copy(
        self, *, source_key: str, source_version: str, target_key: str, content_type: str
    ) -> str | None: ...

    def delete(self, key: str) -> None: ...


@dataclass(frozen=True)
class TenantStorage:
    """A PIPELINE context (for its key builders) and the objects it reaches."""

    context: StorageContext
    objects: Objects


class StorageSource(Protocol):
    def pipeline(self, tenant_id: uuid.UUID, service: str, job: str) -> TenantStorage: ...


class S3Objects:
    def __init__(self, ctx: StorageContext) -> None:
        # WORKER: the worker task's context, for one document's keys (build step 6, second
        # half). It may read and copy by key and version; it can neither head a key it was
        # not given nor delete anything.
        if ctx.access not in (StorageAccess.PIPELINE, StorageAccess.WORKER):
            raise ValueError("the pipeline works with PIPELINE and WORKER contexts only")
        self._bucket = ctx.bucket
        self._s3: S3Client = ctx.session().client("s3", region_name=ctx.region, config=AWS_CONFIG)

    def head(self, key: str, version_id: str | None = None) -> ObjectInfo | None:
        args: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version_id is not None:
            args["VersionId"] = version_id
        try:
            head = self._s3.head_object(**args)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in MISSING:
                return None
            raise
        return ObjectInfo(
            size_bytes=int(head["ContentLength"]),
            version_id=head.get("VersionId"),
            checksum_sha256=head.get("ChecksumSHA256"),
        )

    def read(self, key: str, version_id: str, max_bytes: int) -> bytes:
        """At most `max_bytes` + 1 bytes, so the caller can tell a file is too large."""
        response = self._s3.get_object(
            Bucket=self._bucket, Key=key, VersionId=version_id, Range=f"bytes=0-{max_bytes}"
        )
        data: bytes = response["Body"].read(max_bytes + 1)
        return data

    def download(self, key: str, version_id: str, path: Path) -> str:
        """Write that exact version to `path`; return its hex SHA-256."""
        response = self._s3.get_object(Bucket=self._bucket, Key=key, VersionId=version_id)
        digest = hashlib.sha256()
        with path.open("wb") as out:
            for chunk in response["Body"].iter_chunks(CHUNK):
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest()

    def copy(
        self, *, source_key: str, source_version: str, target_key: str, content_type: str
    ) -> str | None:
        """Copy one version, with no tags. Returns the new object's version ID.

        GuardDuty tags every object it scans, and a copy takes the source's tags by default
        (`TaggingDirective` COPY). Copying them needs object-tagging permissions that no
        tenant session has (`s3:GetObjectVersionTagging`, `s3:PutObjectTagging`), so S3
        refused every copy of a scanned upload (build step 6, first browser upload,
        2026-11-24). REPLACE with no tag set reads no tags and writes none. The scan
        result is kept in the database, not in a tag.
        """
        response = self._s3.copy_object(
            Bucket=self._bucket,
            Key=target_key,
            CopySource={"Bucket": self._bucket, "Key": source_key, "VersionId": source_version},
            ContentType=content_type,
            MetadataDirective="REPLACE",
            TaggingDirective="REPLACE",
            ChecksumAlgorithm="SHA256",
        )
        return response.get("VersionId")

    def delete(self, key: str) -> None:
        """A delete marker only: the role never deletes a version (Object Lock keeps it)."""
        self._s3.delete_object(Bucket=self._bucket, Key=key)


class TssStorageSource:
    def __init__(self, endpoint_url: str, *, region: str) -> None:
        self._client = TssClient(endpoint_url, region=region)

    def pipeline(self, tenant_id: uuid.UUID, service: str, job: str) -> TenantStorage:
        ctx = self._client.resolve(tenant_id, StorageAccess.PIPELINE, source_identity(service, job))
        return TenantStorage(context=ctx, objects=S3Objects(ctx))


# --- Queue ------------------------------------------------------------------------------


class IngestQueue(Protocol):
    def send(self, jobs: Sequence[IngestJob]) -> None: ...


class SqsIngestQueue:
    def __init__(self, queue_url: str, *, region: str) -> None:
        self._url = queue_url
        self._client: SQSClient = boto3.client("sqs", region_name=region, config=AWS_CONFIG)

    def send(self, jobs: Sequence[IngestJob]) -> None:
        for start in range(0, len(jobs), SQS_BATCH):
            chunk = jobs[start : start + SQS_BATCH]
            response = self._client.send_message_batch(
                QueueUrl=self._url,
                Entries=[
                    {"Id": str(i), "MessageBody": job.model_dump_json()}
                    for i, job in enumerate(chunk)
                ],
            )
            if response.get("Failed"):
                raise RuntimeError(f"{len(response['Failed'])} ingest jobs were not queued")


def jobs_for(tenant_id: uuid.UUID, document_ids: Sequence[uuid.UUID]) -> list[IngestJob]:
    return [IngestJob(tenant_id=tenant_id, document_id=d) for d in document_ids]


# --- Security notices -------------------------------------------------------------------


class Notices(Protocol):
    """The security-notice topic (owner decision 4, second half): apart from the alarms."""

    def publish(self, *, subject: str, message: str) -> None: ...


class SnsNotices:
    def __init__(self, topic_arn: str, *, region: str) -> None:
        self._arn = topic_arn
        self._client: Any = boto3.client("sns", region_name=region, config=AWS_CONFIG)

    def publish(self, *, subject: str, message: str) -> None:
        self._client.publish(TopicArn=self._arn, Subject=subject, Message=message)


# --- Settings ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    region: str
    name_prefix: str
    tss_endpoint_url: str
    ingest_queue_url: str
    state_machine_arn: str | None = None
    security_topic_arn: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            region=env["AWS_REGION"],
            name_prefix=env["NAME_PREFIX"],
            tss_endpoint_url=env.get("TSS_ENDPOINT_URL", ""),
            ingest_queue_url=env.get("INGEST_QUEUE_URL", ""),
            state_machine_arn=env.get("STATE_MACHINE_ARN") or None,
            security_topic_arn=env.get("SECURITY_TOPIC_ARN") or None,
        )


def app_tenant_engine() -> Engine:
    from assay_db.connect import DbTarget, iam_engine

    return iam_engine(DbTarget.from_env(), DB_USER, application_name="assay-pipeline", pool_size=1)
