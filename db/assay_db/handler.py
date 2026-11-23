"""Migration Lambda. Runs in the VPC; started by `scripts/migrate.py` and a monthly schedule.

Commands (the event's "command" field):

- `upgrade`: as the master user, create or re-assert the database roles; then, as
  `migrator`, run `alembic upgrade head`, create any missing audit_log partitions and run
  the `verify` checks before committing.
- `verify`: read-only. As `migrator`, check that every tenant table has row-level security
  enabled and forced (checks.verify_rls) and that no role has SUPERUSER, BYPASSRLS or any
  other attribute or membership it must not have (roles.verify_roles).
- `current`: report the current Alembic revision.
- `maintain`: create any missing audit_log partitions (the monthly schedule).

The result never includes credentials or row data.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from sqlalchemy import Connection, text

from assay_db import runner
from assay_db.checks import verify_rls
from assay_db.connect import DbTarget, iam_engine, master_connection
from assay_db.roles import MIGRATOR, bootstrap, verify_roles

log = logging.getLogger()
log.setLevel(logging.INFO)

COMMANDS = frozenset({"upgrade", "verify", "current", "maintain"})


def _driver_connection(conn: Connection) -> psycopg.Connection:
    raw = conn.connection.driver_connection
    if not isinstance(raw, psycopg.Connection):
        raise TypeError(f"expected a psycopg connection, got {type(raw).__name__}")
    return raw


def _verify(conn: Connection, result: dict[str, Any]) -> None:
    """Raises if an invariant is broken; inside `upgrade` that rolls the migration back."""
    raw = _driver_connection(conn)
    report = verify_rls(raw)
    verify_roles(raw)
    result["rls_tenant_tables"] = report.tenant_tables
    result["rls_verified"] = True
    result["roles_verified"] = True


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    command = event.get("command", "current")
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}; expected one of {sorted(COMMANDS)}")
    target = DbTarget.from_env()
    result: dict[str, Any] = {"command": command}

    if command == "upgrade":
        with master_connection(target, os.environ["DB_MASTER_SECRET_ARN"]) as conn:
            roles = bootstrap(conn, target.database)
        result["roles_created"] = roles.created
        result["roles_existing"] = roles.existing

    engine = iam_engine(target, MIGRATOR)
    with engine.begin() as conn:
        user = conn.execute(text("SELECT current_user")).scalar_one()
        if user != MIGRATOR:
            raise RuntimeError(f"expected to run as {MIGRATOR}, got {user}")
        # Two invocations (the schedule and a manual run) never migrate at the same time.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('assay-db-migrate'))"))
        result["revision_before"] = runner.current_revision(conn)
        if command == "upgrade":
            runner.upgrade(conn)
        if command in {"upgrade", "maintain"}:
            result["partitions_created"] = runner.ensure_audit_partitions(conn)
        if command in {"upgrade", "verify"}:
            _verify(conn, result)
        result["revision_after"] = runner.current_revision(conn)

    log.info("migration command finished", extra=result)
    return result
