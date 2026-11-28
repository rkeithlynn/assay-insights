"""A database and fakes for the build step 6 tests (batches, scans, the pipeline, history).

One throwaway database per test module, migrated to head, with three business tenants
(A and B active, S suspended), the platform tenant, and their users. Engines sign in as the
real roles (`app_tenant`, `platform_reader`, `platform_writer`), so row-level security and
the grants are exactly those on Aurora.

The fakes stand in for AWS:
- `FakeBuckets` keeps every version of every object, per bucket, with S3's checksum; its
  `Objects` only reads by key and version (there is no listing call to fake).
- `FakeStorage` hands out storage contexts (offline credentials, so links can be signed) and,
  for the pipeline, those objects.
- `FakeQueue` records ingest jobs.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import itertools
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, create_engine

from assay_db.roles import APP_TENANT, APP_WORKER, PLATFORM_READER, PLATFORM_WRITER
from assay_schemas.pipeline import IngestJob
from assay_tss import naming
from assay_tss.models import Credentials, StorageAccess, StorageContext
from assay_workers.runtime import ObjectInfo, TenantStorage
from tests.unit.pgtest import (
    _bootstrap_as_master,
    _conninfo,
    _create_database,
    _drop_database,
    _migrate,
)

PREFIX = "assay-test"
REGION = "us-east-1"
ACCOUNT = "000000000000"

# sub -> (tenant key, role)
USERS = {
    "m-admin": ("platform", "master-admin"),
    "m-user": ("platform", "master-user"),
    "a-admin": ("a", "tenant-admin"),
    "a-admin2": ("a", "tenant-admin"),
    "a-user": ("a", "tenant-user"),
    "b-admin": ("b", "tenant-admin"),
    "b-user": ("b", "tenant-user"),
    "s-admin": ("s", "tenant-admin"),
}


def engine_for(name: str, role: str) -> Engine:
    return create_engine(
        "postgresql+psycopg://", creator=lambda: psycopg.connect(_conninfo(name, role))
    )


@dataclass
class Db:
    name: str
    url: str
    tenants: dict[str, uuid.UUID]
    users: dict[str, uuid.UUID]
    app_tenant: Engine
    platform_reader: Engine
    platform_writer: Engine

    def row(self, query: str, *args: object) -> tuple[Any, ...] | None:
        with psycopg.connect(self.url) as conn:
            return conn.execute(query, args).fetchone()

    def rows(self, query: str, *args: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(query, args).fetchall()

    def execute(self, query: str, *args: object) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(query, args)

    def dispose(self) -> None:
        for engine in (self.app_tenant, self.platform_reader, self.platform_writer):
            engine.dispose()

    def worker_connection(self) -> psycopg.Connection:
        """A fresh session as app_worker, as the worker task opens one (unbound)."""
        return psycopg.connect(_conninfo(self.name, APP_WORKER))


@pytest.fixture(scope="module")
def step6_db(db: Any) -> Iterator[Db]:
    """`db` (the session fixture) resets the server and creates the roles first."""
    name = f"assay_test_{uuid.uuid4().hex[:12]}"
    _create_database(name)
    try:
        _bootstrap_as_master(name)
        _migrate(name)
        url = _conninfo(name)
        tenants = {k: uuid.uuid4() for k in ("platform", "a", "b", "s")}
        users: dict[str, uuid.UUID] = {}
        with psycopg.connect(url) as conn:
            for key, tenant_name, kind, status in (
                ("platform", "Assay Insights, LLC", "platform", "active"),
                ("a", "Tenant A", "business", "active"),
                ("b", "Tenant B", "business", "active"),
                ("s", "Tenant S", "business", "suspended"),
            ):
                conn.execute(
                    "INSERT INTO assay.tenants (id, name, kind, status) VALUES (%s, %s, %s, %s)",
                    (tenants[key], tenant_name, kind, status),
                )
            for sub, (tenant, role) in USERS.items():
                users[sub] = uuid.uuid4()
                conn.execute(
                    "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, "
                    "status) VALUES (%s, %s, %s, %s, %s, %s, 'active')",
                    (
                        users[sub],
                        tenants[tenant],
                        sub,
                        f"{sub}@example.test",
                        f"Name {sub}",
                        role,
                    ),
                )
        world = Db(
            name=name,
            url=url,
            tenants=tenants,
            users=users,
            app_tenant=engine_for(name, APP_TENANT),
            platform_reader=engine_for(name, PLATFORM_READER),
            platform_writer=engine_for(name, PLATFORM_WRITER),
        )
        try:
            yield world
        finally:
            world.dispose()
    finally:
        _drop_database(name)


# --- Fakes -----------------------------------------------------------------------------


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def checksum(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


@dataclass
class Version:
    version_id: str
    body: bytes
    content_type: str
    deleted: bool = False  # a delete marker


@dataclass
class FakeBuckets:
    """Every version of every object, per bucket. Records what the pipeline did."""

    objects: dict[tuple[str, str], list[Version]] = field(default_factory=dict)
    copies: list[tuple[str, str, str, str]] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)
    uploads: list[tuple[str, str]] = field(default_factory=list)
    reads: list[tuple[str, str, str | None]] = field(default_factory=list)
    _counter: Iterator[int] = field(default_factory=itertools.count)

    def put(self, bucket: str, key: str, body: bytes, content_type: str = "x") -> str:
        version = f"v{next(self._counter)}"
        self.objects.setdefault((bucket, key), []).append(Version(version, body, content_type))
        return version

    def current(self, bucket: str, key: str) -> Version | None:
        versions = self.objects.get((bucket, key)) or []
        return versions[-1] if versions and not versions[-1].deleted else None

    def get(self, bucket: str, key: str, version_id: str | None) -> Version | None:
        if version_id is None:
            return self.current(bucket, key)
        for v in self.objects.get((bucket, key)) or []:
            if v.version_id == version_id and not v.deleted:
                return v
        return None


class FakeObjects:
    def __init__(self, buckets: FakeBuckets, bucket: str) -> None:
        self._b = buckets
        self._bucket = bucket

    def head(self, key: str, version_id: str | None = None) -> ObjectInfo | None:
        v = self._b.get(self._bucket, key, version_id)
        if v is None:
            return None
        return ObjectInfo(
            size_bytes=len(v.body), version_id=v.version_id, checksum_sha256=checksum(v.body)
        )

    def read(self, key: str, version_id: str, max_bytes: int) -> bytes:
        self._b.reads.append((self._bucket, key, version_id))
        v = self._b.get(self._bucket, key, version_id)
        assert v is not None
        return v.body[: max_bytes + 1]

    def download(self, key: str, version_id: str, path: Path) -> str:
        self._b.reads.append((self._bucket, key, version_id))
        v = self._b.get(self._bucket, key, version_id)
        assert v is not None, (key, version_id)
        path.write_bytes(v.body)
        return sha256_hex(v.body)

    def copy(
        self, *, source_key: str, source_version: str, target_key: str, content_type: str
    ) -> str | None:
        v = self._b.get(self._bucket, source_key, source_version)
        assert v is not None
        self._b.copies.append((self._bucket, source_key, source_version, target_key))
        return self._b.put(self._bucket, target_key, v.body, content_type)

    def upload(self, key: str, path: Path, content_type: str) -> str | None:
        self._b.uploads.append((self._bucket, key))
        return self._b.put(self._bucket, key, path.read_bytes(), content_type)

    def delete(self, key: str) -> None:
        self._b.deletes.append((self._bucket, key))
        version = f"d{next(self._b._counter)}"
        self._b.objects.setdefault((self._bucket, key), []).append(
            Version(version, b"", "", deleted=True)
        )


def context(tenant_id: uuid.UUID, access: StorageAccess) -> StorageContext:
    bucket = naming.bucket_name(PREFIX, tenant_id)
    return StorageContext(
        tenant_id=tenant_id,
        access=access,
        source_identity="test.job",
        region=REGION,
        bucket=bucket,
        kms_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant_id}",
        vector_bucket=naming.vector_bucket_name(PREFIX, tenant_id),
        vector_index=naming.VECTOR_INDEX,
        face_collection=naming.face_collection_id(PREFIX, tenant_id),
        credentials=Credentials(
            access_key_id="ASIATESTEXAMPLE",
            secret_access_key="test-secret",  # type: ignore[arg-type]
            session_token="test-token",  # type: ignore[arg-type]
            expiration=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15),
        ),
    )


@dataclass
class FakeStorage:
    """The API's Storage and the pipeline's StorageSource."""

    buckets: FakeBuckets = field(default_factory=FakeBuckets)
    asked: list[tuple[uuid.UUID, StorageAccess, str]] = field(default_factory=list)

    def context(self, tenant_id: uuid.UUID, access: StorageAccess, job: str) -> StorageContext:
        self.asked.append((tenant_id, access, job))
        return context(tenant_id, access)

    def pipeline(self, tenant_id: uuid.UUID, service: str, job: str) -> TenantStorage:
        self.asked.append((tenant_id, StorageAccess.PIPELINE, f"{service}.{job}"))
        ctx = context(tenant_id, StorageAccess.PIPELINE)
        return TenantStorage(context=ctx, objects=FakeObjects(self.buckets, ctx.bucket))

    def worker(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> TenantStorage:
        """What `/resolve-worker` gives the worker task: a context for one document."""
        self.asked.append((tenant_id, StorageAccess.WORKER, f"worker.{document_id.hex[:12]}"))
        ctx = context(tenant_id, StorageAccess.WORKER).model_copy(
            update={"document_id": document_id}
        )
        return TenantStorage(context=ctx, objects=FakeObjects(self.buckets, ctx.bucket))


@dataclass
class FakeQueue:
    sent: list[IngestJob] = field(default_factory=list)
    fail: bool = False

    def send(self, jobs: Sequence[IngestJob]) -> None:
        if self.fail:
            raise RuntimeError("queue down")
        self.sent.extend(jobs)


def scan_event(
    bucket: str,
    key: str,
    version_id: str | None,
    result: str = "NO_THREATS_FOUND",
    reasons: Sequence[str] = (),
    threat: str | None = None,
) -> dict[str, Any]:
    """A GuardDuty Malware Protection Object Scan Result event, as AWS documents it."""
    status = "COMPLETED" if result in ("NO_THREATS_FOUND", "THREATS_FOUND") else "SKIPPED"
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "GuardDuty Malware Protection Object Scan Result",
        "source": "aws.guardduty",
        "account": ACCOUNT,
        "region": REGION,
        "detail": {
            "schemaVersion": "1.0",
            "scanStatus": status,
            "resourceType": "S3_OBJECT",
            "s3ObjectDetails": {
                "bucketName": bucket,
                "objectKey": key,
                "eTag": "etag",
                "versionId": version_id,
                "s3Throttled": False,
            },
            "scanResultDetails": {
                "scanResultStatus": result,
                "threats": None if threat is None else [{"name": threat}],
                "statusReasons": list(reasons) or None,
            },
        },
    }


def minimal_pdf(pages: int = 2, text: str = "") -> bytes:
    """A real PDF, made by PDFium (no third-party generator)."""
    import io

    import pypdfium2

    pdf = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(612, 792)
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()
    return buffer.getvalue() + (f"\n%{text}\n".encode() if text else b"")
