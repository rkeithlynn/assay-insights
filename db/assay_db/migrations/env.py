"""Alembic environment. Migrations always run as the `migrator` database role.

The migration Lambda (and the tests) pass an open connection in
`config.attributes["connection"]`. For local development against a throwaway PostgreSQL,
`ASSAY_DATABASE_URL` may be set instead.
"""

from __future__ import annotations

import os
from typing import Any

from alembic import context
from sqlalchemy import Connection, create_engine, text

from assay_db.models import SCHEMA, Base

config = context.config
target_metadata = Base.metadata

# Monthly partitions of audit_log are created by assay.ensure_audit_log_partitions(), not by
# migrations, so autogenerate must not try to drop them.
PARTITION_PREFIX = "audit_log_"


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table" and reflected and compare_to is None and name:
        return not name.startswith(PARTITION_PREFIX)
    return True


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "schema":
        return name == SCHEMA
    return True


def _configure(connection: Connection) -> None:
    # migrator owns schema assay; the version table lives in it.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        compare_type=True,
        transaction_per_migration=False,
    )


def run_offline() -> None:
    context.configure(
        url=os.environ.get("ASSAY_DATABASE_URL", "postgresql+psycopg://"),
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    url = os.environ.get("ASSAY_DATABASE_URL")
    if not url:
        raise RuntimeError("pass a connection in config.attributes or set ASSAY_DATABASE_URL")
    engine = create_engine(url)
    with engine.begin() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
