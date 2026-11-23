"""Run Alembic and audit_log partition upkeep on a connection that is signed in as migrator."""

from __future__ import annotations

from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text

from assay_db.models import SCHEMA

# Monthly audit_log partitions are created this many months ahead (section 7a).
AUDIT_MONTHS_AHEAD = 12


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("assay_db") / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(connection: Connection, revision: str = "head") -> None:
    command.upgrade(alembic_config(connection), revision)


def downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(alembic_config(connection), revision)


def current_revision(connection: Connection) -> str | None:
    context = MigrationContext.configure(connection, opts={"version_table_schema": SCHEMA})
    return context.get_current_revision()


def ensure_audit_partitions(
    connection: Connection, months_ahead: int = AUDIT_MONTHS_AHEAD
) -> list[str]:
    """Create any missing monthly partitions; returns the names created."""
    rows = connection.execute(
        text("SELECT assay.ensure_audit_log_partitions(:months)"), {"months": months_ahead}
    )
    return [r[0] for r in rows]
