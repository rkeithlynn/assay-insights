"""Admin routes: tenants, their admins, and MFA resets (BUILD_SPEC.md sections 5 and 10).

Platform-pool tokens only. Reads run in the account check's read-only `platform_reader`
transaction, through `tenants` and the views in schema `platform`; master users see no
tenant content. Changes run in a `platform_writer` transaction of their own, which writes the
change and its audit row together and commits before the response is sent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Row, text

from assay_api.database import Databases
from assay_api.requestinfo import request_info
from assay_api.routes.users import Cognito
from assay_api.security import Caller, MasterAdmin, PlatformMember
from assay_api.services import CognitoRefused, Provisioning
from assay_api.tenants import (
    TenantConflict,
    create_tenant,
    first_admin_on_record,
    retry_tenant,
    set_status,
)
from assay_db import audit
from assay_db.tenants import MAX_NAME

router = APIRouter(prefix="/admin")

TenantKind = Literal["platform", "business"]
TenantStatus = Literal["provisioning", "active", "suspended", "failed"]
UserStatus = Literal["invited", "active", "disabled"]


def databases(request: Request) -> Databases:
    dbs: Databases = request.app.state.databases
    return dbs


def provisioning(request: Request) -> Provisioning:
    starter: Provisioning = request.app.state.provisioning
    return starter


Dbs = Annotated[Databases, Depends(databases)]
Starter = Annotated[Provisioning, Depends(provisioning)]


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: TenantKind
    status: TenantStatus
    users: int
    documents: int
    created_at: dt.datetime


class TenantList(BaseModel):
    tenants: list[TenantOut]


class TenantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    admin_name: str = Field(min_length=1, max_length=MAX_NAME)
    admin_email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]{1,64}@[^@\s]{1,255}$")


class TenantChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "suspended"]


class AdminOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: Literal["tenant-admin", "master-admin", "master-user"]
    status: UserStatus
    # Whether the master admin may reset this user's MFA (section 5): a tenant-admin or the
    # master-user, never the master admin (an AWS administrator resets that one).
    mfa_resettable: bool


class AdminList(BaseModel):
    admins: list[AdminOut]


TENANT_QUERY = """
SELECT t.id, t.name, t.kind, t.status, t.created_at,
       COALESCE(s.user_count, 0) AS users, COALESCE(s.document_count, 0) AS documents
