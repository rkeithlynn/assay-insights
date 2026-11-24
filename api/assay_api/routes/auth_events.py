"""Audit rows the portal reports (BUILD_SPEC.md section 7a): sign-out, password change,
session start and MFA enrolment.

- `POST /auth/signed-out`: the portal calls it before it sends the user to Cognito's logout.
- `POST /auth/password-changed`: the portal changes the password with Cognito itself
  (`ChangePassword`, with the user's access token), so the password never reaches this API,
  and then calls this route to record it. A user who calls Cognito directly, outside the
  portal, leaves no row here; CloudTrail still records `ChangePassword`.

- `POST /auth/session-started`: the portal calls it once, right after a sign-in redirect
  completes, so the trail has the sign-in's origin. Cognito's post-authentication trigger
  sees neither the viewer's IP address nor the browser; this row has both, from the same
  headers as every row the API writes. The trigger's `sign_in` row stays the record of the
  sign-in; this one is the portal's report of the session it started.
  - One row per sign-in: its ID is derived from the user and the token's `auth_time`, and its
    time is `auth_time`, so a second call (or one after a token refresh, which keeps
    `auth_time`) conflicts on the primary key and writes nothing.
  - Only within `SESSION_START_WINDOW` of `auth_time`: later, the request's address may no
    longer be where the user signed in, so the call is refused (`session_not_new`).
- `POST /auth/mfa-enabled` (tenant pool only): the MFA setup screen calls it after Cognito
  accepted the code. The row is written only if Cognito confirms, through the Cognito admin
  Lambda's read-only `mfa_status`, that the caller's TOTP MFA is on (`mfa_not_enabled`
  otherwise). Platform-pool users set up MFA in managed login, which no trigger reports.

All four are open to every signed-in user, a tenant-admin before MFA setup included, and
record only the caller's own action. Tenant users' rows are written as `app_tenant` in their
own tenant; platform users' rows as `platform_writer`, in the platform tenant from the
account.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import Connection

from assay_api.database import Databases
from assay_api.requestinfo import request_info
from assay_api.routes.users import Cognito
from assay_api.security import AnySignedIn, Caller
from assay_api.services import CognitoRefused
from assay_db import audit

router = APIRouter(prefix="/auth")


def databases(request: Request) -> Databases:
    dbs: Databases = request.app.state.databases
    return dbs


Dbs = Annotated[Databases, Depends(databases)]

SESSION_START_WINDOW = dt.timedelta(minutes=5)
# A token's auth_time may be a little ahead of this task's clock.
CLOCK_LEEWAY = dt.timedelta(minutes=1)
# Fixed forever: session-start row IDs are derived from it.
SESSION_START_NAMESPACE = uuid.UUID("5d0f7c1e-3a52-4c8b-9d2e-6b1f0a7c4e93")


def _record(
    caller: Caller,
    dbs: Databases,
    request: Request,
    action: str,
    *,
    detail: dict[str, Any] | None = None,
    row_id: uuid.UUID | None = None,
    at: dt.datetime | None = None,
) -> Response:
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
            detail=detail,
            request=request_info(request),
            row_id=row_id,
            at=at,
        )

    if account.principal.pool == "tenant":
        write(caller.conn)
    else:
        with dbs.platform_writer() as writer:
            write(writer)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def session_start_id(user_id: uuid.UUID, auth_time: int) -> uuid.UUID:
    return uuid.uuid5(SESSION_START_NAMESPACE, f"session_started:{user_id}:{auth_time}")


@router.post("/signed-out", status_code=status.HTTP_204_NO_CONTENT)
def signed_out(caller: AnySignedIn, dbs: Dbs, request: Request) -> Response:
    return _record(caller, dbs, request, "sign_out")


@router.post("/password-changed", status_code=status.HTTP_204_NO_CONTENT)
def password_changed(caller: AnySignedIn, dbs: Dbs, request: Request) -> Response:
    return _record(caller, dbs, request, "password_changed")


@router.post("/session-started", status_code=status.HTTP_204_NO_CONTENT)
def session_started(caller: AnySignedIn, dbs: Dbs, request: Request) -> Response:
    signed_in_at = dt.datetime.fromtimestamp(caller.principal.auth_time, dt.UTC)
    now = dt.datetime.now(dt.UTC)
    if not now - SESSION_START_WINDOW <= signed_in_at <= now + CLOCK_LEEWAY:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session_not_new")
    return _record(
        caller,
        dbs,
        request,
        "session_started",
        detail={"reported_by": "portal"},
        row_id=session_start_id(caller.account.user_id, caller.principal.auth_time),
        at=signed_in_at,
    )


@router.post("/mfa-enabled", status_code=status.HTTP_204_NO_CONTENT)
def mfa_enabled(caller: AnySignedIn, dbs: Dbs, cognito: Cognito, request: Request) -> Response:
    account = caller.account
    if account.principal.pool != "tenant":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant_pool_only")
    try:
        result = cognito.call(
            "mfa_status",
            pool="tenant",
            tenant_id=str(account.tenant_id),
            email=account.email,
            sub=account.principal.sub,
        )
    except CognitoRefused as exc:
        if exc.code == "user_not_found":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from None
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="cognito_failed"
        ) from None
    if result.get("totp") is not True:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="mfa_not_enabled")
    return _record(caller, dbs, request, "mfa_enabled")
