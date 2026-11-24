"""Tenant storage and the ingest queue, as the API uses them (build step 6).

- Storage contexts come from the Tenant Storage Service, through the `execute-api` endpoint,
  always for the verified token's tenant. The API asks only for `UPLOAD` (to sign upload
  links) and `READ` (to sign view links of originals); the TSS refuses it anything else. So
  the API never reads `inbox/` or `quarantine/`, and never lists a bucket.
- Signing a link calls no AWS API.
- The ingest queue, through the `sqs` endpoint: one job per eligible PDF, sent after the
  transaction that made it eligible has committed.

Each part sits behind a small protocol, so the route tests run against fakes. Kept apart
from `assay_api.services`, which the ops Lambda image also ships.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.config import Config

from assay_schemas.pipeline import IngestJob, checksum_b64
from assay_tss.client import TssClient
from assay_tss.models import StorageAccess, StorageContext, source_identity

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sqs import SQSClient

SQS_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 3})
# Virtual-hosted, regional S3 names (`<bucket>.s3.<region>.amazonaws.com`): the form the
# portal's Content-Security-Policy admits. SigV4, which SSE-KMS objects require.
S3_SIGNING = Config(
    signature_version="s3v4",
    s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
)
# The API's TSS caller name: its source identities are `api.<job>`.
SERVICE = "api"
# A view link lasts five minutes (section 10: "short-lived view link").
VIEW_LINK_SECONDS = 300
# An upload link lasts as long as its credentials, at most 15 minutes (section 8).
UPLOAD_LINK_SECONDS = 900
SQS_BATCH = 10


class Storage(Protocol):
    def context(self, tenant_id: uuid.UUID, access: StorageAccess, job: str) -> StorageContext: ...


class TssStorage:
    def __init__(self, endpoint_url: str, *, region: str) -> None:
        self._client = TssClient(endpoint_url, region=region)

    def context(self, tenant_id: uuid.UUID, access: StorageAccess, job: str) -> StorageContext:
        return self._client.resolve(tenant_id, access, source_identity(SERVICE, job))


def _s3(ctx: StorageContext) -> S3Client:
    client: S3Client = ctx.session().client("s3", region_name=ctx.region, config=S3_SIGNING)
    return client


def _seconds_left(ctx: StorageContext, limit: int, now: dt.datetime) -> int:
    left = int((ctx.credentials.expiration - now).total_seconds())
    return max(1, min(limit, left))


@dataclass(frozen=True)
class Link:
    url: str
    expires_at: dt.datetime
    # Headers the browser must send with the request, exactly as given.
    headers: dict[str, str] = field(default_factory=dict)


def upload_link(
    ctx: StorageContext,
    key: str,
    *,
    content_type: str,
    size_bytes: int,
    sha256_hex: str,
    now: dt.datetime | None = None,
) -> Link:
    """A presigned PUT that only the registered content can satisfy (condition a).

    The signature covers `Content-Length`, `Content-Type` and `x-amz-checksum-sha256`: S3
    refuses a body of any other size or hash, and the checksum also meets Object Lock's
    integrity requirement. `ctx` must be an UPLOAD context (PUT under inbox/ only).
    """
    if ctx.access is not StorageAccess.UPLOAD:
        raise ValueError("upload links are signed with an UPLOAD context only")
    now = now or dt.datetime.now(dt.UTC)
    seconds = _seconds_left(ctx, UPLOAD_LINK_SECONDS, now)
    checksum = checksum_b64(sha256_hex)
    url = _s3(ctx).generate_presigned_url(
        "put_object",
        Params={
            "Bucket": ctx.bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
            "ChecksumSHA256": checksum,
        },
        ExpiresIn=seconds,
        HttpMethod="PUT",
    )
    return Link(
        url=url,
        expires_at=now + dt.timedelta(seconds=seconds),
        headers={"Content-Type": content_type, "x-amz-checksum-sha256": checksum},
    )


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_file_name(name: str) -> str:
    """A name for Content-Disposition: plain ASCII, no quotes, ending in .pdf."""
    cleaned = _UNSAFE_NAME.sub("_", name).strip(" .")[:100] or "document"
    return cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned}.pdf"


def view_link(
    ctx: StorageContext, key: str, *, file_name: str, now: dt.datetime | None = None
) -> Link:
    """A presigned GET of an original PDF for five minutes, always served as a PDF.

    `ctx` must be a READ context, which reaches originals/, derived/ and chat/ only.
    """
    if ctx.access is not StorageAccess.READ:
        raise ValueError("view links are signed with a READ context only")
    now = now or dt.datetime.now(dt.UTC)
    seconds = _seconds_left(ctx, VIEW_LINK_SECONDS, now)
    url = _s3(ctx).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": ctx.bucket,
            "Key": key,
            "ResponseContentType": "application/pdf",
            "ResponseContentDisposition": f'inline; filename="{safe_file_name(file_name)}"',
            "ResponseCacheControl": "private, no-store",
        },
        ExpiresIn=seconds,
        HttpMethod="GET",
    )
    return Link(url=url, expires_at=now + dt.timedelta(seconds=seconds))


class IngestQueue(Protocol):
    def send(self, jobs: Sequence[IngestJob]) -> None: ...


class SqsIngestQueue:
    """Sends ingest jobs. A failure is raised; the sweeper queues the document again."""

    def __init__(self, queue_url: str, *, region: str, client: SQSClient | None = None) -> None:
        self._url = queue_url
        self._client: SQSClient = client or boto3.client(
            "sqs", region_name=region, config=SQS_CONFIG
        )

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
