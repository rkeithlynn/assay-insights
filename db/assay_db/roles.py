"""Database roles (BUILD_SPEC.md sections 7 and 7a) and their idempotent bootstrap.

`bootstrap()` runs as the RDS master user before Alembic. It creates the roles and
database-level grants only; everything inside the database (schemas, tables, row-level
security, table grants) belongs to the migrations, which run as `migrator`.

No role has SUPERUSER, BYPASSRLS, REPLICATION, CREATEDB or CREATEROLE. Login roles
authenticate with IAM database authentication (`rds_iam`), so none has a password.

Aurora's master user is not a superuser. It has CREATEROLE, CREATEDB and membership of
rds_superuser, and PostgreSQL 16+ applies these rules to it:

- It may not mention SUPERUSER, BYPASSRLS or REPLICATION in CREATE ROLE or ALTER ROLE at
  all, not even to switch them off. So the bootstrap never does: a new role gets
  PostgreSQL's defaults (all three off), and `verify_roles()` checks the result afterwards
  instead of forcing it.
- It may alter or grant only roles on which it holds ADMIN OPTION. It holds that on every
  role it created, and on rds_iam.
- It is not a member (with SET or INHERIT) of the roles it creates, and needs not be: the
  bootstrap never switches to them or creates objects as them.

Everything runs in one transaction, so a failure leaves no role half-created.
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

# The only role memberships our roles may have: (member, granted role) -> (inherit, set).
EXPECTED_MEMBERSHIPS: dict[tuple[str, str], tuple[bool, bool]] = {
    **{(role, RDS_IAM): (True, True) for role in LOGIN_ROLES},
    # migrator may hand objects to the view owner, but inherits none of its privileges.
    (MIGRATOR, PLATFORM_VIEW_OWNER): (False, True),
}


class RoleVerificationError(RuntimeError):
    """A role has an attribute or membership it must not have. Nothing was committed."""


@dataclass(frozen=True)
class BootstrapResult:
    created: list[str]
    existing: list[str]


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def _login(role: str) -> sql.SQL:
    return sql.SQL("LOGIN" if role in LOGIN_ROLES else "NOLOGIN")


def verify_roles(conn: psycopg.Connection) -> None:
    """Raise unless every role has only the attributes and memberships designed for it.

    Reads pg_roles and pg_auth_members, which any role may read, so it works under the
    Aurora master user.
    """
    problems: list[str] = []
    rows = conn.execute(
        """
        SELECT rolname, rolsuper, rolbypassrls, rolreplication, rolcreatedb, rolcreaterole,
               rolcanlogin
        FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
        """,
        (list(ALL_ROLES),),
    ).fetchall()
    found = {r[0] for r in rows}
    problems += [f"{role}: missing" for role in ALL_ROLES if role not in found]
    for name, superuser, bypassrls, replication, createdb, createrole, login in rows:
        for attribute, value in (
            ("SUPERUSER", superuser),
            ("BYPASSRLS", bypassrls),
            ("REPLICATION", replication),
            ("CREATEDB", createdb),
            ("CREATEROLE", createrole),
        ):
            if value:
                problems.append(f"{name}: has {attribute}")
        if login != (name in LOGIN_ROLES):
            problems.append(f"{name}: LOGIN is {login}")

    memberships = {
        (member, granted): (inherit, set_)
        for member, granted, inherit, set_ in conn.execute(
            """
            SELECT m.rolname, g.rolname, bool_or(a.inherit_option), bool_or(a.set_option)
            FROM pg_catalog.pg_auth_members a
            JOIN pg_catalog.pg_roles m ON m.oid = a.member
            JOIN pg_catalog.pg_roles g ON g.oid = a.roleid
            WHERE m.rolname = ANY(%s)
            GROUP BY m.rolname, g.rolname
            """,
            (list(ALL_ROLES),),
        ).fetchall()
    }
    for key, options in memberships.items():
        expected = EXPECTED_MEMBERSHIPS.get(key)
        if expected is None:
            problems.append(f"{key[0]}: unexpected membership of {key[1]}")
        elif options != expected:
            problems.append(f"{key[0]} in {key[1]}: (inherit, set) is {options}, not {expected}")
    problems += [
        f"{member}: not a member of {granted}"
        for member, granted in EXPECTED_MEMBERSHIPS
        if member in found and (member, granted) not in memberships
    ]
    if problems:
        raise RoleVerificationError("; ".join(sorted(problems)))


def bootstrap(conn: psycopg.Connection, database: str) -> BootstrapResult:
    """Create every role and its database-level grants, then verify. Safe to run repeatedly.

    Runs as the RDS master user, never as a superuser; see the module docstring for what
    that allows. Everything is one transaction: if any step or the verification fails,
    nothing is committed.
    """
    created: list[str] = []
    existing: list[str] = []
    with conn.transaction():
        for role in ALL_ROLES:
            ident = sql.Identifier(role)
            if _role_exists(conn, role):
                existing.append(role)
                # Only what CREATEROLE with ADMIN OPTION may change. SUPERUSER, BYPASSRLS
                # and REPLICATION are never mentioned; verify_roles() checks them.
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH {} PASSWORD NULL").format(ident, _login(role))
                )
            else:
                # Defaults: NOSUPERUSER NOBYPASSRLS NOREPLICATION NOCREATEDB NOCREATEROLE
                # INHERIT, and no password.
                conn.execute(sql.SQL("CREATE ROLE {} WITH {}").format(ident, _login(role)))
                created.append(role)

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

        # The master user owns the database (RDS creates it), so it may manage these grants.
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
        # public is owned by pg_database_owner, that is by the database owner.
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")

        verify_roles(conn)
    return BootstrapResult(created=created, existing=existing)
