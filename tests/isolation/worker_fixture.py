"""The worker probe's fixture: documents, objects and grants in the two test tenants.

The worker task's probe (`assay_workers.worker.probe`, run by `scripts/run_isolation.py` as
the worker's own role) needs documents to be granted and refused. This Lambda prepares them,
because only it signs in as `app_tenant` and holds pipeline storage contexts for the test
tenants:

- three fixture documents with fixed IDs (derived from the tenant ID, so every run reuses
  them): two in tenant A, one in tenant B. Each has a `documents` row (`processing` during a
  run, `discarded` after it, so it shows in no history view by default), one clean PDF row
  in `batch_files`, and a `start` event, so the sweeper never queues it and the pipeline
  never starts an execution for it;
- one small object at each document's `originals/` key and (stage 2) at its
  `derived/text/` key, written once with the tenant's pipeline credentials and kept by Object
  Lock. A refused read proves something only for an object that exists;
- grants in every state, made as the pipeline's `claim` step makes them: a live one for the
  first document of tenant A (the probe's own), an ended one and then a live one for the
  second (the live one is ended mid-run, on request), and an expired one for tenant B's.

The tokens go back to `scripts/run_isolation.py`, which passes them to the probe task. They
are grants on test documents only, and `cleanup` ends them.

Unlike the reserved document ID, these documents do have rows. They are never processed all
the same: nothing queues them, and their PDF row names an inbox object that does not exist.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import Connection, Engine, text

from assay_db import grants
from assay_db.connect import DbTarget, iam_engine
from assay_tss.client import TssClient
from assay_tss.models import StorageAccess, StorageContext, source_identity

DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT
NAMESPACE = uuid.UUID("6f1d2c3e-7a4b-4c5d-9e8f-0a1b2c3d4e5f")
FILE_NAME = "isolation-worker-fixture.pdf"
BODY = b"isolation-test-fixture: the worker probe's document, never processed\n"
MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "403", "AccessDenied"})
# No real cap applies to the fixture's own claims.
NO_CAP = 1000
GRANT_TTL = dt.timedelta(minutes=30)


def batch_id(tenant_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{tenant_id}:batch")


def document_id(tenant_id: uuid.UUID, number: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{tenant_id}:document-{number}")


def _tenant(conn: Connection, tenant_id: uuid.UUID) -> None:
    conn.execute(
        text("SELECT pg_catalog.set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def ensure_document(conn: Connection, tenant_id: uuid.UUID, number: int) -> uuid.UUID:
    """The fixture document's rows, `processing` at attempt 1, with a `start` event."""
    batch, document = batch_id(tenant_id), document_id(tenant_id, number)
    user = conn.execute(text("SELECT id FROM assay.users ORDER BY id LIMIT 1")).scalar_one()
    conn.execute(
        text(
            "INSERT INTO assay.batches (id, tenant_id, name, status, opened_by, closed_at) "
            "VALUES (:id, :tenant, 'Isolation tests: worker fixture', 'closed', :user, now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": batch, "tenant": tenant_id, "user": user},
    )
    conn.execute(
        text(
            "INSERT INTO assay.documents (id, tenant_id, batch_id, uploaded_by, file_name, "
            "status, queued_at) VALUES (:id, :tenant, :batch, :user, :name, 'processing', now()) "
            "ON CONFLICT (id) DO UPDATE SET status = 'processing', attempt = 1, "
            "error_code = NULL, completed_at = NULL, queued_at = now()"
        ),
        {"id": document, "tenant": tenant_id, "batch": batch, "user": user, "name": FILE_NAME},
    )
    conn.execute(
        text(
            "INSERT INTO assay.batch_files (id, tenant_id, batch_id, kind, file_name, "
            "base_name, size_bytes, sha256, s3_key, content_type, status, version_id, "
            "document_id, registered_by) VALUES (:id, :tenant, :batch, 'pdf', :name, :base, "
            ":size, :sha, :key, 'application/pdf', 'clean', 'isolation-fixture', :id, :user) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": document,
            "tenant": tenant_id,
            "batch": batch,
            "user": user,
            "name": FILE_NAME,
            "base": f"isolation-worker-fixture-{number}",
            "size": len(BODY),
            "sha": "0" * 64,
            "key": f"inbox/{batch}/{document}",
        },
    )
    # With a `start` event the sweeper never queues it and the starter never starts it.
    conn.execute(
        text(
            "INSERT INTO assay.document_events (tenant_id, document_id, step, status) "
            "SELECT :tenant, :id, 'start', 'started' WHERE NOT EXISTS ("
            "SELECT 1 FROM assay.document_events WHERE document_id = :id AND step = 'start')"
        ),
        {"tenant": tenant_id, "id": document},
    )
    return document


