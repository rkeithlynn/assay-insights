"""Route dependencies: token, pool, role, MFA, then the account check, in that order.

Every route except the health check must take its caller from one of the dependencies at
the bottom of this module. All of them are built by `require()`, which runs the shared
account check (`assay_api.accounts.load_account`) in the request's database transaction and
hands the route that same transaction. `tests/unit/test_api_routes.py` fails if a route
skips it.

- 401 `invalid_token`: no bearer token, or one that fails verification.
- 403 `wrong_pool`: a tenant-pool token on an admin route, or the reverse.
- 403 `forbidden`: the role may not use this route.
- 403 `mfa_required`: a tenant-admin whose session did not pass MFA. Only `GET /me` lets
  them through, and it reports `mfa_required` so the portal can set MFA up.
- 403 with an `assay_api.accounts.Refusal` code: the account check failed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection

from assay_api.accounts import Account, AccountRefused, load_account
from assay_api.auth import InvalidToken, Principal, TokenVerifier
from assay_api.claims import PLATFORM_GROUPS, TENANT_GROUPS
from assay_api.database import Databases
from assay_api.settings import PoolKind

# Marks the dependencies built by require(); the route test looks for it.
ACCOUNT_CHECK_ATTRIBUTE = "__assay_account_check__"

TENANT_ADMIN = "tenant-admin"

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Caller:
    """A caller that passed every check, and the open transaction the check ran in."""

    account: Account
    conn: Connection

    @property
    def principal(self) -> Principal:
        return self.account.principal


def _forbidden(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=code)


def _principal(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
    verifier: TokenVerifier = request.app.state.verifier
    token = None if credentials is None else credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verifier.verify(token)
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None


def require(
    *,
    pools: frozenset[PoolKind],
    roles: frozenset[str],
    allow_without_mfa: bool = False,
) -> Callable[..., Iterator[Caller]]:
    """A dependency that admits only these pools and roles, then checks the account."""

    def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Iterator[Caller]:
        principal = _principal(request, credentials)
        if principal.pool not in pools:
            raise _forbidden("wrong_pool")
        if principal.role not in roles:
            raise _forbidden("forbidden")
        if principal.role == TENANT_ADMIN and not principal.mfa and not allow_without_mfa:
            raise _forbidden("mfa_required")

        databases: Databases = request.app.state.databases
        if principal.pool == "tenant":
            if principal.tenant_id is None:  # the verifier never allows this
                raise _forbidden("forbidden")
            transaction = databases.tenant(principal.tenant_id)
        else:
            transaction = databases.platform()
        with transaction as conn:
            try:
                account = load_account(conn, principal)
            except AccountRefused as exc:
                raise _forbidden(exc.reason.value) from None
            yield Caller(account=account, conn=conn)

    setattr(dependency, ACCOUNT_CHECK_ATTRIBUTE, True)
    return dependency


_TENANT: frozenset[PoolKind] = frozenset({"tenant"})
_PLATFORM: frozenset[PoolKind] = frozenset({"platform"})

# GET /me and POST /auth/signed-out: anyone signed in, a tenant-admin even before MFA setup.
AnySignedIn = Annotated[
    Caller,
    Depends(
        require(
            pools=_TENANT | _PLATFORM,
            roles=frozenset(TENANT_GROUPS + PLATFORM_GROUPS),
            allow_without_mfa=True,
        )
    ),
]
TenantMember = Annotated[Caller, Depends(require(pools=_TENANT, roles=frozenset(TENANT_GROUPS)))]
TenantAdmin = Annotated[Caller, Depends(require(pools=_TENANT, roles=frozenset({TENANT_ADMIN})))]
PlatformMember = Annotated[
    Caller, Depends(require(pools=_PLATFORM, roles=frozenset(PLATFORM_GROUPS)))
]
MasterAdmin = Annotated[
    Caller, Depends(require(pools=_PLATFORM, roles=frozenset({"master-admin"})))
]
