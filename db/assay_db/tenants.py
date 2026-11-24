"""Adding and provisioning a tenant: the rows, as database role `platform_writer`.

This is the one add-tenant path (BUILD_SPEC.md sections 5 and 6). The provisioning task
uses every function here; `POST /admin/tenants` (build step 5) calls `add_tenant` in the
request's transaction and then starts the same provisioning execution.

Each function runs inside the caller's transaction and writes its audit row in that same
transaction (section 7a). What `platform_writer` may do shapes the SQL:

- it can read and update `tenants`, but only INSERT into `tenant_storage`, `users` and
  `audit_log`, so those inserts use `ON CONFLICT DO NOTHING` with no conflict target (a
  target would need SELECT) and are safe to repeat;
- it cannot read `tenant_storage` back. Only the TSS can.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from assay_db.catalog import DefaultModels, ensure_default_models

KIND_BUSINESS = "business"
STATUS_PROVISIONING = "provisioning"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"
FIRST_ADMIN_ROLE = "tenant-admin"

# Recorded as the actor's role when someone starts provisioning with scripts/add_tenant.py.
ROLE_OPERATOR = "operator"
# Recorded for what the provisioning task itself does.
ROLE_SYSTEM = "system"

MAX_NAME = 200
MAX_ERROR = 500
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")

# Stable user IDs, so a repeated provisioning run inserts the same row.
_USER_NAMESPACE = uuid.UUID("6f1d3c0e-7a55-4c0b-9d0e-2f6b8a4e91c7")


class TenantStateError(RuntimeError):
    """The tenant row is not in a state this step may run from. Nothing was written."""


@dataclass(frozen=True)
class Actor:
    """Who asked for the change. `principal` is the IAM caller, for an operator."""

    role: str
    user_id: uuid.UUID | None = None
    email: str | None = None
    principal: str | None = None


SYSTEM = Actor(role=ROLE_SYSTEM)


@dataclass(frozen=True)
class FirstAdmin:
    name: str
    email: str


@dataclass(frozen=True)
class TenantRow:
    id: uuid.UUID
    name: str
    kind: str
    status: str


def check_name(value: str, what: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > MAX_NAME:
        raise ValueError(f"{what} must be 1 to {MAX_NAME} characters")
    return cleaned


def check_email(value: str) -> str:
    cleaned = value.strip()
    if not _EMAIL.match(cleaned):
        raise ValueError("email address is not valid")
    return cleaned


def first_admin_user_id(tenant_id: uuid.UUID, cognito_sub: str) -> uuid.UUID:
    return uuid.uuid5(_USER_NAMESPACE, f"{tenant_id}:{cognito_sub}")


def _audit(
    conn: Connection,
    *,
    tenant: TenantRow,
    actor: Actor,
    category: str,
    action: str,
    detail: dict[str, Any],
    outcome: str = "success",
    target_type: str = "tenant",
    target_id: str | None = None,
    target_label: str | None = None,
) -> None:
    if actor.principal:
        detail = {**detail, "started_by": actor.principal}
    conn.execute(
        text(
            "INSERT INTO assay.audit_log (tenant_id, actor_user_id, actor_email, actor_role, "
            "category, action, target_type, target_id, target_label, detail, outcome) "
            "VALUES (:tenant_id, :actor_user_id, :actor_email, :actor_role, :category, "
            ":action, :target_type, :target_id, :target_label, CAST(:detail AS jsonb), :outcome)"
        ),
        {
            "tenant_id": tenant.id,
            "actor_user_id": actor.user_id,
            "actor_email": actor.email,
            "actor_role": actor.role,
            "category": category,
            "action": action,
            "target_type": target_type,
            "target_id": target_id or str(tenant.id),
            "target_label": target_label or tenant.name,
            "detail": json.dumps(detail),
            "outcome": outcome,
        },
    )


def get_tenant(conn: Connection, tenant_id: uuid.UUID) -> TenantRow | None:
    row = conn.execute(
        text("SELECT id, name, kind, status FROM assay.tenants WHERE id = :id"),
        {"id": tenant_id},
    ).one_or_none()
    return None if row is None else TenantRow(*row)


def lock_tenant(conn: Connection, tenant_id: uuid.UUID) -> TenantRow | None:
    """As `get_tenant`, holding the row until the transaction ends."""
    row = conn.execute(
        text("SELECT id, name, kind, status FROM assay.tenants WHERE id = :id FOR UPDATE"),
        {"id": tenant_id},
    ).one_or_none()
    return None if row is None else TenantRow(*row)


def add_tenant(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    name: str,
    first_admin: FirstAdmin,
    actor: Actor,
) -> bool:
    """Insert a business tenant with status `provisioning`. True when this call inserted it.

    Safe to repeat with the same ID: a second call changes nothing and writes no audit row.
    """
    name = check_name(name, "tenant name")
    admin = FirstAdmin(check_name(first_admin.name, "admin name"), check_email(first_admin.email))
    inserted = conn.execute(
        text(
            "INSERT INTO assay.tenants (id, name, kind, status) "
            "VALUES (:id, :name, :kind, :status) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": tenant_id, "name": name, "kind": KIND_BUSINESS, "status": STATUS_PROVISIONING},
    ).rowcount
    if inserted != 1:
        return False
    _audit(
        conn,
        tenant=TenantRow(tenant_id, name, KIND_BUSINESS, STATUS_PROVISIONING),
        actor=actor,
        category="tenant",
        action="created",
        detail={
            "after": {
                "name": name,
                "kind": KIND_BUSINESS,
                "status": STATUS_PROVISIONING,
                "first_admin": {"name": admin.name, "email": admin.email},
            }
        },
    )
    return True


def begin_retry(conn: Connection, tenant: TenantRow, actor: Actor) -> TenantRow:
    """`failed` back to `provisioning`, with its audit row."""
    updated = conn.execute(
        text("UPDATE assay.tenants SET status = :new WHERE id = :id AND status = :old"),
        {"id": tenant.id, "new": STATUS_PROVISIONING, "old": STATUS_FAILED},
    ).rowcount
    if updated != 1:
        raise TenantStateError(f"tenant is {tenant.status}; only a failed tenant can be retried")
    _audit(
        conn,
        tenant=tenant,
        actor=actor,
        category="tenant",
        action="provisioning_retried",
        detail={"before": {"status": STATUS_FAILED}, "after": {"status": STATUS_PROVISIONING}},
    )
    return TenantRow(tenant.id, tenant.name, tenant.kind, STATUS_PROVISIONING)


def record_storage(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    mode: str,
    bucket: str,
    kms_key_arn: str,
    role_arn: str,
    vector_bucket: str,
    vector_index: str,
    region: str,
) -> bool:
    """Write the tenant_storage row. True when this call inserted it."""
    inserted = conn.execute(
        text(
            "INSERT INTO assay.tenant_storage (tenant_id, mode, bucket, kms_key_arn, role_arn, "
            "vector_bucket, vector_index, region, status) VALUES (:tenant_id, :mode, :bucket, "
            ":kms_key_arn, :role_arn, :vector_bucket, :vector_index, :region, 'active') "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "tenant_id": tenant_id,
            "mode": mode,
            "bucket": bucket,
            "kms_key_arn": kms_key_arn,
            "role_arn": role_arn,
            "vector_bucket": vector_bucket,
            "vector_index": vector_index,
            "region": region,
        },
    ).rowcount
    return inserted == 1


def activate(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    cognito_sub: str,
    first_admin: FirstAdmin,
    actor: Actor,
    default_models: DefaultModels | None = None,
) -> uuid.UUID:
    """The first admin's user row, the default models, tenant `active`, and the audit rows.

    Returns the first admin's user ID. With `default_models`, a tenant that has no model
    selection yet gets the defaults in the same transaction (assay_db.catalog), so an active
    tenant always has one available and one selected model per job.
    """
    tenant = lock_tenant(conn, tenant_id)
    if tenant is None or tenant.status != STATUS_PROVISIONING:
        state = "missing" if tenant is None else tenant.status
        raise TenantStateError(f"tenant is {state}; expected {STATUS_PROVISIONING}")
    user_id = first_admin_user_id(tenant_id, cognito_sub)
    email = check_email(first_admin.email)
    name = check_name(first_admin.name, "admin name")
    inserted = conn.execute(
        text(
            "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, status, "
            "invited_by) VALUES (:id, :tenant_id, :sub, :email, :name, :role, 'invited', "
            ":invited_by) ON CONFLICT DO NOTHING"
        ),
        {
            "id": user_id,
            "tenant_id": tenant_id,
            "sub": cognito_sub,
            "email": email,
            "name": name,
            "role": FIRST_ADMIN_ROLE,
            "invited_by": actor.user_id,
        },
    ).rowcount
    if inserted == 1:
        _audit(
            conn,
            tenant=tenant,
            actor=actor,
            category="user",
            action="invited",
            target_type="user",
            target_id=str(user_id),
            target_label=email,
            detail={"after": {"name": name, "email": email, "role": FIRST_ADMIN_ROLE}},
        )
    if default_models is not None:
        ensure_default_models(conn, tenant_id=tenant_id, defaults=default_models)
    conn.execute(
        text("UPDATE assay.tenants SET status = :new WHERE id = :id"),
        {"id": tenant_id, "new": STATUS_ACTIVE},
    )
    _audit(
        conn,
        tenant=tenant,
        actor=SYSTEM,
        category="tenant",
        action="provisioning_succeeded",
        detail={"before": {"status": STATUS_PROVISIONING}, "after": {"status": STATUS_ACTIVE}},
    )
    return user_id


def record_failure(conn: Connection, *, tenant_id: uuid.UUID, step: str, error: str) -> bool:
    """`provisioning` to `failed`, recording the failed step. False if there was nothing to do.

    Safe to repeat: a tenant that is already `failed` (or was never inserted) is left alone.
    """
    tenant = lock_tenant(conn, tenant_id)
    if tenant is None or tenant.status != STATUS_PROVISIONING:
        return False
    conn.execute(
        text("UPDATE assay.tenants SET status = :new WHERE id = :id"),
        {"id": tenant_id, "new": STATUS_FAILED},
    )
    _audit(
        conn,
        tenant=tenant,
        actor=SYSTEM,
        category="tenant",
        action="provisioning_failed",
        outcome="failure",
        detail={
            "before": {"status": STATUS_PROVISIONING},
            "after": {"status": STATUS_FAILED},
            "step": step,
            "error": error[:MAX_ERROR],
        },
    )
    return True
