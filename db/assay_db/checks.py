"""Read-only checks of the tenant-isolation invariants (BUILD_SPEC.md section 6, rule 5).

`verify_rls()` reads only the system catalogs, which every role may read, so it runs as
`migrator` in the migration Lambda (the `verify` command, and at the end of every `upgrade`
before it commits) and in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from assay_db.models import SCHEMA, TENANT_TABLES

PLATFORM_TABLES: frozenset[str] = frozenset({"ai_models"})
ALEMBIC_VERSION_TABLE = "alembic_version"


class RlsVerificationError(RuntimeError):
    """A table in the schema breaks the row-level security invariant."""


@dataclass(frozen=True)
class RlsReport:
    tenant_tables: list[str]  # every one: tenant_id, RLS enabled and forced, >= 1 policy
    platform_tables: list[str]


def verify_rls(conn: psycopg.Connection) -> RlsReport:
    """Raise unless every table but ai_models carries tenant_id and forced row-level security.

    Compares the tables actually in the schema with the models, so a table added without
    RLS, or a model whose table is missing, both fail.
    """
    rows = conn.execute(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                       WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                         AND NOT a.attisdropped),
               (SELECT count(*) FROM pg_catalog.pg_policy p WHERE p.polrelid = c.oid)
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
          AND c.relname <> %s
        """,
        (SCHEMA, ALEMBIC_VERSION_TABLE),
    ).fetchall()
    found = {r[0]: r[1:] for r in rows}
    expected = set(TENANT_TABLES) | PLATFORM_TABLES

    problems = [f"{t}: table missing" for t in sorted(expected - set(found))]
    problems += [f"{t}: table not in the models" for t in sorted(set(found) - expected)]
    for table, (enabled, forced, has_tenant_id, policies) in sorted(found.items()):
        if table in PLATFORM_TABLES:
            if has_tenant_id:
                problems.append(f"{table}: platform-wide table has tenant_id")
            continue
        if not enabled:
            problems.append(f"{table}: row-level security not enabled")
        if not forced:
            problems.append(f"{table}: row-level security not forced")
        if policies == 0:
            problems.append(f"{table}: no policy")
        if not has_tenant_id and table != "tenants":  # in tenants the tenant ID is `id`
            problems.append(f"{table}: no tenant_id column")
    if problems:
        raise RlsVerificationError("; ".join(problems))
    return RlsReport(
        tenant_tables=sorted(t for t in found if t not in PLATFORM_TABLES),
        platform_tables=sorted(t for t in found if t in PLATFORM_TABLES),
    )
