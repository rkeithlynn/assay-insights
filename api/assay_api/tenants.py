"""Adding, retrying, suspending and reactivating business tenants (BUILD_SPEC.md sections 5, 6).

Shared by the admin routes and the seed Lambda, so `scripts/seed.py` adds a tenant through
exactly the path the portal uses: `assay_db.tenants.add_tenant` inserts the row
(`provisioning`) and its audit row as `platform_writer`, then the provisioning state machine
is started with the same request `scripts/add_tenant.py` sends. The task does the rest.

The execution starts before the transaction commits. If starting it fails, the insert is
rolled back and nothing is left behind. If the commit fails after the start, the task inserts
the same row itself (`add_tenant` is idempotent on the tenant ID).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from assay_api.services import Provisioning, provision_request
from assay_db import audit, tenants

STATUS_ACTIVE = tenants.STATUS_ACTIVE
STATUS_SUSPENDED = "suspended"
STATUS_FAILED = tenants.STATUS_FAILED


class TenantConflict(Exception):
    """The tenant is not in a state that allows the change. `code` goes to the caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _actor(actor: audit.AuditActor) -> tenants.Actor:
    return tenants.Actor(
        role=actor.role, user_id=actor.user_id, email=actor.email, principal=actor.principal
    )


def _request_actor(actor: audit.AuditActor) -> dict[str, Any]:
    """The actor as assay_tss.provision.task.ActorModel expects it."""
    fields: dict[str, Any] = {"role": actor.role}
    if actor.user_id is not None:
        fields["user_id"] = str(actor.user_id)
    if actor.email:
        fields["email"] = actor.email
    if actor.principal:
        fields["principal"] = actor.principal
    return fields


def create_tenant(
    conn: Connection,
    provisioning: Provisioning,
    *,
    name: str,
    admin_name: str,
    admin_email: str,
    actor: audit.AuditActor,
) -> uuid.UUID:
    """Insert the tenant and start provisioning it. `conn` is a platform_writer transaction."""
    tenant_id = uuid.uuid4()
    clean_name = tenants.check_name(name, "tenant name")
    admin = tenants.FirstAdmin(
        tenants.check_name(admin_name, "admin name"), tenants.check_email(admin_email)
    )
    tenants.add_tenant(
        conn, tenant_id=tenant_id, name=clean_name, first_admin=admin, actor=_actor(actor)
    )
    provisioning.start(
        tenant_id=tenant_id,
        request=provision_request(
            tenant_id=tenant_id,
            name=clean_name,
            admin_name=admin.name,
            admin_email=admin.email,
            actor=_request_actor(actor),
            retry=False,
        ),
    )
    return tenant_id


@dataclass(frozen=True)
class FirstAdminOnRecord:
    name: str
    email: str


def first_admin_on_record(reader: Connection, tenant_id: uuid.UUID) -> FirstAdminOnRecord:
    """The first admin named when the tenant was added, from its `created` audit row.

    Read as platform_reader, which may read audit_log (platform_writer may not).
    """
    row = reader.execute(
        text(
            "SELECT detail FROM assay.audit_log WHERE tenant_id = :tenant AND category = 'tenant' "
            "AND action = 'created' ORDER BY at DESC LIMIT 1"
        ),
        {"tenant": tenant_id},
    ).one_or_none()
    detail = row[0] if row is not None else None
    if isinstance(detail, str):
        detail = json.loads(detail)
    try:
        first = detail["after"]["first_admin"]  # type: ignore[index]
        return FirstAdminOnRecord(name=str(first["name"]), email=str(first["email"]))
    except KeyError, TypeError:
        raise TenantConflict("first_admin_unknown") from None


def retry_tenant(
    writer: Connection,
    provisioning: Provisioning,
    *,
    tenant_id: uuid.UUID,
    first_admin: FirstAdminOnRecord,
    actor: audit.AuditActor,
) -> None:
    """`failed` to `provisioning` (with its audit row), then a new execution with retry."""
    tenant = tenants.lock_tenant(writer, tenant_id)
    if tenant is None or tenant.kind != tenants.KIND_BUSINESS:
        raise TenantConflict("tenant_not_found")
    if tenant.status != STATUS_FAILED:
        raise TenantConflict("not_failed")
    tenants.begin_retry(writer, tenant, _actor(actor))
    provisioning.start(
        tenant_id=tenant_id,
        request=provision_request(
            tenant_id=tenant_id,
            name=tenant.name,
            admin_name=first_admin.name,
            admin_email=first_admin.email,
            actor=_request_actor(actor),
            retry=True,
        ),
    )


# (from, to) -> audit action
STATUS_CHANGES = {
    (STATUS_ACTIVE, STATUS_SUSPENDED): "suspended",
    (STATUS_SUSPENDED, STATUS_ACTIVE): "reactivated",
}


def set_status(
    writer: Connection,
    *,
    tenant_id: uuid.UUID,
    status: str,
    actor: audit.AuditActor,
    request: audit.RequestInfo = audit.NO_REQUEST,
) -> str:
    """Suspend an active business tenant or reactivate a suspended one. Returns the action."""
    tenant = tenants.lock_tenant(writer, tenant_id)
    if tenant is None or tenant.kind != tenants.KIND_BUSINESS:
        raise TenantConflict("tenant_not_found")
    action = STATUS_CHANGES.get((tenant.status, status))
    if action is None:
        raise TenantConflict("status_change_not_allowed")
    writer.execute(
        text("UPDATE assay.tenants SET status = :status WHERE id = :id"),
        {"id": tenant_id, "status": status},
    )
    audit.write(
        writer,
        tenant_id=tenant_id,
        actor=actor,
        category="tenant",
        action=action,
        target_type="tenant",
        target_id=str(tenant_id),
        target_label=tenant.name,
        detail={"before": {"status": tenant.status}, "after": {"status": status}},
        request=request,
    )
    return action
