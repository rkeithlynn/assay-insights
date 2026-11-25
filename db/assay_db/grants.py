"""Worker attempt grants (migration 0010; build step 6, second half).

The pipeline's `claim` step calls `claim()` as `app_tenant`, in the document's tenant, just
before Step Functions starts the worker task, and passes the returned token to that one task.
Only the token's SHA-256 is stored. The task presents the token to the Tenant Storage Service
and to the database; both take the tenant from the grant's row. `end()` runs when the task is
over, whatever its outcome, and the expiry covers a `release` step that never ran.

A live grant is also the tenant's slot in the cap on concurrent workers: `claim()` returns
None while the tenant already has `max_per_tenant` of them, and the state machine waits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from sqlalchemy import Connection, text

TOKEN_BYTES = 32


def new_token() -> str:
    """URL-safe text (letters, digits, `-`, `_`): never a colon, which separates the parts of
    the session setting `pipeline.bind_worker` writes."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def claim(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    attempt: int,
    max_per_tenant: int,
    ttl: dt.timedelta,
) -> str | None:
    """Take a worker slot for this attempt and return its token, or None if the tenant has
    none free. Any earlier grant for the document ends first, so a repeated claim (a retried
    step) leaves one live grant, and the earlier token is dead."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"worker-grants:{tenant_id}"},
    )
    end(conn, document_id=document_id)
    live = conn.execute(
        text(
            "SELECT count(*) FROM assay.worker_grants WHERE ended_at IS NULL AND expires_at > now()"
        )
    ).scalar_one()
    if int(live) >= max_per_tenant:
        return None
    token = new_token()
    conn.execute(
        text(
            "INSERT INTO assay.worker_grants "
            "(tenant_id, document_id, attempt, token_hash, expires_at) "
            "VALUES (:tenant_id, :document_id, :attempt, :token_hash, now() + :ttl)"
        ),
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "attempt": attempt,
            "token_hash": token_hash(token),
            "ttl": ttl,
        },
    )
    return token


def end(conn: Connection, *, document_id: uuid.UUID, attempt: int | None = None) -> int:
    """End every live grant for the document, or only those of one attempt (a step of an
    attempt that was superseded must not end its successor's). Returns how many ended."""
    result = conn.execute(
        text(
            "UPDATE assay.worker_grants SET ended_at = now() "
            "WHERE document_id = :document_id AND ended_at IS NULL "
            "AND (CAST(:attempt AS integer) IS NULL OR attempt <= CAST(:attempt AS integer))"
        ),
        {"document_id": document_id, "attempt": attempt},
    )
    return int(result.rowcount)
