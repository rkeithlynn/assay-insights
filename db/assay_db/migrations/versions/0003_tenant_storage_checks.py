"""Allowed values for tenant_storage.mode and tenant_storage.status.

Both columns were free text until the Tenant Storage Service defined them (build step 4):
`mode` is the isolation mode (only `dedicated` exists) and `status` is `active` or
`offboarded`. No grant or policy changes: `tss` stays the only reader of the table and
`platform_writer` its only writer.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen with this revision on purpose; later revisions change the lists with their own SQL.
MODE_CHECK = "mode IN ('dedicated')"
STATUS_CHECK = "status IN ('active', 'offboarded')"


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_tenant_storage_mode"), "tenant_storage", MODE_CHECK, schema="assay"
    )
    op.create_check_constraint(
        op.f("ck_tenant_storage_status"), "tenant_storage", STATUS_CHECK, schema="assay"
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_tenant_storage_status"), "tenant_storage", schema="assay", type_="check"
    )
    op.drop_constraint(
        op.f("ck_tenant_storage_mode"), "tenant_storage", schema="assay", type_="check"
    )
