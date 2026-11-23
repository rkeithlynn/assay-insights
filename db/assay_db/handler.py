"""Migration Lambda. Runs in the VPC; started by `scripts/migrate.py` and a monthly schedule.

Commands (the event's "command" field):

- `upgrade`: as the master user, create or re-assert the database roles; then, as
  `migrator`, run `alembic upgrade head` and create any missing audit_log partitions.
- `current`: report the current Alembic revision.
- `maintain`: create any missing audit_log partitions (the monthly schedule).

The result never includes credentials or row data.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

from assay_db import runner
from assay_db.connect import DbTarget, iam_engine, master_connection
from assay_db.roles import MIGRATOR, bootstrap

log = logging.getLogger()
log.setLevel(logging.INFO)

COMMANDS = frozenset({"upgrade", "current", "maintain"})


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
        result["revision_after"] = runner.current_revision(conn)

    log.info("migration command finished", extra=result)
    return result
