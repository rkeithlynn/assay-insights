"""GET /me: the signed-in user, their role and their tenant (BUILD_SPEC.md section 10)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from assay_api.security import TENANT_ADMIN, AnySignedIn
from assay_api.settings import PoolKind

router = APIRouter()


class MeUser(BaseModel):
    id: uuid.UUID
    name: str
    email: str


class MeTenant(BaseModel):
    id: uuid.UUID
    name: str


class Me(BaseModel):
    user: MeUser
    role: str
    tenant: MeTenant
    # Which sign-in the portal used: the tenant portal or the admin portal.
    pool: PoolKind
    # True for a tenant-admin who has not set up MFA yet. Every other route refuses them
    # with 403 mfa_required until they have.
    mfa_required: bool


@router.get("/me", response_model=Me)
def me(caller: AnySignedIn) -> Me:
    account = caller.account
    return Me(
        user=MeUser(id=account.user_id, name=account.name, email=account.email),
        role=account.role,
        tenant=MeTenant(id=account.tenant_id, name=account.tenant_name),
        pool=account.principal.pool,
        mfa_required=account.role == TENANT_ADMIN and not account.principal.mfa,
    )
