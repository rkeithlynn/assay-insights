"""Connections to Aurora from inside the VPC.

- The master user (bootstrap only) reads its password from the RDS-managed secret.
- Every other role signs in with an IAM database authentication token. Generating the token
  is a local signature, so it needs no network path to AWS.

TLS is always `verify-full` against the RDS CA bundle baked into the image.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import boto3
import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

log = logging.getLogger(__name__)

# A paused Serverless v2 cluster (0 ACU) resumes on the first connection, which can take
# around 15 seconds. Keep trying for a little over a minute.
CONNECT_TIMEOUT_SECONDS = 20
CONNECT_ATTEMPTS = 4
MIGRATE_APPLICATION = "assay-db-migrate"
# An IAM token authenticates only the sign-in, so a pooled connection may outlive it. Pooled
# connections are still replaced every 10 minutes.
POOL_RECYCLE_SECONDS = 600


@dataclass(frozen=True)
class DbTarget:
    host: str
    port: int
    database: str
    region: str
    ca_bundle: str

    @classmethod
    def from_env(cls) -> DbTarget:
        return cls(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            database=os.environ["DB_NAME"],
            region=os.environ["AWS_REGION"],
            ca_bundle=os.environ["DB_CA_BUNDLE"],
        )


def _connect_once(
    target: DbTarget, user: str, password: str, application_name: str
) -> psycopg.Connection:
    return psycopg.connect(
        host=target.host,
        port=target.port,
        dbname=target.database,
        user=user,
        password=password,
        sslmode="verify-full",
        sslrootcert=target.ca_bundle,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=application_name,
    )


def _connect(
    target: DbTarget, user: str, password: str, application_name: str = MIGRATE_APPLICATION
) -> psycopg.Connection:
    for attempt in range(1, CONNECT_ATTEMPTS):
        try:
            return _connect_once(target, user, password, application_name)
        except psycopg.OperationalError as exc:
            log.warning(
                "database connection failed, retrying",
                extra={"attempt": attempt, "user": user, "error": type(exc).__name__},
            )
            time.sleep(5)
    return _connect_once(target, user, password, application_name)


def master_connection(target: DbTarget, secret_arn: str) -> psycopg.Connection:
    client = boto3.client("secretsmanager", region_name=target.region)
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    return _connect(target, secret["username"], secret["password"])


def iam_token(target: DbTarget, user: str) -> str:
    client = boto3.client("rds", region_name=target.region)
    return client.generate_db_auth_token(
        DBHostname=target.host, Port=target.port, DBUsername=user, Region=target.region
    )


def iam_connection(target: DbTarget, user: str, application_name: str) -> psycopg.Connection:
    """One connection as `user`, with a fresh IAM token."""
    return _connect(target, user, iam_token(target, user), application_name)


def iam_engine(
    target: DbTarget,
    user: str,
    *,
    application_name: str = MIGRATE_APPLICATION,
    pool_size: int = 0,
) -> Engine:
    """An engine whose every new connection uses a fresh IAM token (they last 15 minutes).

    `pool_size=0` (the migration Lambda) opens a connection per use. A long-running service
    passes a pool size; pooled connections are checked before use and recycled.
    """

    def creator() -> psycopg.Connection:
        return iam_connection(target, user, application_name)

    if pool_size == 0:
        return create_engine("postgresql+psycopg://", creator=creator, poolclass=NullPool)
    return create_engine(
        "postgresql+psycopg://",
        creator=creator,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
    )
