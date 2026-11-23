"""Database roles (BUILD_SPEC.md sections 7 and 7a) and their idempotent bootstrap.

`bootstrap()` runs as the RDS master user before Alembic. It creates the roles and
database-level grants only; everything inside the database (schemas, tables, row-level
security, table grants) belongs to the migrations, which run as `migrator`.

No role has BYPASSRLS, SUPERUSER, CREATEDB, CREATEROLE or REPLICATION. Login roles
authenticate with IAM database authentication (`rds_iam`), so none has a password.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql

MIGRATOR = "migrator"  # Alembic only. Owns schema `assay` and every table in it.
APP_TENANT = "app_tenant"  # Tenant routes and pipeline workers. Sees one tenant at a time.
PLATFORM_READER = "platform_reader"  # Admin routes: tenants, platform views, audit_log.
PLATFORM_WRITER = "platform_writer"  # Tenant provisioning and admin tenant changes.
AUDIT_WRITER = "audit_writer"  # Cognito trigger Lambdas: INSERT on audit_log only.
TSS = "tss"  # Tenant Storage Service: the only reader of tenant_storage.
# Cannot sign in. Owns the views in schema `platform`, so platform_reader can read them
# without any privilege on the tables behind them.
PLATFORM_VIEW_OWNER = "platform_view_owner"

LOGIN_ROLES: tuple[str, ...] = (
    MIGRATOR,
    APP_TENANT,
    PLATFORM_READER,
    PLATFORM_WRITER,
    AUDIT_WRITER,
    TSS,
)
NOLOGIN_ROLES: tuple[str, ...] = (PLATFORM_VIEW_OWNER,)
ALL_ROLES: tuple[str, ...] = LOGIN_ROLES + NOLOGIN_ROLES

# Granting this RDS-provided role turns on IAM database authentication for a login role.
RDS_IAM = "rds_iam"


@dataclass(frozen=True)
class BootstrapResult:
    created: list[str]
    existing: list[str]


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def bootstrap(conn: psycopg.Connection, database: str) -> BootstrapResult:
    """Create or re-assert every role and its database-level grants. Safe to run repeatedly."""
    created: list[str] = []
    existing: list[str] = []
    with conn.transaction():
        for role in ALL_ROLES:
            ident = sql.Identifier(role)
            if _role_exists(conn, role):
                existing.append(role)
            else:
                conn.execute(sql.SQL("CREATE ROLE {}").format(ident))
                created.append(role)
            login = sql.SQL("LOGIN" if role in LOGIN_ROLES else "NOLOGIN")
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} WITH {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS INHERIT PASSWORD NULL"
                ).format(ident, login)
            )

        for role in LOGIN_ROLES:
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(sql.Identifier(RDS_IAM), sql.Identifier(role))
            )

        # migrator may hand objects to the view owner (ALTER VIEW ... OWNER TO), but does not
        # inherit its privileges or its row-level security policies.
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
                sql.Identifier(PLATFORM_VIEW_OWNER), sql.Identifier(MIGRATOR)
            )
        )

        db = sql.Identifier(database)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                db, sql.SQL(", ").join(sql.Identifier(r) for r in LOGIN_ROLES)
            )
        )
        # migrator creates the schemas it owns.
        conn.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(db, sql.Identifier(MIGRATOR))
        )
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    return BootstrapResult(created=created, existing=existing)
