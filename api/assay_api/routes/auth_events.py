"""Audit rows the portal reports: sign-out and password change (BUILD_SPEC.md section 7a).

- `POST /auth/signed-out`: the portal calls it before it sends the user to Cognito's logout.
- `POST /auth/password-changed`: the portal changes the password with Cognito itself
  (`ChangePassword`, with the user's access token), so the password never reaches this API,
  and then calls this route to record it. A user who calls Cognito directly, outside the
  portal, leaves no row here; CloudTrail still records `ChangePassword`.

Both are open to every signed-in user, a tenant-admin before MFA setup included, and record
only the caller's own action. Tenant users' rows are written as `app_tenant` in their own
tenant; platform users' rows as `platform_writer`, in the platform tenant from the account.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import Connection

from assay_api.database import Databases
from assay_api.requestinfo import request_info
from assay_api.security import AnySignedIn, Caller
from assay_db import audit

router = APIRouter(prefix="/auth")


def databases(request: Request) -> Databases:
    dbs: Databases = request.app.state.databases
    return dbs


Dbs = Annotated[Databases, Depends(databases)]


def _record(caller: Caller, dbs: Databases, request: Request, action: str) -> Response:
    account = caller.account

    def write(conn: Connection) -> None:
        audit.write(
            conn,
            tenant_id=account.tenant_id,
            actor=audit.AuditActor(role=account.role, user_id=account.user_id, email=account.email),
            category="auth",
            action=action,
            target_type="user",
            target_id=str(account.user_id),
            target_label=account.email,
            request=request_info(request),
        )

    if account.principal.pool == "tenant":
        write(caller.conn)
    else:
        with dbs.platform_writer() as writer:
            write(writer)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/signed-out", status_code=status.HTTP_204_NO_CONTENT)
def signed_out(caller: AnySignedIn, dbs: Dbs, request: Request) -> Response:
    return _record(caller, dbs, request, "sign_out")


@router.post("/password-changed", status_code=status.HTTP_204_NO_CONTENT)
def password_changed(caller: AnySignedIn, dbs: Dbs, request: Request) -> Response:
    return _record(caller, dbs, request, "password_changed")
