"""The account check every tenant and admin route runs, inside the request's transaction.

A verified token is not enough on its own: it lives for 60 minutes after its user is
disabled or its tenant suspended. So every request also requires that:

- the token's `sub` matches a user row (in the token's tenant, for the tenant pool; in the
  platform tenant, for the platform pool);
- that user is not disabled;
- the user's tenant is `active` (a suspended tenant's users are refused, section 10, and so
  are users of a tenant that is still provisioning or failed);
- the token's group equals `users.role`, so a role change takes effect on the next request;
- the token comes from a sign-in after `users.sessions_revoked_at`. Resetting a user's MFA or
  disabling them sets it, so every token they already hold stops working at once (global
  sign-out revokes only refresh tokens, and the API verifies access tokens itself).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Row, column, select, table

from assay_api.auth import Principal
from assay_db.models import Tenant, User

# platform.platform_users_v (migration 0002): users of the platform tenant only.
PLATFORM_USERS = table(
    "platform_users_v",
    column("user_id"),
    column("tenant_id"),
    column("tenant_name"),
    column("tenant_status"),
    column("cognito_sub"),
    column("email"),
    column("name"),
    column("role"),
    column("status"),
    column("sessions_revoked_at"),
    schema="platform",
)


class Refusal(enum.StrEnum):
    """Why an authenticated caller is refused. Returned to the caller as the error code."""

    ACCOUNT_NOT_FOUND = "account_not_found"
    USER_DISABLED = "user_disabled"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_NOT_ACTIVE = "tenant_not_active"
    ROLE_MISMATCH = "role_mismatch"
    SESSION_REVOKED = "session_revoked"


class AccountRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class Account:
    """A caller who passed both the token check and the account check."""

    principal: Principal
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    email: str
    name: str
    role: str
    status: str


def _tenant_row(conn: Connection, principal: Principal) -> Row[Any] | None:
    # Row-level security already limits both tables to the token's tenant; the explicit
    # tenant condition is a second guard, not the only one.
    query = (
        select(
            User.id,
            User.tenant_id,
            Tenant.name,
            Tenant.status,
            User.email,
            User.name,
            User.role,
            User.status,
            User.sessions_revoked_at,
        )
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(User.cognito_sub == principal.sub, User.tenant_id == principal.tenant_id)
    )
    return conn.execute(query).one_or_none()


def _platform_row(conn: Connection, principal: Principal) -> Row[Any] | None:
    v = PLATFORM_USERS.c
    query = select(
        v.user_id,
        v.tenant_id,
        v.tenant_name,
        v.tenant_status,
        v.email,
        v.name,
        v.role,
        v.status,
        v.sessions_revoked_at,
    ).where(v.cognito_sub == principal.sub)
    return conn.execute(query).one_or_none()


def load_account(conn: Connection, principal: Principal) -> Account:
    """Raise AccountRefused unless the caller may use the API right now."""
    if principal.pool == "tenant":
        row = _tenant_row(conn, principal)
    else:
        row = _platform_row(conn, principal)
    if row is None:
        raise AccountRefused(Refusal.ACCOUNT_NOT_FOUND)
    user_id, tenant_id, tenant_name, tenant_status, email, name, role, status, revoked = row
    if status == "disabled":
        raise AccountRefused(Refusal.USER_DISABLED)
    if revoked is not None and _signed_in_at(principal) <= revoked:
        raise AccountRefused(Refusal.SESSION_REVOKED)
    if tenant_status == "suspended":
        raise AccountRefused(Refusal.TENANT_SUSPENDED)
    if tenant_status != "active":
        raise AccountRefused(Refusal.TENANT_NOT_ACTIVE)
    if role != principal.role:
        raise AccountRefused(Refusal.ROLE_MISMATCH)
    return Account(
        principal=principal,
        user_id=uuid.UUID(str(user_id)),
        tenant_id=uuid.UUID(str(tenant_id)),
        tenant_name=str(tenant_name),
        email=str(email),
        name=str(name),
        role=str(role),
        status=str(status),
    )


def _signed_in_at(principal: Principal) -> dt.datetime:
    return dt.datetime.fromtimestamp(principal.auth_time, dt.UTC)
