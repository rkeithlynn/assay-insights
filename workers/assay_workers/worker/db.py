"""The worker's database session: `app_worker`, bound to its attempt grant (migration 0010).

Every connection signs in with a fresh IAM token and at once calls
`pipeline.bind_worker(document, attempt, token)`, which checks the grant and ties the session
to it, or raises. From then on the session's row-level security policies show that one
document. The worker never sets `app.tenant_id` (which means nothing to `app_worker`) and has
no other way to choose what it sees.

The bind is its own committed transaction: a session setting made in a transaction that
rolls back is undone with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from sqlalchemy import Engine, create_engine

DB_USER = "app_worker"  # assay_db.roles.APP_WORKER
APPLICATION_NAME = "assay-worker"
BIND = "SELECT pipeline.bind_worker(%s, %s, %s)"


class GrantRefused(Exception):
    """The database would not bind the session: no live grant matches. Never retried."""


@dataclass(frozen=True)
class Job:
    """What Step Functions gave this task. The token never appears in a repr or a log."""

    document_id: uuid.UUID
    attempt: int
    token: str = field(repr=False)


def bind(connection: psycopg.Connection, job: Job) -> uuid.UUID:
    """Tie this session to the job's grant and return the tenant the grant's row names."""
    try:
        row = connection.execute(BIND, (job.document_id, job.attempt, job.token)).fetchone()
        connection.commit()
    except psycopg.errors.InsufficientPrivilege:
        connection.rollback()
        raise GrantRefused("no live grant for this document, attempt and token") from None
    if row is None or row[0] is None:
        raise GrantRefused("no live grant for this document, attempt and token")
    return uuid.UUID(str(row[0]))


def bound_engine(connect: Callable[[], psycopg.Connection], job: Job) -> Engine:
    """One pooled connection, bound when it is opened. A replacement connection binds again,
    and is refused if the grant has ended meanwhile.

    `hide_parameters`: a statement's values are never written into an error message.
    """

    def creator() -> psycopg.Connection:
        connection = connect()
        try:
            bind(connection, job)
        except BaseException:
            connection.close()
            raise
        return connection

    return create_engine(
        "postgresql+psycopg://",
        creator=creator,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def aurora_engine(job: Job) -> Engine:
    from assay_db.connect import DbTarget, iam_connection

    target = DbTarget.from_env()
    return bound_engine(lambda: iam_connection(target, DB_USER, APPLICATION_NAME), job)
