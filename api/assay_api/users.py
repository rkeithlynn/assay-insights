"""Users of a business tenant: invite, change, and the Cognito side of each change.

Shared by the tenant-admin routes (`assay_api.routes.users`, as `app_tenant` in the
caller's tenant) and the seed Lambda (`assay_api.seed`, as `platform_writer`), so a seeded
user is invited exactly as the portal invites one.

The tenant ID always comes from the verified token (or, for the seed, from the tenant row
it just created), never from a request body. Each Cognito call carries it, and the Cognito
admin Lambda refuses a target whose `custom:tenant_id` differs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from assay_api.services import CognitoAdmin
from assay_db import audit
from assay_db.tenants import first_admin_user_id

TENANT_ROLES = ("tenant-admin", "tenant-user")
TENANT_ADMIN = "tenant-admin"
TENANT_USER = "tenant-user"


@dataclass(frozen=True)
class Invited:
    user_id: uuid.UUID
    cognito_sub: str
    # False when the user already existed for this tenant (a repeated call).
    created: bool


def invite_user(
    conn: Connection,
    cognito: CognitoAdmin,
    *,
    tenant_id: uuid.UUID,
    name: str,
    email: str,
    role: str,
    invited_by: uuid.UUID | None,
    actor: audit.AuditActor,
    request: audit.RequestInfo = audit.NO_REQUEST,
) -> Invited:
    """Create the Cognito user (Cognito emails the invitation), then the user row and audit.

    Safe to repeat: the user ID is derived from the tenant and the Cognito `sub`, the insert
    does nothing if the row exists, and the audit row is written only by the insert.
    """
    if role not in TENANT_ROLES:
        raise ValueError("role must be tenant-admin or tenant-user")
    result = cognito.call(
        "invite", pool="tenant", tenant_id=str(tenant_id), email=email, name=name, role=role
    )
    sub = str(result["sub"])
    user_id = first_admin_user_id(tenant_id, sub)
    inserted = conn.execute(
        text(
            "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, status, "
            "invited_by) VALUES (:id, :tenant_id, :sub, :email, :name, :role, 'invited', "
            ":invited_by) ON CONFLICT DO NOTHING"
        ),
        {
            "id": user_id,
            "tenant_id": tenant_id,
            "sub": sub,
            "email": email,
            "name": name,
            "role": role,
            "invited_by": invited_by,
        },
    ).rowcount
    if inserted == 1:
        audit.write(
            conn,
            tenant_id=tenant_id,
            actor=actor,
            category="user",
            action="invited",
            target_type="user",
            target_id=str(user_id),
            target_label=email,
            detail={"after": {"name": name, "email": email, "role": role}},
            request=request,
        )
    return Invited(user_id=user_id, cognito_sub=sub, created=inserted == 1)