def ensure_object(context: StorageContext, document: uuid.UUID) -> None:
    """One small object at the document's `originals/` key, written once."""
    api: Any = context.session().client("s3", region_name=context.region)
    key = context.original_key(document)
    try:
        api.head_object(Bucket=context.bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in MISSING_CODES:
            raise
        api.put_object(Bucket=context.bucket, Key=key, Body=BODY, ContentType="application/pdf")


TEXT_BODY = (
    b'{"isolation_test_fixture": "the worker probe reads this; it is no document\'s text"}\n'
)


def ensure_text_object(context: StorageContext, document: uuid.UUID) -> None:
    """One small object at the document's `derived/text/` key, written once (stage 2): the
    probe reads its own document's and must be refused every other's."""
    api: Any = context.session().client("s3", region_name=context.region)
    key = context.text_key(document)
    try:
        api.head_object(Bucket=context.bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in MISSING_CODES:
            raise
        api.put_object(
            Bucket=context.bucket, Key=key, Body=TEXT_BODY, ContentType="application/json"
        )


def ensure_objects(context: StorageContext, document: uuid.UUID) -> None:
    ensure_object(context, document)
    ensure_text_object(context, document)


def _claim(conn: Connection, tenant_id: uuid.UUID, document: uuid.UUID) -> dict[str, Any]:
    token = grants.claim(
        conn,
        tenant_id=tenant_id,
        document_id=document,
        attempt=1,
        max_per_tenant=NO_CAP,
        ttl=GRANT_TTL,
    )
    assert token is not None  # nosec B101  (no cap applies)
    return {"document_id": str(document), "attempt": 1, "token": token}


def prepare(
    engine: Engine,
    tss: TssClient,
    service: str,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
    *,
    write: Callable[[StorageContext, uuid.UUID], None] = ensure_objects,
) -> dict[str, Any]:
    """Documents, objects and grants; returns the probe's cases."""
    with engine.begin() as conn:
        _tenant(conn, tenant_a)
        own_document = ensure_document(conn, tenant_a, 1)
        sibling = ensure_document(conn, tenant_a, 2)
        own = _claim(conn, tenant_a, own_document)
        ended = _claim(conn, tenant_a, sibling)
        grants.end(conn, document_id=sibling)
        to_end = _claim(conn, tenant_a, sibling)
    with engine.begin() as conn:
        _tenant(conn, tenant_b)
        other = ensure_document(conn, tenant_b, 1)
        expired = _claim(conn, tenant_b, other)
        conn.execute(
            text(
                "UPDATE assay.worker_grants SET expires_at = now() - interval '1 second' "
                "WHERE document_id = :id AND ended_at IS NULL"
            ),
            {"id": other},
        )
    identity = source_identity(service, "worker-fixture")
    for tenant_id, documents in ((tenant_a, (own_document, sibling)), (tenant_b, (other,))):
        context = tss.resolve(tenant_id, StorageAccess.PIPELINE, identity)
        for document in documents:
            write(context, document)
    return {
        "tenant_a": str(tenant_a),
        "tenant_b": str(tenant_b),
        "own": own,
        "sibling": {"document_id": str(sibling)},
        "other_tenant": {"document_id": str(other)},
        "ended": ended,
        "expired": expired,
        "to_end": to_end,
    }


def end_grant(engine: Engine, tenant_id: uuid.UUID, document: uuid.UUID) -> int:
    with engine.begin() as conn:
        _tenant(conn, tenant_id)
        return grants.end(conn, document_id=document)


def cleanup(engine: Engine, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> int:
    """End every fixture grant and take the fixture documents out of the history views."""
    ended = 0
    for tenant_id, numbers in ((tenant_a, (1, 2)), (tenant_b, (1,))):
        with engine.begin() as conn:
            _tenant(conn, tenant_id)
            for number in numbers:
                document = document_id(tenant_id, number)
                ended += grants.end(conn, document_id=document)
                conn.execute(
                    text(
                        "UPDATE assay.documents SET status = 'discarded', completed_at = now() "
                        "WHERE id = :id"
                    ),
                    {"id": document},
                )
    return ended


def handle(event: dict[str, Any]) -> dict[str, Any]:
    """The runner's `worker_fixture`, `end_worker_grant` and `worker_cleanup` actions."""
    tenant_a = uuid.UUID(str(event["tenant_a"]))
    tenant_b = uuid.UUID(str(event["tenant_b"]))
    engine = iam_engine(DbTarget.from_env(), DB_USER, application_name="assay-isolation-fixture")
    try:
        action = event["action"]
        if action == "worker_fixture":
            tss = TssClient(os.environ["TSS_ENDPOINT"], region=os.environ["AWS_REGION"])
            cases = prepare(engine, tss, os.environ["TSS_SERVICE"], tenant_a, tenant_b)
            return {"ok": True, "cases": cases}
        if action == "end_worker_grant":
            ended = end_grant(engine, tenant_a, uuid.UUID(str(event["document_id"])))
            return {"ok": ended == 1, "ended": ended}
        if action == "worker_cleanup":
            return {"ok": True, "ended": cleanup(engine, tenant_a, tenant_b)}
        raise ValueError("unknown action")
    finally:
        engine.dispose()