FROM assay.tenants t
LEFT JOIN platform.tenant_summary_v s ON s.tenant_id = t.id
"""


def _tenant_out(row: Row[Any]) -> TenantOut:
    return TenantOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        status=row.status,
        users=row.users,
        documents=row.documents,
        created_at=row.created_at,
    )


def _one_tenant(conn: Connection, tenant_id: uuid.UUID) -> TenantOut:
    row = conn.execute(text(TENANT_QUERY + " WHERE t.id = :id"), {"id": tenant_id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")
    return _tenant_out(row)


def _actor(caller: Caller) -> audit.AuditActor:
    a = caller.account
    return audit.AuditActor(role=a.role, user_id=a.user_id, email=a.email)


def _conflict(exc: TenantConflict) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if exc.code == "tenant_not_found" else 409
    return HTTPException(status_code=code, detail=exc.code)


@router.get("/tenants", response_model=TenantList)
def list_tenants(caller: PlatformMember) -> TenantList:
    rows = caller.conn.execute(text(TENANT_QUERY + " ORDER BY t.created_at, t.name")).all()
    return TenantList(tenants=[_tenant_out(r) for r in rows])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_202_ACCEPTED)
def add_tenant(body: TenantIn, caller: MasterAdmin, dbs: Dbs, starter: Starter) -> TenantOut:
    try:
        with dbs.platform_writer() as writer:
            tenant_id = create_tenant(
                writer,
                starter,
                name=body.name,
                admin_name=body.admin_name,
                admin_email=body.admin_email,
                actor=_actor(caller),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with dbs.platform() as reader:
        return _one_tenant(reader, tenant_id)


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
def change_tenant(
    tenant_id: uuid.UUID, body: TenantChange, caller: MasterAdmin, dbs: Dbs, request: Request
) -> TenantOut:
    try:
        with dbs.platform_writer() as writer:
            set_status(
                writer,
                tenant_id=tenant_id,
                status=body.status,
                actor=_actor(caller),
                request=request_info(request),
            )
    except TenantConflict as exc:
        raise _conflict(exc) from None
    with dbs.platform() as reader:
        return _one_tenant(reader, tenant_id)


@router.post(
    "/tenants/{tenant_id}/retry", response_model=TenantOut, status_code=status.HTTP_202_ACCEPTED
)
def retry(tenant_id: uuid.UUID, caller: MasterAdmin, dbs: Dbs, starter: Starter) -> TenantOut:
    current = _one_tenant(caller.conn, tenant_id)
    if current.kind != "business" or current.status != "failed":
        raise HTTPException(status_code=409, detail="not_failed")
    try:
        first_admin = first_admin_on_record(caller.conn, tenant_id)
        with dbs.platform_writer() as writer:
            retry_tenant(
                writer,
                starter,
                tenant_id=tenant_id,
                first_admin=first_admin,
                actor=_actor(caller),
            )
    except TenantConflict as exc:
        raise _conflict(exc) from None
    with dbs.platform() as reader:
        return _one_tenant(reader, tenant_id)


@router.get("/tenants/{tenant_id}/admins", response_model=AdminList)
def tenant_admins(tenant_id: uuid.UUID, caller: PlatformMember) -> AdminList:
    """A business tenant's admins; for the platform tenant, its master admin and user."""
    tenant = _one_tenant(caller.conn, tenant_id)
    if tenant.kind == "platform":
        query = (
            "SELECT user_id, name, email, role, status FROM platform.platform_users_v "
            "WHERE tenant_id = :id ORDER BY role, lower(name), lower(email)"
        )
    else:
        query = (
            "SELECT user_id, name, email, 'tenant-admin' AS role, status "
            "FROM platform.tenant_admins_v WHERE tenant_id = :id ORDER BY lower(name), lower(email)"
        )
    rows = caller.conn.execute(text(query), {"id": tenant_id}).all()
    return AdminList(
        admins=[
            AdminOut(
                id=r.user_id,
                name=r.name,
                email=r.email,
                role=r.role,
                status=r.status,
                mfa_resettable=r.role in {"tenant-admin", "master-user"},
            )
            for r in rows
        ]
    )


@router.post("/users/{user_id}/reset-mfa", status_code=status.HTTP_204_NO_CONTENT)
def reset_admin_mfa(
    user_id: uuid.UUID, caller: MasterAdmin, dbs: Dbs, cognito: Cognito, request: Request
) -> Response:
    """Reset MFA for a tenant-admin or the master-user; nobody else (section 5)."""
    reader = caller.conn
    admin = reader.execute(
        text(
            "SELECT user_id, tenant_id, cognito_sub, email FROM platform.tenant_admins_v "
            "WHERE user_id = :id"
        ),
        {"id": user_id},
    ).one_or_none()
    target: Row[Any] | None = admin
    if admin is not None:
        role, pool = "tenant-admin", "tenant"
    else:
        target = reader.execute(
            text(
                "SELECT user_id, tenant_id, cognito_sub, email FROM platform.platform_users_v "
                "WHERE user_id = :id AND role = 'master-user'"
            ),
            {"id": user_id},
        ).one_or_none()
        role, pool = "master-user", "platform"
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")

    fields: dict[str, str] = {"pool": pool, "email": target.email, "sub": target.cognito_sub}
    if pool == "tenant":
        fields["tenant_id"] = str(target.tenant_id)
    with dbs.platform_writer() as writer:
        updated = writer.execute(
            text(
                "UPDATE assay.users SET sessions_revoked_at = now() WHERE id = :id AND role = :role"
            ),
            {"id": target.user_id, "role": role},
        ).rowcount
        if updated != 1:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
        audit.write(
            writer,
            tenant_id=target.tenant_id,
            actor=_actor(caller),
            category="user",
            action="mfa_reset",
            target_type="user",
            target_id=str(target.user_id),
            target_label=target.email,
            detail={"role": role},
            request=request_info(request),
        )
        try:
            cognito.call("reset_mfa", **fields)
        except CognitoRefused as exc:
            code = exc.code if exc.code in {"user_not_found", "wrong_state"} else "cognito_failed"
            raise HTTPException(
                status_code=409 if code != "cognito_failed" else 502, detail=code
            ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
