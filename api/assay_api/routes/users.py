"""Users of the caller's tenant: tenant-admin only (BUILD_SPEC.md sections 5 and 10).

Every route runs as `app_tenant` in the caller's tenant, so a user ID from another tenant
is simply not found. Every change writes its audit row in the same transaction, and the
Cognito call is made last, just before the commit: if Cognito refuses, nothing is written.

Rules (section 10):
- an admin cannot change their own role or disable themselves;
- a tenant always keeps one admin who is not disabled;
- MFA can be reset only for a tenant-user of the same tenant (a tenant-admin's MFA is reset
  by the master admin).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, Row, text

from assay_api.requestinfo import request_info
from assay_api.security import Caller, TenantAdmin
from assay_api.services import CognitoAdmin, CognitoRefused
from assay_api.users import TENANT_ADMIN, TENANT_USER, invite_user
from assay_db import audit
from assay_db.tenants import MAX_NAME

router = APIRouter()

Role = Literal["tenant-admin", "tenant-user"]
UserStatus = Literal["invited", "active", "disabled"]


def cognito_admin(request: Request) -> CognitoAdmin:
    admin: CognitoAdmin = request.app.state.cognito
    return admin


Cognito = Annotated[CognitoAdmin, Depends(cognito_admin)]


class UserOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: Role
    status: UserStatus
    last_sign_in_at: dt.datetime | None


class UserList(BaseModel):
    users: list[UserOut]


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$")
    role: Role


class UserChange(BaseModel):
    """Change the role, or disable or enable the user. At least one field."""

    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def _something(self) -> UserChange:
        if self.role is None and self.status is None:
            raise ValueError("give role or status")
        return self


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


def _cognito_error(exc: CognitoRefused) -> HTTPException:
    if exc.code in {"email_in_use", "user_not_found", "wrong_state"}:
        return _conflict(exc.code)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cognito_failed")


USER_COLUMNS = "id, cognito_sub, name, email, role, status, last_sign_in_at FROM assay.users"


def _out(row: Row[Any]) -> UserOut:
    return UserOut(
        id=row.id,
        name=row.name,
        email=row.email,
        role=row.role,
        status=row.status,
        last_sign_in_at=row.last_sign_in_at,
    )


def _target(conn: Connection, user_id: uuid.UUID) -> Row[Any]:
    """The user, locked for this transaction. Row-level security limits it to the tenant."""
    row = conn.execute(
        text(f"SELECT {USER_COLUMNS} WHERE id = :id FOR UPDATE"),
        {"id": user_id},
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    return row


def _actor(caller: Caller) -> audit.AuditActor:
    a = caller.account
    return audit.AuditActor(role=a.role, user_id=a.user_id, email=a.email)


def _audit(
    caller: Caller,
    request: Request,
    target: Row[Any],
    action: str,
    detail: dict[str, Any] | None = None,
) -> None:
    audit.write(
        caller.conn,
        tenant_id=caller.account.tenant_id,
        actor=_actor(caller),
        category="user",
        action=action,
        target_type="user",
        target_id=str(target.id),
        target_label=target.email,
        detail=detail,
        request=request_info(request),
    )


def _cognito_fields(caller: Caller, target: Row[Any]) -> dict[str, str]:
    return {
        "pool": "tenant",
        "tenant_id": str(caller.account.tenant_id),
        "email": target.email,
        "sub": target.cognito_sub,
    }


def _other_active_admins(conn: Connection, user_id: uuid.UUID) -> int:
    """Admins other than this user who are not disabled, locking them for the transaction."""
    rows = conn.execute(
        text(
            "SELECT id FROM assay.users WHERE role = :admin AND status <> 'disabled' "
            "AND id <> :id FOR UPDATE"
        ),
        {"admin": TENANT_ADMIN, "id": user_id},
    ).all()
    return len(rows)


@router.get("/users", response_model=UserList)
def list_users(caller: TenantAdmin) -> UserList:
    rows = caller.conn.execute(
        text(f"SELECT {USER_COLUMNS} ORDER BY lower(name), lower(email)")
    ).all()
    return UserList(users=[_out(r) for r in rows])


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def invite(body: InviteIn, caller: TenantAdmin, cognito: Cognito, request: Request) -> UserOut:
    name = " ".join(body.name.split())
    email = body.email.strip()
    existing = caller.conn.execute(
        text("SELECT 1 FROM assay.users WHERE lower(email) = lower(:email)"), {"email": email}
    ).first()
    if existing is not None:
        raise _conflict("user_exists")
    try:
        invited = invite_user(
            caller.conn,
            cognito,
            tenant_id=caller.account.tenant_id,
            name=name,
            email=email,
            role=body.role,
            invited_by=caller.account.user_id,
            actor=_actor(caller),
            request=request_info(request),
        )
    except CognitoRefused as exc:
        raise _cognito_error(exc) from None
    if not invited.created:
        raise _conflict("user_exists")
    return _out(_target(caller.conn, invited.user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
def change_user(
    user_id: uuid.UUID, body: UserChange, caller: TenantAdmin, cognito: Cognito, request: Request
) -> UserOut:
    conn = caller.conn
    target = _target(conn, user_id)
    is_self = target.id == caller.account.user_id
    calls: list[tuple[str, dict[str, Any]]] = []

    if body.role is not None and body.role != target.role:
        if is_self:
            raise _conflict("cannot_change_self")
        if target.role == TENANT_ADMIN and _other_active_admins(conn, target.id) == 0:
            raise _conflict("last_admin")
        conn.execute(
            text("UPDATE assay.users SET role = :role WHERE id = :id"),
            {"role": body.role, "id": target.id},
        )
        _audit(
            caller,
            request,
            target,
            "role_changed",
            {"before": {"role": target.role}, "after": {"role": body.role}},
        )
        calls.append(("set_role", {"role": body.role}))

    if body.status == "disabled" and target.status != "disabled":
        if is_self:
            raise _conflict("cannot_change_self")
        role_after = body.role or target.role
        if role_after == TENANT_ADMIN and _other_active_admins(conn, target.id) == 0:
            raise _conflict("last_admin")
        conn.execute(
            text(
                "UPDATE assay.users SET status = 'disabled', sessions_revoked_at = now() "
                "WHERE id = :id"
            ),
            {"id": target.id},
        )
        _audit(
            caller,
            request,
            target,
            "disabled",
            {"before": {"status": target.status}, "after": {"status": "disabled"}},
        )
        calls.append(("disable", {}))
    elif body.status == "active" and target.status == "disabled":
        restored = "active" if target.last_sign_in_at is not None else "invited"
        conn.execute(
            text("UPDATE assay.users SET status = :status WHERE id = :id"),
            {"status": restored, "id": target.id},
        )
        _audit(
            caller,
            request,
            target,
            "enabled",
            {"before": {"status": "disabled"}, "after": {"status": restored}},
        )
        calls.append(("enable", {}))

    try:
        for operation, extra in calls:
            cognito.call(operation, **_cognito_fields(caller, target), **extra)
    except CognitoRefused as exc:
        raise _cognito_error(exc) from None
    return _out(_target(conn, target.id))


@router.post("/users/{user_id}/resend-invite", status_code=status.HTTP_204_NO_CONTENT)
def resend_invite(
    user_id: uuid.UUID, caller: TenantAdmin, cognito: Cognito, request: Request
) -> Response:
    target = _target(caller.conn, user_id)
    if target.status != "invited":
        raise _conflict("wrong_state")
    _audit(caller, request, target, "invite_resent")
    try:
        cognito.call("resend_invite", **_cognito_fields(caller, target))
    except CognitoRefused as exc:
        raise _cognito_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: uuid.UUID, caller: TenantAdmin, cognito: Cognito, request: Request
) -> Response:
    target = _target(caller.conn, user_id)
    if target.status != "active":
        # Invited users have no password yet: resend the invitation instead.
        raise _conflict("wrong_state")
    _audit(caller, request, target, "password_reset_sent")
    try:
        cognito.call("reset_password", **_cognito_fields(caller, target))
    except CognitoRefused as exc:
        raise _cognito_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/{user_id}/reset-mfa", status_code=status.HTTP_204_NO_CONTENT)
def reset_mfa(
    user_id: uuid.UUID, caller: TenantAdmin, cognito: Cognito, request: Request
) -> Response:
    target = _target(caller.conn, user_id)
    if target.role != TENANT_USER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    caller.conn.execute(
        text("UPDATE assay.users SET sessions_revoked_at = now() WHERE id = :id"),
        {"id": target.id},
    )
    _audit(caller, request, target, "mfa_reset")
    try:
        cognito.call("reset_mfa", **_cognito_fields(caller, target))
    except CognitoRefused as exc:
        raise _cognito_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
